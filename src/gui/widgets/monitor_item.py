"""MonitorItem - represents a physical display in the layout editor."""

from PySide6.QtCore import Qt, QRectF, Signal, QPointF
from PySide6.QtGui import QColor, QPen, QBrush, QFont, QPainter, QCursor
from PySide6.QtWidgets import QGraphicsObject, QGraphicsSceneMouseEvent, QMenu

from ..services.screen_info import MonitorInfo
from .slot_item import SlotItem


# Split button size as fraction of monitor dimensions
SPLIT_BTN_W_FRAC = 0.15   # 15% of monitor width
SPLIT_BTN_H_FRAC = 0.07   # 7% of monitor height
SPLIT_BTN_MARGIN = 0.01   # 1% margin from edges

# Split-button cycle. 'H Split' = side by side, 'V Split' = top and bottom
# (the split line's direction). Top and bottom comes first: most games' UI
# copes better with a wide tile than a tall one. Game.sideBySideFirst swaps it.
# Grids for 5-8 players follow Quad: 3x3 has 16:9 tiles (any game copes),
# 4x2 the biggest ones.
GRID_PRESETS = {'Grid 3x2': (3, 2), 'Grid 3x3': (3, 3), 'Grid 4x2': (4, 2)}
PRESET_CYCLE = ['Full', 'V Split', 'H Split', 'Quad', *GRID_PRESETS]
PRESET_CYCLE_SIDE_BY_SIDE = ['Full', 'H Split', 'V Split', 'Quad', *GRID_PRESETS]
PRESET_LABELS = {'H Split': 'Side by side', 'V Split': 'Top/bottom',
                 'Grid 3x2': '3 × 2', 'Grid 3x3': '3 × 3', 'Grid 4x2': '4 × 2'}


class SplitNode:
    """BSP tree node for recursive monitor splitting."""

    __slots__ = ('rect', 'direction', 'children', 'parent_node', 'ratio')

    def __init__(self, rect: QRectF, parent_node: 'SplitNode | None' = None):
        self.rect = QRectF(rect)
        self.direction: str | None = None  # 'horizontal' or 'vertical'; None = leaf
        self.children: tuple['SplitNode', 'SplitNode'] | None = None
        self.parent_node: SplitNode | None = parent_node
        self.ratio: float = 0.5

    @property
    def is_leaf(self) -> bool:
        return self.children is None

    def split(self, direction: str, ratio: float = 0.5) -> tuple['SplitNode', 'SplitNode']:
        """Split this leaf into two children. Returns (first, second)."""
        assert self.is_leaf, "Cannot split a non-leaf node"
        self.direction = direction
        self.ratio = max(0.1, min(0.9, ratio))
        r = self.rect
        if direction == 'horizontal':
            w1 = r.width() * self.ratio
            left_rect = QRectF(r.x(), r.y(), w1, r.height())
            right_rect = QRectF(r.x() + w1, r.y(), r.width() - w1, r.height())
            self.children = (SplitNode(left_rect, self), SplitNode(right_rect, self))
        else:  # vertical
            h1 = r.height() * self.ratio
            top_rect = QRectF(r.x(), r.y(), r.width(), h1)
            bot_rect = QRectF(r.x(), r.y() + h1, r.width(), r.height() - h1)
            self.children = (SplitNode(top_rect, self), SplitNode(bot_rect, self))
        return self.children

    def merge(self):
        """Merge this internal node back into a leaf (discards children)."""
        assert not self.is_leaf, "Cannot merge a leaf node"
        self.direction = None
        self.children = None

    def leaves(self) -> list['SplitNode']:
        """Return all leaf nodes in depth-first (left-then-right) order."""
        if self.is_leaf:
            return [self]
        result = []
        result.extend(self.children[0].leaves())
        result.extend(self.children[1].leaves())
        return result

    def sibling(self) -> 'SplitNode | None':
        """Return this node's sibling, or None if root."""
        if self.parent_node is None:
            return None
        a, b = self.parent_node.children
        return b if a is self else a

    def leaf_index(self) -> int | None:
        """Return the index of this leaf among root's leaves, or None if not a leaf."""
        if not self.is_leaf:
            return None
        root = self
        while root.parent_node is not None:
            root = root.parent_node
        for i, leaf in enumerate(root.leaves()):
            if leaf is self:
                return i
        return None

    def recompute_rects(self, rect: QRectF):
        """Recursively recompute all child rects from a bounding rect using stored ratios."""
        self.rect = QRectF(rect)
        if self.is_leaf:
            return
        r = rect
        if self.direction == 'horizontal':
            w1 = r.width() * self.ratio
            self.children[0].recompute_rects(QRectF(r.x(), r.y(), w1, r.height()))
            self.children[1].recompute_rects(QRectF(r.x() + w1, r.y(), r.width() - w1, r.height()))
        else:  # vertical
            h1 = r.height() * self.ratio
            self.children[0].recompute_rects(QRectF(r.x(), r.y(), r.width(), h1))
            self.children[1].recompute_rects(QRectF(r.x(), r.y() + h1, r.width(), r.height() - h1))

    def internal_nodes(self) -> list['SplitNode']:
        """Return all non-leaf nodes in depth-first order."""
        if self.is_leaf:
            return []
        result = [self]
        result.extend(self.children[0].internal_nodes())
        result.extend(self.children[1].internal_nodes())
        return result

    def matches_preset(self) -> str:
        """Detect if this tree matches a known preset layout. Returns preset name."""
        if self.is_leaf:
            return 'Full'
        if self.direction == 'horizontal' and self.children[0].is_leaf and self.children[1].is_leaf:
            return 'H Split'
        if self.direction == 'vertical' and self.children[0].is_leaf and self.children[1].is_leaf:
            return 'V Split'
        # Quad: root splits one way, each child splits the other way
        if not self.is_leaf and self.children[0].children and self.children[1].children:
            c0, c1 = self.children
            if (not c0.is_leaf and not c1.is_leaf
                    and c0.children[0].is_leaf and c0.children[1].is_leaf
                    and c1.children[0].is_leaf and c1.children[1].is_leaf):
                # Check it's a 2x2 grid (root horizontal, children vertical or vice versa)
                if self.direction != c0.direction and c0.direction == c1.direction:
                    return 'Quad'
        mine = self.to_dict()
        for name in GRID_PRESETS:
            if build_preset_tree(name, self.rect).to_dict() == mine:
                return name
        return 'Custom'

    def to_dict(self) -> dict:
        """Serialize tree structure."""
        if self.is_leaf:
            return {'type': 'leaf'}
        d = {
            'type': 'split',
            'direction': self.direction,
            'children': [self.children[0].to_dict(), self.children[1].to_dict()],
        }
        if self.ratio != 0.5:
            d['ratio'] = round(self.ratio, 4)
        return d

    @staticmethod
    def from_dict(data: dict, rect: QRectF, parent: 'SplitNode | None' = None) -> 'SplitNode':
        """Deserialize tree structure, computing rects from the given bounding rect."""
        node = SplitNode(rect, parent)
        if data.get('type') == 'split':
            direction = data['direction']
            node.direction = direction
            ratio = data.get('ratio', 0.5)
            node.ratio = max(0.1, min(0.9, ratio))
            r = rect
            if direction == 'horizontal':
                w1 = r.width() * node.ratio
                left_rect = QRectF(r.x(), r.y(), w1, r.height())
                right_rect = QRectF(r.x() + w1, r.y(), r.width() - w1, r.height())
            else:
                h1 = r.height() * node.ratio
                left_rect = QRectF(r.x(), r.y(), r.width(), h1)
                right_rect = QRectF(r.x(), r.y() + h1, r.width(), r.height() - h1)
            children_data = data.get('children', [{}, {}])
            child_a = SplitNode.from_dict(children_data[0], left_rect, node)
            child_b = SplitNode.from_dict(children_data[1], right_rect, node)
            node.children = (child_a, child_b)
        return node


def build_preset_tree(name: str, rect: QRectF) -> SplitNode:
    """Build a BSP tree matching a named preset."""
    root = SplitNode(rect)
    if name == 'Full':
        pass  # single leaf
    elif name == 'H Split':
        root.split('horizontal')
    elif name == 'V Split':
        root.split('vertical')
    elif name == 'Quad':
        root.split('horizontal')
        root.children[0].split('vertical')
        root.children[1].split('vertical')
    elif name in GRID_PRESETS:
        cols, rows = GRID_PRESETS[name]
        for row in _split_evenly(root, 'vertical', rows):
            _split_evenly(row, 'horizontal', cols)
    return root


def _split_evenly(node: SplitNode, direction: str, n: int) -> list[SplitNode]:
    """Split a leaf into n equal parts along direction; returns the parts."""
    if n <= 1:
        return [node]
    first, rest = node.split(direction, 1 / n)
    return [first] + _split_evenly(rest, direction, n - 1)


class SplitButtonItem(QGraphicsObject):
    """Split button + monitor label overlay. Renders above slots (z=1)."""

    clicked = Signal()

    def __init__(self, btn_rect: QRectF, label_rect: QRectF, monitor: 'MonitorInfo', parent=None):
        super().__init__(parent)
        self._btn_rect = btn_rect
        self._label_rect = label_rect
        self._monitor = monitor
        self._mode_label = 'Full'
        self.setZValue(1)  # Above slots (z=0)
        self.setAcceptedMouseButtons(Qt.LeftButton)

    def boundingRect(self):
        return self._btn_rect.united(self._label_rect)

    def set_mode_label(self, label: str):
        self._mode_label = PRESET_LABELS.get(label, label)
        self.update()

    def paint(self, painter: QPainter, option, widget=None):
        painter.setRenderHint(QPainter.Antialiasing)

        # Split button background
        painter.setBrush(QBrush(QColor(70, 70, 70)))
        painter.setPen(QPen(QColor(160, 160, 160), 2))
        painter.drawRoundedRect(self._btn_rect, 8, 8)

        # Button text
        painter.setPen(QPen(QColor(220, 220, 220)))
        btn_font = QFont()
        btn_font.setPointSize(max(8, int(self._btn_rect.height() / 3)))
        btn_font.setBold(True)
        painter.setFont(btn_font)
        painter.drawText(self._btn_rect, Qt.AlignCenter, self._mode_label)

        # Monitor name label
        painter.setPen(QPen(QColor(180, 180, 180)))
        label_font = QFont()
        label_font.setPointSize(max(8, int(self._btn_rect.height() / 2.5)))
        painter.setFont(label_font)
        label = f"{self._monitor.name}  {self._monitor.width}x{self._monitor.height}"
        painter.drawText(self._label_rect, Qt.AlignLeft | Qt.AlignVCenter, label)

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent):
        if self._btn_rect.contains(event.pos()):
            self.clicked.emit()
            event.accept()
        else:
            event.ignore()


EDGE_GRAB_WIDTH = 12  # pixels wide grab zone centered on edge line
EDGE_LINE_WIDTH = 3   # visual line thickness


class SplitEdgeItem(QGraphicsObject):
    """Draggable edge line at a BSP split boundary."""

    edgeMoved = Signal()

    def __init__(self, node: SplitNode, parent: 'MonitorItem'):
        super().__init__(parent)
        self.node = node
        self._hover = False
        self._dragging = False
        self.setZValue(0.5)  # Between slots (0) and split button (1)
        self.setAcceptHoverEvents(True)
        self.setAcceptedMouseButtons(Qt.LeftButton)
        self._line_rect = QRectF()   # prepareGeometryChange() asks for it first
        self._update_geometry()

    def _update_geometry(self):
        """Recalculate bounding rect from node state."""
        self.prepareGeometryChange()
        r = self.node.rect
        half = EDGE_GRAB_WIDTH / 2
        if self.node.direction == 'horizontal':
            # Vertical edge line
            x = r.x() + r.width() * self.node.ratio
            self._line_rect = QRectF(x - half, r.y(), EDGE_GRAB_WIDTH, r.height())
        else:
            # Horizontal edge line
            y = r.y() + r.height() * self.node.ratio
            self._line_rect = QRectF(r.x(), y - half, r.width(), EDGE_GRAB_WIDTH)

    def boundingRect(self):
        return self._line_rect

    def paint(self, painter: QPainter, option, widget=None):
        painter.setRenderHint(QPainter.Antialiasing)
        if self._hover or self._dragging:
            color = QColor(200, 200, 200, 180)
        else:
            color = QColor(150, 150, 150, 100)
        pen = QPen(color, EDGE_LINE_WIDTH)
        r = self.node.rect
        if self.node.direction == 'horizontal':
            x = r.x() + r.width() * self.node.ratio
            painter.setPen(pen)
            painter.drawLine(QPointF(x, r.y()), QPointF(x, r.y() + r.height()))
        else:
            y = r.y() + r.height() * self.node.ratio
            painter.setPen(pen)
            painter.drawLine(QPointF(r.x(), y), QPointF(r.x() + r.width(), y))

    def hoverEnterEvent(self, event):
        self._hover = True
        if self.node.direction == 'horizontal':
            self.setCursor(QCursor(Qt.SizeHorCursor))
        else:
            self.setCursor(QCursor(Qt.SizeVerCursor))
        self.update()

    def hoverLeaveEvent(self, event):
        self._hover = False
        self.unsetCursor()
        self.update()

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent):
        if event.button() == Qt.LeftButton:
            self._dragging = True
            event.accept()
        else:
            event.ignore()

    def mouseMoveEvent(self, event: QGraphicsSceneMouseEvent):
        if not self._dragging:
            return
        pos = event.pos()
        r = self.node.rect
        if self.node.direction == 'horizontal':
            new_ratio = (pos.x() - r.x()) / r.width()
        else:
            new_ratio = (pos.y() - r.y()) / r.height()
        new_ratio = max(0.1, min(0.9, new_ratio))
        self.node.ratio = new_ratio
        self.node.recompute_rects(self.node.rect)
        self._update_geometry()
        self.edgeMoved.emit()

    def mouseReleaseEvent(self, event: QGraphicsSceneMouseEvent):
        if self._dragging:
            self._dragging = False
            self.update()

    def contextMenuEvent(self, event):
        menu = QMenu()
        menu.addAction("Reset to Center").setData('reset')
        chosen = menu.exec(event.screenPos())
        if chosen and chosen.data() == 'reset':
            self.node.ratio = 0.5
            self.node.recompute_rects(self.node.rect)
            self._update_geometry()
            self.edgeMoved.emit()


class MonitorItem(QGraphicsObject):
    """One physical display, containing a BSP tree of SlotItems."""

    splitChanged = Signal()
    controllerPlaced = Signal()
    controllerRemoved = Signal(int)  # controller_index
    indexAssignRequested = Signal(object, int)  # slot, desired_index

    def __init__(self, monitor: MonitorInfo, parent=None):
        super().__init__(parent)
        self.monitor = monitor
        self.setPos(monitor.x, monitor.y)
        self.side_by_side_first = False   # Game.sideBySideFirst

        self._root = SplitNode(QRectF(0, 0, monitor.width, monitor.height))
        self._leaf_slots: dict[int, SlotItem] = {}  # leaf_id(id(node)) -> SlotItem
        self._edge_items: list[SplitEdgeItem] = []

        # Compute split button rect in monitor coordinates
        btn_w = monitor.width * SPLIT_BTN_W_FRAC
        btn_h = monitor.height * SPLIT_BTN_H_FRAC
        margin = min(monitor.width, monitor.height) * SPLIT_BTN_MARGIN
        btn_rect = QRectF(margin, margin, btn_w, btn_h)
        label_rect = QRectF(
            margin + btn_w + margin, margin,
            monitor.width - btn_w - margin * 3, btn_h,
        )

        # Split button overlay (z=1, renders above slots)
        self._split_btn = SplitButtonItem(btn_rect, label_rect, monitor, parent=self)
        self._split_btn.clicked.connect(self._cycle_split)

        self.setAcceptedMouseButtons(Qt.NoButton)
        self._rebuild_slots()

    def boundingRect(self):
        return QRectF(0, 0, self.monitor.width, self.monitor.height)

    def _rebuild_slots(self):
        """Rebuild all SlotItems and SplitEdgeItems from the BSP tree."""
        # Remove old slots
        for slot in self._leaf_slots.values():
            slot.setParentItem(None)
            if self.scene():
                self.scene().removeItem(slot)
        self._leaf_slots.clear()

        for leaf in self._root.leaves():
            self._make_slot_for_leaf(leaf)

        self._rebuild_edges()
        self._split_btn.set_mode_label(self._root.matches_preset())

    def overlay_rect(self) -> QRectF:
        """The split button + monitor label band drawn over the slots."""
        return self._split_btn.boundingRect()

    def _make_slot_for_leaf(self, leaf: SplitNode) -> SlotItem:
        """Create a SlotItem for a leaf node and wire signals."""
        slot = SlotItem(leaf.rect, parent=self)
        slot.node = leaf
        slot.controllerPlaced.connect(self._on_controller_placed)
        slot.profileChanged.connect(lambda _slot: self.splitChanged.emit())
        slot.controllerRemoved.connect(self._on_controller_removed)
        slot.expandRequested.connect(self._on_expand_requested)
        slot.mergeRequested.connect(self._on_merge_requested)
        slot.indexAssignRequested.connect(self._on_index_assign_requested)
        slot.splitRequested.connect(self._on_split_requested)
        self._leaf_slots[id(leaf)] = slot
        return slot

    def _on_controller_placed(self, slot):
        self.controllerPlaced.emit()

    def _on_controller_removed(self, controller_index):
        self.controllerRemoved.emit(controller_index)

    def _on_expand_requested(self, slot):
        self.expand_into_sibling(slot)

    def _on_merge_requested(self, slot):
        self.merge_slot(slot)

    def _on_index_assign_requested(self, slot, desired_index):
        self.indexAssignRequested.emit(slot, desired_index)

    def _on_split_requested(self, slot, direction):
        self.split_slot(slot, direction)

    def _rebuild_edges(self):
        """Create SplitEdgeItems for all internal nodes."""
        for edge in self._edge_items:
            edge.setParentItem(None)
            if self.scene():
                self.scene().removeItem(edge)
        self._edge_items.clear()

        for node in self._root.internal_nodes():
            edge = SplitEdgeItem(node, parent=self)
            edge.edgeMoved.connect(self._on_edge_moved)
            self._edge_items.append(edge)

    def _on_edge_moved(self):
        """Called when any edge is dragged. Update all slot rects and edge positions."""
        self._update_slot_rects()
        self._update_edge_positions()
        self.splitChanged.emit()

    def _update_slot_rects(self):
        """Sync each SlotItem's rect with its SplitNode's rect."""
        for leaf in self._root.leaves():
            slot = self._leaf_slots.get(id(leaf))
            if slot:
                slot.set_rect(leaf.rect)
                slot.update()

    def _update_edge_positions(self):
        """Recalculate all edge item positions from their nodes."""
        for edge in self._edge_items:
            edge._update_geometry()
            edge.update()

    def apply_preset(self, name: str):
        """Build a specific preset tree, displacing existing controllers."""
        displaced = []
        for slot in self._leaf_slots.values():
            if slot.has_controller():
                displaced.append((slot.controller_index, slot.controller_name, slot._vid, slot._pid))

        rect = QRectF(0, 0, self.monitor.width, self.monitor.height)
        self._root = build_preset_tree(name, rect)
        self._rebuild_slots()
        self.splitChanged.emit()

        for idx, _name, _vid, _pid in displaced:
            self.controllerRemoved.emit(idx)

    def _cycle_split(self):
        """Cycle through preset split modes."""
        cycle = PRESET_CYCLE_SIDE_BY_SIDE if self.side_by_side_first else PRESET_CYCLE
        current = self._root.matches_preset()
        if current in cycle:
            idx = cycle.index(current)
            next_preset = cycle[(idx + 1) % len(cycle)]
        else:
            next_preset = 'Full'
        self.apply_preset(next_preset)

    def split_slot(self, slot: SlotItem, direction: str):
        """Split an empty leaf slot into two children."""
        leaf = slot.node
        if leaf is None or not leaf.is_leaf:
            return

        # Remove old slot
        del self._leaf_slots[id(leaf)]
        slot.setParentItem(None)
        if self.scene():
            self.scene().removeItem(slot)

        # Split the leaf
        child_a, child_b = leaf.split(direction)
        self._make_slot_for_leaf(child_a)
        self._make_slot_for_leaf(child_b)

        self._rebuild_edges()
        self._split_btn.set_mode_label(self._root.matches_preset())
        self.splitChanged.emit()

    def merge_slot(self, slot: SlotItem):
        """Merge two empty sibling leaves back into one."""
        leaf = slot.node
        if leaf is None or leaf.parent_node is None:
            return
        sibling = leaf.sibling()
        if sibling is None or not sibling.is_leaf:
            return
        # Both must be empty
        sib_slot = self._leaf_slots.get(id(sibling))
        if sib_slot and sib_slot.has_controller():
            return
        if slot.has_controller():
            return

        parent = leaf.parent_node

        # Remove both slots
        for node in (leaf, sibling):
            s = self._leaf_slots.pop(id(node), None)
            if s:
                s.setParentItem(None)
                if self.scene():
                    self.scene().removeItem(s)

        # Merge parent back to leaf
        parent.merge()
        self._make_slot_for_leaf(parent)

        self._rebuild_edges()
        self._split_btn.set_mode_label(self._root.matches_preset())
        self.splitChanged.emit()

    def expand_into_sibling(self, slot: SlotItem):
        """Expand a placed controller into its empty sibling leaf."""
        leaf = slot.node
        if leaf is None or leaf.parent_node is None:
            return
        sibling = leaf.sibling()
        if sibling is None or not sibling.is_leaf:
            return
        sib_slot = self._leaf_slots.get(id(sibling))
        if sib_slot and sib_slot.has_controller():
            return
        if not slot.has_controller():
            return

        # Save controller data
        ctrl_idx = slot.controller_index
        ctrl_name = slot.controller_name
        ctrl_vid = slot._vid
        ctrl_pid = slot._pid
        inst_idx = slot.instance_index
        prof = slot.profile_id

        parent = leaf.parent_node

        # Remove both slots
        for node in (leaf, sibling):
            s = self._leaf_slots.pop(id(node), None)
            if s:
                s.setParentItem(None)
                if self.scene():
                    self.scene().removeItem(s)

        # Merge parent back to leaf
        parent.merge()
        new_slot = self._make_slot_for_leaf(parent)
        new_slot.place_controller(ctrl_idx, ctrl_name, instance_index=inst_idx, vid=ctrl_vid, pid=ctrl_pid,
                                  profile_id=prof)

        self._rebuild_edges()
        self._split_btn.set_mode_label(self._root.matches_preset())
        self.controllerPlaced.emit()

    def can_expand(self, slot: SlotItem) -> bool:
        """Check if a placed slot can expand into its sibling."""
        leaf = slot.node
        if leaf is None or leaf.parent_node is None:
            return False
        sibling = leaf.sibling()
        if sibling is None or not sibling.is_leaf:
            return False
        sib_slot = self._leaf_slots.get(id(sibling))
        if sib_slot and sib_slot.has_controller():
            return False
        return True

    def can_merge(self, slot: SlotItem) -> bool:
        """Check if an empty slot can merge with its sibling."""
        leaf = slot.node
        if leaf is None or leaf.parent_node is None:
            return False
        sibling = leaf.sibling()
        if sibling is None or not sibling.is_leaf:
            return False
        sib_slot = self._leaf_slots.get(id(sibling))
        if sib_slot and sib_slot.has_controller():
            return False
        return True

    def paint(self, painter: QPainter, option, widget=None):
        painter.setRenderHint(QPainter.Antialiasing)

        # Monitor background only — button/label drawn by SplitButtonItem child
        painter.setBrush(QBrush(QColor(35, 35, 35)))
        painter.setPen(QPen(QColor(100, 100, 100), 3))
        painter.drawRect(QRectF(0, 0, self.monitor.width, self.monitor.height))

    def get_placed_slots(self) -> list[SlotItem]:
        """Return all slots that have a controller placed."""
        return [s for s in self._leaf_slots.values() if s.has_controller()]

    def get_all_slots(self) -> list[SlotItem]:
        """Return all leaf slots."""
        return list(self._leaf_slots.values())

    def get_layout_state(self) -> dict:
        """Serialize monitor layout state for persistence (version 2 BSP format)."""
        state = {
            'version': 2,
            'tree': self._root.to_dict(),
            'placements': {},
        }
        leaves = self._root.leaves()
        for i, leaf in enumerate(leaves):
            slot = self._leaf_slots.get(id(leaf))
            if slot and slot.has_controller():
                placement = {
                    'controller': slot.controller_index,
                    'name': slot.controller_name,
                    'vid': slot._vid,
                    'pid': slot._pid,
                }
                if slot.instance_index is not None:
                    placement['instance_index'] = slot.instance_index
                if slot.profile_id:
                    placement['profile'] = slot.profile_id
                state['placements'][str(i)] = placement
        return state

    def load_layout_state(self, state: dict):
        """Restore monitor layout state from persistence."""
        version = state.get('version', 1)

        if version >= 2:
            self._load_v2(state)
        else:
            self._load_legacy(state)

    def _load_v2(self, state: dict):
        """Load version 2 BSP tree format."""
        tree_data = state.get('tree', {'type': 'leaf'})
        rect = QRectF(0, 0, self.monitor.width, self.monitor.height)
        self._root = SplitNode.from_dict(tree_data, rect)
        self._rebuild_slots()

        placements = state.get('placements', {})
        leaves = self._root.leaves()
        for idx_str, placement in placements.items():
            idx = int(idx_str)
            if idx >= len(leaves):
                continue
            leaf = leaves[idx]
            slot = self._leaf_slots.get(id(leaf))
            if slot and isinstance(placement, dict):
                ctrl_idx = placement['controller']
                name = placement.get('name', '')
                inst_idx = placement.get('instance_index')
                vid = placement.get('vid', 0)
                pid = placement.get('pid', 0)
                slot.place_controller(ctrl_idx, name, instance_index=inst_idx, vid=vid, pid=pid,
                                      profile_id=placement.get('profile'))

    def _load_legacy(self, state: dict):
        """Load version 1 grid-based format (backward compat)."""
        mode = state.get('split_mode', 'full')

        # Map old modes to presets
        mode_to_preset = {
            'full': 'Full',
            'h2': 'H Split',
            'v2': 'V Split',
            'quad': 'Quad',
        }
        preset = mode_to_preset.get(mode, 'Full')
        rect = QRectF(0, 0, self.monitor.width, self.monitor.height)
        self._root = build_preset_tree(preset, rect)
        self._rebuild_slots()

        placements = state.get('placements', {})
        leaves = self._root.leaves()

        # Map old cell keys to leaf indices
        # Full: (0,0) -> 0
        # H Split: (0,0) -> 0, (0,1) -> 1
        # V Split: (0,0) -> 0, (1,0) -> 1
        # Quad: (0,0) -> 0, (0,1) -> 1, (1,0) -> 2, (1,1) -> 3
        if mode == 'quad':
            cell_to_leaf = {
                '0,0': 0,  # top-left
                '0,1': 1,  # top-right (H split first child -> V split)
                '1,0': 2,  # bottom-left (H split second child -> V split)
                '1,1': 3,  # bottom-right
            }
        elif mode == 'h2':
            cell_to_leaf = {'0,0': 0, '0,1': 1}
        elif mode == 'v2':
            cell_to_leaf = {'0,0': 0, '1,0': 1}
        else:
            cell_to_leaf = {'0,0': 0}

        for cell_key, placement in placements.items():
            leaf_idx = cell_to_leaf.get(cell_key)
            if leaf_idx is None or leaf_idx >= len(leaves):
                continue
            leaf = leaves[leaf_idx]
            slot = self._leaf_slots.get(id(leaf))
            if slot is None:
                continue

            if isinstance(placement, dict):
                ctrl_idx = placement['controller']
                name = placement.get('name', '')
                inst_idx = placement.get('instance_index')
                vid = placement.get('vid', 0)
                pid = placement.get('pid', 0)
                slot.place_controller(ctrl_idx, name, instance_index=inst_idx, vid=vid, pid=pid)
            else:
                slot.place_controller(int(placement), '')

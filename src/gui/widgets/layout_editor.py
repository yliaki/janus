"""Layout editor - Nucleus Co-Op style monitor + controller drag-drop."""

from PySide6.QtCore import Signal, Qt, QRectF
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QGraphicsView, QGraphicsScene, QLabel,
)

from ..models.config import InstanceConfig
from ..models.controller_model import ControllerModel
from ..services.screen_info import get_all_monitors
from .monitor_item import MonitorItem
from .controller_dock import ControllerDock


class LayoutEditor(QWidget):
    """Multi-monitor layout editor with drag-drop controller placement."""

    configurationChanged = Signal()

    def __init__(self, controller_model: ControllerModel, parent=None):
        super().__init__(parent)
        self._controller_model = controller_model
        self._monitors: list[MonitorItem] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        layout.addWidget(QLabel("Layout"))

        self._scene = QGraphicsScene(self)
        self._view = QGraphicsView(self._scene)
        self._view.setRenderHints(
            QPainter.Antialiasing | QPainter.SmoothPixmapTransform
        )
        self._view.setDragMode(QGraphicsView.DragMode.NoDrag)
        self._view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._view.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._view.setAcceptDrops(True)
        layout.addWidget(self._view, stretch=1)

        # Controller dock
        self._dock = ControllerDock(controller_model, self)
        self._dock.buttonPressed.connect(self._flash_placed_controller)
        layout.addWidget(self._dock)

        # Re-sync placed state when controllers are hot-plugged
        controller_model.modelReset.connect(self._on_controllers_changed)

        self._setup_monitors()

    def _setup_monitors(self):
        """Detect monitors and add MonitorItems to scene."""
        self._scene.clear()
        self._monitors.clear()

        monitors = get_all_monitors()
        for mon in monitors:
            item = MonitorItem(mon)
            item.splitChanged.connect(self._on_change)
            item.controllerPlaced.connect(self._on_controller_placed_in_monitor)
            item.controllerRemoved.connect(self._on_controller_removed_from_monitor)
            item.indexAssignRequested.connect(self._on_index_assign_requested)
            self._scene.addItem(item)
            self._monitors.append(item)

        self._fit_scene()

    def set_side_by_side_first(self, on: bool):
        """Game.sideBySideFirst: the split button offers side by side before
        top and bottom."""
        for item in self._monitors:
            item.side_by_side_first = on

    def _fit_scene(self):
        """Fit the scene to show all monitors."""
        if not self._monitors:
            return
        # Build bounding rect of all monitors
        x_min = min(m.monitor.x for m in self._monitors)
        y_min = min(m.monitor.y for m in self._monitors)
        x_max = max(m.monitor.x + m.monitor.width for m in self._monitors)
        y_max = max(m.monitor.y + m.monitor.height for m in self._monitors)

        margin = 40
        self._scene.setSceneRect(QRectF(
            x_min - margin, y_min - margin,
            (x_max - x_min) + margin * 2,
            (y_max - y_min) + margin * 2,
        ))
        self._view.fitInView(self._scene.sceneRect(), Qt.KeepAspectRatio)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_scene()

    def showEvent(self, event):
        super().showEvent(event)
        self._fit_scene()

    def _on_change(self):
        self.configurationChanged.emit()

    def _on_controller_placed_in_monitor(self):
        """A controller was placed on a slot — update dock + auto-assign index + emit change."""
        self._sync_dock_from_monitors()
        # Auto-assign instance_index to newly placed slot (the one without an index)
        max_idx = 0
        newly_placed = None
        for mon in self._monitors:
            for slot in mon.get_placed_slots():
                if slot.instance_index is not None:
                    max_idx = max(max_idx, slot.instance_index)
                elif slot.has_controller():
                    newly_placed = slot
        if newly_placed is not None:
            newly_placed.instance_index = max_idx + 1
            newly_placed.update()
        self.configurationChanged.emit()

    def _on_controller_removed_from_monitor(self, controller_index: int):
        """A controller was removed from a slot — update dock + renumber + emit change."""
        # Only return to dock if the controller isn't placed on another slot
        # (handles drag-between-slots where removal fires from the source)
        for mon in self._monitors:
            for slot in mon.get_placed_slots():
                if slot.controller_index == controller_index:
                    return  # re-placed on another slot, don't return to dock
        self._dock.mark_returned(controller_index)
        self._renumber_indexes()
        self.configurationChanged.emit()

    def _flash_placed_controller(self, controller_index: int):
        """Flash the slot where this controller is placed (if any)."""
        for mon in self._monitors:
            for slot in mon.get_placed_slots():
                if slot.controller_index == controller_index:
                    slot.flash()
                    return

    def _on_index_assign_requested(self, slot, desired_index: int):
        """Swap instance indexes between the requesting slot and the slot that has desired_index."""
        old_index = slot.instance_index
        # Find the slot currently holding the desired index
        other = None
        for mon in self._monitors:
            for s in mon.get_placed_slots():
                if s is not slot and s.instance_index == desired_index:
                    other = s
                    break
            if other:
                break

        slot.instance_index = desired_index
        slot.update()
        if other is not None:
            other.instance_index = old_index
            other.update()
        self.configurationChanged.emit()

    def _renumber_indexes(self):
        """Reassign instance indexes 1..N sequentially, preserving relative order."""
        placed = []
        for mon in self._monitors:
            for slot in mon.get_placed_slots():
                placed.append(slot)
        # Sort by current instance_index (None sorts last)
        placed.sort(key=lambda s: s.instance_index if s.instance_index is not None else float('inf'))
        for i, slot in enumerate(placed, start=1):
            slot.instance_index = i
            slot.update()

    def _sync_dock_from_monitors(self):
        """Sync dock placed state from all monitor slots."""
        placed = set()
        for mon in self._monitors:
            for slot in mon.get_placed_slots():
                if slot.controller_index is not None:
                    placed.add(slot.controller_index)
        self._dock.sync_placed(placed)

    def get_instances(self) -> list[InstanceConfig]:
        """Collect all placed slots into InstanceConfig list, sorted by instance_index."""
        raw = []
        for mon in self._monitors:
            for slot in mon.get_placed_slots():
                if slot.controller_index is None:
                    continue
                rect = slot.boundingRect()
                pos_x = int(mon.monitor.x + rect.x())
                pos_y = int(mon.monitor.y + rect.y())
                width = int(rect.width())
                height = int(rect.height())
                idx = slot.instance_index if slot.instance_index is not None else 0
                raw.append((idx, slot.controller_index, width, height, pos_x, pos_y,
                            mon.monitor.name, slot._vid, slot._pid, slot.controller_name,
                            slot.profile_id))
        raw.sort(key=lambda t: t[0])
        instances = []
        for idx, ctrl_idx, w, h, px, py, mname, vid, pid, cname, prof in raw:
            instances.append(InstanceConfig(
                controller_index=ctrl_idx,
                instance_number=idx,
                gamescope_width=w,
                gamescope_height=h,
                pos_x=px,
                pos_y=py,
                monitor_name=mname,
                controller_vid=vid or 0,
                controller_pid=pid or 0,
                controller_name=cname or None,
                profile_id=prof,
            ))
        return instances

    def get_instance_count(self) -> int:
        """Return number of placed controllers."""
        count = 0
        for mon in self._monitors:
            count += len(mon.get_placed_slots())
        return count

    def get_layout_state(self) -> dict:
        """Serialize full layout state for round-trip persistence."""
        state = {'monitors': {}}
        for mon in self._monitors:
            state['monitors'][mon.monitor.name] = mon.get_layout_state()
        return state

    def load_layout_state(self, state: dict):
        """Restore layout state from saved config."""
        self._dock.clear_all_placed()
        monitors_state = state.get('monitors', {})
        for mon in self._monitors:
            if mon.monitor.name in monitors_state:
                mon.load_layout_state(monitors_state[mon.monitor.name])
        self._remove_disconnected_controllers()
        self._renumber_indexes()
        self._sync_dock_from_monitors()
        self.configurationChanged.emit()

    def load_instances(self, instances: list[InstanceConfig]):
        """Backward compat: load from old InstanceConfig list (no layout dict)."""
        # Clear everything
        self.clear_all()

        if not instances or not self._monitors:
            return

        # Use primary monitor (or first)
        primary = self._monitors[0]
        for mon in self._monitors:
            if mon.monitor.primary:
                primary = mon
                break

        # Determine best split mode from instance count
        n = len(instances)
        if n <= 1:
            primary.apply_preset('Full')
        elif n == 2:
            # Check if instances are side-by-side or stacked
            if len(instances) >= 2:
                if instances[0].pos_y == instances[1].pos_y:
                    primary.apply_preset('H Split')
                else:
                    primary.apply_preset('V Split')
            else:
                primary.apply_preset('H Split')
        elif n <= 4:
            primary.apply_preset('Quad')
        elif n <= 6:
            primary.apply_preset('Grid 3x2')
        else:
            primary.apply_preset('Grid 3x3')

        # Place controllers in order (skip disconnected ones)
        slots = primary.get_all_slots()
        for i, inst in enumerate(instances):
            if i >= len(slots):
                break
            if inst.controller_index is not None:
                row = self._controller_model.find_row_by_index(inst.controller_index)
                if row is None:
                    continue  # controller no longer connected
                mi = self._controller_model.index(row)
                name = self._controller_model.data(mi, self._controller_model.NameRole) or ""
                vid = self._controller_model.data(mi, self._controller_model.VidRole) or 0
                pid = self._controller_model.data(mi, self._controller_model.PidRole) or 0
                slots[i].place_controller(inst.controller_index, name, vid=vid, pid=pid,
                                          profile_id=inst.profile_id)

        self._sync_dock_from_monitors()
        self.configurationChanged.emit()

    def _on_controllers_changed(self):
        """Handle hot-plug: migrate slots whose controller reappeared at a new index."""
        model = self._controller_model

        # Collect indices that are still validly placed (not needing migration)
        valid_placed = set()
        stale_slots = []
        for mon in self._monitors:
            for slot in mon.get_placed_slots():
                if slot.controller_index is None:
                    continue
                if model.find_row_by_index(slot.controller_index) is not None:
                    valid_placed.add(slot.controller_index)
                else:
                    stale_slots.append(slot)

        # Try to migrate stale slots to new indices
        for slot in stale_slots:
            new_idx = model.find_by_identity(
                slot.controller_name,
                vid=slot._vid,
                pid=slot._pid,
                exclude=valid_placed,
            )
            # Don't steal an index that another slot already validly holds
            if new_idx is not None and new_idx not in valid_placed:
                slot.controller_index = new_idx
                from .slot_item import COLORS
                slot._color = COLORS[new_idx % len(COLORS)]
                slot.update()
                valid_placed.add(new_idx)

        # Re-sync dock placed state from current slot assignments
        self._sync_dock_from_monitors()

    def _remove_disconnected_controllers(self):
        """Remove any placed controllers that aren't currently connected."""
        for mon in self._monitors:
            for slot in mon.get_placed_slots():
                if slot.controller_index is not None:
                    row = self._controller_model.find_row_by_index(slot.controller_index)
                    if row is None:
                        slot._clear_without_signal()

    def clear_all(self):
        """Remove all controllers and reset splits to full."""
        for mon in self._monitors:
            for slot in mon.get_placed_slots():
                slot.remove_controller()
            mon.apply_preset('Full')
        self._dock.clear_all_placed()
        self.configurationChanged.emit()

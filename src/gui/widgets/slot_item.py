"""SlotItem - drop target for controllers within a BSP tree leaf."""

from pathlib import Path

from PySide6.QtCore import Qt, QRectF, QPointF, QMimeData, QByteArray, QTimer, Signal
from PySide6.QtGui import (
    QColor, QPen, QBrush, QFont, QPainter, QDrag, QPixmap, QImage,
    QFontMetrics,
)
from PySide6.QtWidgets import (QGraphicsObject, QGraphicsSceneMouseEvent, QInputDialog,
                               QMenu, QMessageBox)

COLORS = [
    QColor(76, 175, 80, 200),    # green
    QColor(33, 150, 243, 200),   # blue
    QColor(255, 152, 0, 200),    # orange
    QColor(156, 39, 176, 200),   # purple
    QColor(244, 67, 54, 200),    # red
    QColor(0, 188, 212, 200),    # cyan
    QColor(255, 235, 59, 200),   # yellow
    QColor(121, 85, 72, 200),    # brown
]

import profiles  # src/host is on sys.path (src/gui/main.py)

DRAG_THRESHOLD = 8  # pixels in view space before drag starts

# --- Gamepad icon from PNG asset, tinted per controller color ---

_GAMEPAD_BASE: QImage | None = None       # loaded once, grayscale luminance + alpha
_TINTED_CACHE: dict[int, QPixmap] = {}    # controller_index -> tinted pixmap


def _load_base_image() -> QImage:
    """Load gamepad.png and convert to grayscale luminance (preserving alpha)."""
    global _GAMEPAD_BASE
    if _GAMEPAD_BASE is not None:
        return _GAMEPAD_BASE

    png_path = Path(__file__).parent.parent / 'assets' / 'gamepad.png'
    img = QImage(str(png_path))
    if img.isNull():
        # Fallback: tiny transparent image
        img = QImage(1, 1, QImage.Format.Format_ARGB32)
        img.fill(Qt.transparent)
        _GAMEPAD_BASE = img
        return img

    img = img.convertToFormat(QImage.Format.Format_ARGB32)

    # Convert to grayscale luminance in-place (keep alpha)
    for y in range(img.height()):
        for x in range(img.width()):
            c = img.pixelColor(x, y)
            lum = int(0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue())
            img.setPixelColor(x, y, QColor(lum, lum, lum, c.alpha()))

    _GAMEPAD_BASE = img
    return img


def _get_tinted_pixmap(controller_index: int) -> QPixmap:
    """Get a gamepad pixmap tinted to the controller's color (cached)."""
    if controller_index in _TINTED_CACHE:
        return _TINTED_CACHE[controller_index]

    base = _load_base_image()
    color = COLORS[controller_index % len(COLORS)]
    tr, tg, tb = color.red(), color.green(), color.blue()

    tinted = QImage(base)
    for y in range(tinted.height()):
        for x in range(tinted.width()):
            c = tinted.pixelColor(x, y)
            lum = c.red() / 255.0  # grayscale, so r==g==b
            tinted.setPixelColor(x, y, QColor(
                int(tr * lum), int(tg * lum), int(tb * lum), c.alpha(),
            ))

    pixmap = QPixmap.fromImage(tinted)
    _TINTED_CACHE[controller_index] = pixmap
    return pixmap


def draw_gamepad_icon(painter: QPainter, cx: float, cy: float, size: float, color: QColor, controller_index: int = 0):
    """Draw the gamepad PNG centered at (cx, cy) fitting in `size`, tinted to controller color."""
    pixmap = _get_tinted_pixmap(controller_index)
    if pixmap.isNull():
        return

    # Scale to fit within `size`, keeping aspect ratio
    pw, ph = pixmap.width(), pixmap.height()
    aspect = pw / ph
    if aspect > 1:
        draw_w = size
        draw_h = size / aspect
    else:
        draw_h = size
        draw_w = size * aspect

    target = QRectF(cx - draw_w / 2, cy - draw_h / 2, draw_w, draw_h)
    painter.drawPixmap(target.toRect(), pixmap)


def make_drag_pixmap(controller_index: int, name: str) -> QPixmap:
    """Create a small pixmap for the drag cursor."""
    pixmap = QPixmap(80, 60)
    pixmap.fill(Qt.transparent)
    p = QPainter(pixmap)
    p.setRenderHint(QPainter.Antialiasing)
    draw_gamepad_icon(p, 40, 22, 48, COLORS[controller_index % len(COLORS)], controller_index)
    p.setPen(QPen(Qt.white))
    font = QFont()
    font.setPointSize(8)
    font.setBold(True)
    p.setFont(font)
    label = name if len(name) <= 12 else name[:9] + "..."
    p.drawText(QRectF(0, 44, 80, 16), Qt.AlignCenter, label)
    p.end()
    return pixmap


class SlotItem(QGraphicsObject):
    """A drop target within a BSP tree leaf."""

    # (name, label) of the owner profile for the selected game: a non-Steam
    # game's account (Game.owner, e.g. "Microsoft account"); None = the
    # Steam account. Set by MainWindow when a game is selected.
    owner_display = None
    # () -> {profile id: save line} for the selected game (save_info.py), and
    # () -> bool "a session is running". Set by MainWindow.
    profile_info = None
    session_running = None

    controllerPlaced = Signal(object)    # self
    controllerRemoved = Signal(int)      # controller_index
    expandRequested = Signal(object)     # self (merge with sibling)
    mergeRequested = Signal(object)      # self (merge empty siblings)
    indexAssignRequested = Signal(object, int)  # self, desired_index
    splitRequested = Signal(object, str)  # self, 'horizontal' or 'vertical'
    profileChanged = Signal(object)      # self

    def __init__(self, rect: QRectF, parent=None):
        super().__init__(parent)
        self._rect = QRectF(rect)
        self.node = None  # SplitNode back-reference, set by MonitorItem
        self.controller_index = None
        self.controller_name = ""
        self._vid = 0  # VID for hot-plug identity matching
        self._pid = 0  # PID for hot-plug identity matching
        self.instance_index = None  # user-visible 1-based instance index
        self.profile_id = None      # player profile; None = the instance's default
        self._color = None
        self._flash = False
        self._flash_timer = None

        self.setAcceptDrops(True)

        # Drag state
        self._drag_start_pos = None
        self._dragging = False

    def boundingRect(self):
        return self._rect

    def set_rect(self, rect: QRectF):
        self.prepareGeometryChange()
        self._rect = QRectF(rect)

    def has_controller(self) -> bool:
        return self.controller_index is not None

    def place_controller(self, index: int, name: str, instance_index: int | None = None,
                         vid: int = 0, pid: int = 0, profile_id: str | None = None):
        self.profile_id = profile_id
        self.controller_index = index
        self.controller_name = name
        self._vid = vid
        self._pid = pid
        self.instance_index = instance_index
        self._color = COLORS[index % len(COLORS)]
        self.update()
        self.controllerPlaced.emit(self)

    def remove_controller(self) -> int | None:
        idx = self.controller_index
        self.controller_index = None
        self.controller_name = ""
        self._vid = 0
        self._pid = 0
        self.instance_index = None
        self.profile_id = None
        self._color = None
        self.update()
        if idx is not None:
            self.controllerRemoved.emit(idx)
        return idx

    def _clear_without_signal(self):
        """Clear controller data without emitting controllerRemoved."""
        self.controller_index = None
        self.controller_name = ""
        self._vid = 0
        self._pid = 0
        self.instance_index = None
        self.profile_id = None
        self._color = None
        self.update()

    def profile_name(self) -> str:
        """Who plays this slot (the default follows the instance number)."""
        try:
            if self.profile_id:
                p = profiles.get(self.profile_id)
                if p:
                    return p.name
            p = profiles.peek_default(self.instance_index or 1)
            return p.name if p else f'Player {self.instance_index or 1}'
        except Exception:  # noqa: BLE001 — painting must not fail
            return ''

    def flash(self):
        """Trigger a brief visual flash."""
        self._flash = True
        self.update()
        if self._flash_timer is None:
            self._flash_timer = QTimer()
            self._flash_timer.setSingleShot(True)
            self._flash_timer.timeout.connect(self._end_flash)
        self._flash_timer.start(150)

    def _end_flash(self):
        self._flash = False
        self.update()

    def paint(self, painter: QPainter, option, widget=None):
        painter.setRenderHint(QPainter.Antialiasing)

        if self.has_controller():
            # Filled slot — subtle background tint
            bg = QColor(self._color)
            bg.setAlpha(50)
            painter.setBrush(QBrush(bg))
            painter.setPen(QPen(self._color.darker(130), 2))
            painter.drawRect(self._rect.adjusted(1, 1, -1, -1))

            # Gamepad icon
            rect = self._rect
            icon_size = min(rect.width(), rect.height()) * 0.45
            icon_cy = rect.center().y() - rect.height() * 0.08
            draw_gamepad_icon(painter, rect.center().x(), icon_cy, icon_size, self._color, self.controller_index)

            # Instance index badge (top-right corner)
            if self.instance_index is not None:
                badge_r = max(10, min(rect.width(), rect.height()) * 0.08)
                badge_cx = rect.x() + rect.width() - badge_r - 4
                badge_cy = rect.y() + badge_r + 4
                painter.setBrush(QBrush(self._color.darker(140)))
                painter.setPen(QPen(Qt.white, 1.5))
                painter.drawEllipse(QPointF(badge_cx, badge_cy), badge_r, badge_r)
                badge_font = QFont()
                badge_font.setPixelSize(max(8, int(badge_r * 1.3)))
                badge_font.setBold(True)
                painter.setFont(badge_font)
                painter.setPen(QPen(Qt.white))
                painter.drawText(
                    QRectF(badge_cx - badge_r, badge_cy - badge_r, badge_r * 2, badge_r * 2),
                    Qt.AlignCenter,
                    str(self.instance_index),
                )

            # Controller name below icon
            painter.setPen(QPen(Qt.white))
            font = QFont()
            font.setPointSize(max(8, int(min(rect.height(), rect.width()) / 12)))
            font.setBold(True)
            painter.setFont(font)

            label = self.controller_name or f"Controller {self.controller_index}"
            if len(label) > 20:
                label = label[:17] + "..."
            label = QFontMetrics(font).elidedText(label, Qt.ElideRight, int(rect.width() - 16))
            w = int(rect.width())
            h = int(rect.height())

            text_rect = QRectF(rect.x(), icon_cy + icon_size * 0.35, rect.width(), rect.height() * 0.35)
            painter.drawText(text_rect, Qt.AlignHCenter | Qt.AlignTop, f"{label}\n{w}x{h}")

            # Player profile (top-left)
            pname = self.profile_name()
            if pname:
                pfont = QFont()
                pfont.setPointSize(max(8, int(min(rect.height(), rect.width()) / 14)))
                painter.setFont(pfont)
                prect = rect.adjusted(8, 6, -8, -6)
                # Below the monitor's split button / name band when that
                # covers this slot's top edge (the top-left slot).
                overlay = getattr(self.parentItem(), 'overlay_rect', None)
                if overlay is not None:
                    band = overlay()
                    line = QRectF(prect.x(), prect.y(), prect.width(),
                                  QFontMetrics(pfont).height())
                    if line.intersects(band):
                        prect.setTop(band.bottom() + 6)
                painter.drawText(prect, Qt.AlignLeft | Qt.AlignTop,
                                 f"\N{BUST IN SILHOUETTE} {pname}")

            # Flash overlay
            if self._flash:
                painter.setBrush(QBrush(QColor(255, 255, 255, 100)))
                painter.setPen(Qt.NoPen)
                painter.drawRect(self._rect.adjusted(1, 1, -1, -1))
        else:
            # Empty slot - dashed border
            painter.setBrush(QBrush(QColor(50, 50, 50)))
            pen = QPen(QColor(120, 120, 120), 2, Qt.DashLine)
            painter.setPen(pen)
            painter.drawRect(self._rect.adjusted(2, 2, -2, -2))

            # "Drop here" hint
            painter.setPen(QPen(QColor(100, 100, 100)))
            font = QFont()
            font.setPointSize(max(7, int(min(self._rect.height(), self._rect.width()) / 14)))
            painter.setFont(font)
            painter.drawText(self._rect, Qt.AlignCenter, "Drop\ncontroller")

    # --- Mouse handling for drag-to-move ---

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent):
        if event.button() == Qt.LeftButton and self.has_controller():
            self._drag_start_pos = event.screenPos()
            event.accept()
        else:
            event.ignore()

    def mouseMoveEvent(self, event: QGraphicsSceneMouseEvent):
        if self._drag_start_pos is None or not self.has_controller():
            return

        dist = (event.screenPos() - self._drag_start_pos).manhattanLength()
        if dist < DRAG_THRESHOLD:
            return

        # Start drag — save controller info, clear slot
        idx = self.controller_index
        name = self.controller_name
        inst_idx = self.instance_index
        vid = self._vid
        pid = self._pid
        prof = self.profile_id or ''
        self._clear_without_signal()
        self._drag_start_pos = None

        drag = QDrag(self)
        mime = QMimeData()
        data = (f"{idx}\x1f{name}\x1f{inst_idx if inst_idx is not None else ''}"
                f"\x1f{vid}\x1f{pid}\x1f{prof}")
        mime.setData('application/x-janus-controller', QByteArray(data.encode()))
        drag.setMimeData(mime)
        drag.setPixmap(make_drag_pixmap(idx, name))
        drag.setHotSpot(drag.pixmap().rect().center())

        drag.exec(Qt.MoveAction | Qt.CopyAction)

        # Always emit removal — the LayoutEditor checks if the controller
        # landed on another slot before returning it to the dock.
        # This handles drops outside the window (where drag result varies by DE).
        self.controllerRemoved.emit(idx)

    def mouseReleaseEvent(self, event: QGraphicsSceneMouseEvent):
        self._drag_start_pos = None

    # --- Drop handling ---

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat('application/x-janus-controller'):
            if not self.has_controller():
                event.acceptProposedAction()
            else:
                event.ignore()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasFormat('application/x-janus-controller') and not self.has_controller():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        if event.mimeData().hasFormat('application/x-janus-controller') and not self.has_controller():
            data = event.mimeData().data('application/x-janus-controller').data().decode()
            parts = data.split('\x1f')
            idx = int(parts[0])
            name = parts[1] if len(parts) > 1 else ""
            inst_idx = int(parts[2]) if len(parts) > 2 and parts[2] else None
            vid = int(parts[3]) if len(parts) > 3 and parts[3] else 0
            pid = int(parts[4]) if len(parts) > 4 and parts[4] else 0
            prof = parts[5] if len(parts) > 5 and parts[5] else None
            self.place_controller(idx, name, instance_index=inst_idx, vid=vid, pid=pid,
                                  profile_id=prof)
            event.acceptProposedAction()
        else:
            event.ignore()

    # --- Context menu ---

    def contextMenuEvent(self, event):
        monitor = self.parentItem()

        if not self.has_controller():
            # Empty slot — offer split and merge
            menu = QMenu()

            menu.addAction("Split Left / Right").setData(('split', 'horizontal'))
            menu.addAction("Split Top / Bottom").setData(('split', 'vertical'))

            if monitor and hasattr(monitor, 'can_merge') and monitor.can_merge(self):
                menu.addSeparator()
                menu.addAction("Merge").setData(('merge',))

            chosen = menu.exec(event.screenPos())
            if chosen:
                action_data = chosen.data()
                if isinstance(action_data, tuple):
                    if action_data[0] == 'split':
                        self.splitRequested.emit(self, action_data[1])
                    elif action_data[0] == 'merge':
                        self.mergeRequested.emit(self)
            return

        # Placed slot
        menu = QMenu()

        # Expand (merge with empty sibling)
        if monitor and hasattr(monitor, 'can_expand') and monitor.can_expand(self):
            menu.addAction("Expand").setData('expand')
            menu.addSeparator()

        # Assign Index submenu
        total_placed = self._count_total_placed()
        if total_placed > 1:
            idx_menu = menu.addMenu("Assign Index")
            for n in range(1, total_placed + 1):
                action = idx_menu.addAction(str(n))
                action.setData(('assign_index', n))
                if n == self.instance_index:
                    action.setEnabled(False)
                    action.setCheckable(True)
                    action.setChecked(True)
            menu.addSeparator()

        # Player profile submenu: who plays here (whose saves / name).
        prof_menu = menu.addMenu("Profile")
        holders = self._profile_holders()
        taken = set(holders)
        current = self.profile_id or getattr(
            profiles.peek_default(self.instance_index or 1), 'id', None)
        try:
            infos = SlotItem.profile_info() if SlotItem.profile_info else {}
        except Exception:  # noqa: BLE001 — decoration only
            infos = {}
        all_profiles = profiles.load()
        for prof in all_profiles:
            if prof.owner:
                name, kind = SlotItem.owner_display or (prof.name, "Steam account")
                label = f"{name}  ({kind})"
            else:
                label = prof.name
            if prof.id in holders:
                # Taken by another slot: choosing it swaps the two.
                label += f"  [P{holders[prof.id].instance_index or '?'} — swap]"
            if infos.get(prof.id):
                label += f"   —   {infos[prof.id]}"
            action = prof_menu.addAction(label)
            action.setData(('profile', prof.id))
            action.setCheckable(True)
            action.setChecked(prof.id == current)
        prof_menu.addSeparator()
        prof_menu.addAction("New profile…").setData(('profile_new',))
        deletable = [p for p in all_profiles if not p.owner]
        if deletable:
            del_menu = prof_menu.addMenu("Delete profile")
            for prof in deletable:
                a = del_menu.addAction(prof.name + "…")
                a.setData(('profile_delete', prof.id))
                if prof.id in taken:
                    a.setEnabled(False)
        menu.addSeparator()

        menu.addAction("Remove Controller").setData('remove')

        chosen = menu.exec(event.screenPos())
        if chosen:
            action_data = chosen.data()
            if isinstance(action_data, tuple) and action_data[0] in ('profile', 'profile_new'):
                self._choose_profile(action_data)
            elif isinstance(action_data, tuple) and action_data[0] == 'profile_delete':
                self._delete_profile(action_data[1])
            elif action_data == 'remove':
                self.remove_controller()
            elif action_data == 'expand':
                self.expandRequested.emit(self)
            elif isinstance(action_data, tuple) and action_data[0] == 'assign_index':
                self.indexAssignRequested.emit(self, action_data[1])

    def _profile_holders(self) -> dict:
        """Profile id → the other placed slot playing as it."""
        scene = self.scene()
        out = {}
        for item in (scene.items() if scene else []):
            if isinstance(item, SlotItem) and item is not self and item.has_controller():
                try:
                    pid = item.profile_id or getattr(
                        profiles.peek_default(item.instance_index or 1), 'id', None)
                except Exception:  # noqa: BLE001
                    continue
                if pid:
                    out[pid] = item
        return out

    def _choose_profile(self, action_data):
        if action_data[0] == 'profile':
            other = self._profile_holders().get(action_data[1])
            if other is not None:
                # Swap: the other slot takes the profile this one had.
                try:
                    mine = self.profile_id or profiles.default_for_instance(
                        self.instance_index or 1).id
                except Exception as e:  # noqa: BLE001
                    QMessageBox.warning(None, "Profile", str(e))
                    return
                other.profile_id = mine
                other.update()
                other.profileChanged.emit(other)
            self.profile_id = action_data[1]
        else:
            name, ok = QInputDialog.getText(None, "New profile", "Player name:")
            if not ok or not name.strip():
                return
            try:
                self.profile_id = profiles.create(name).id
            except ValueError as e:
                QMessageBox.warning(None, "New profile", str(e))
                return
        self.update()
        self.profileChanged.emit(self)

    def _delete_profile(self, profile_id):
        """Delete a profile and all its saves, after the user types "delete"."""
        import save_info
        prof = profiles.get(profile_id)
        if prof is None or prof.owner:
            return
        title = f"Delete {prof.name}"
        if SlotItem.session_running and SlotItem.session_running():
            QMessageBox.warning(None, title, "Profiles can't be deleted while games are running.")
            return
        stats = save_info.total_stats(profile_id)
        what = (f"{save_info.human_size(stats[0])} of saves in every game"
                if stats and stats[0] else "no saves")
        text, ok = QInputDialog.getText(
            None, title,
            f"{prof.name} is removed with everything Janus keeps for them: {what}.\n"
            f"This can't be undone.\n\nType delete to confirm:")
        if not ok:
            return
        if text.strip().lower() != 'delete':
            QMessageBox.information(None, title, 'Nothing deleted: type "delete" to confirm.')
            return
        try:
            profiles.delete(profile_id, delete_data=True)
        except (ValueError, KeyError, OSError) as e:
            QMessageBox.warning(None, title, str(e))
            return
        if self.profile_id == profile_id:
            self.profile_id = None
        self.update()
        self.profileChanged.emit(self)

    def _count_total_placed(self) -> int:
        """Count total placed controllers across all monitors in the scene."""
        scene = self.scene()
        if not scene:
            return 0
        count = 0
        for item in scene.items():
            if isinstance(item, SlotItem) and item.has_controller():
                count += 1
        return count

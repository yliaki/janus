"""ControllerDock - horizontal row of draggable controller icons."""

from PySide6.QtCore import Qt, QMimeData, QByteArray, QRectF, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QDrag, QFont, QFontMetrics, QPixmap
from PySide6.QtWidgets import QWidget, QHBoxLayout, QLabel

from ..models.controller_model import ControllerModel
from ..services.joystick_monitor import JoystickMonitor
from controllers import EVDEV_INDEX_BASE, event_node
from .slot_item import draw_gamepad_icon, make_drag_pixmap, COLORS


FLASH_DURATION_MS = 150


class ControllerPill(QWidget):
    """A draggable pill representing one controller, with gamepad icon."""

    def __init__(self, controller_index: int, name: str, vid: int = 0, pid: int = 0, parent=None):
        super().__init__(parent)
        self.controller_index = controller_index
        self.controller_name = name
        self.vid = vid
        self.pid = pid
        self._placed = False
        self._flash = False
        self._color = COLORS[controller_index % len(COLORS)]
        self.setFixedHeight(44)
        self.setMinimumWidth(100)
        self.setCursor(Qt.OpenHandCursor)
        self._update_size()

        self._flash_timer = QTimer(self)
        self._flash_timer.setSingleShot(True)
        self._flash_timer.timeout.connect(self._end_flash)

    def _update_size(self):
        fm = QFontMetrics(self.font())
        label = self._display_text()
        # Icon (36px) + gap (6px) + text + padding
        w = 36 + 6 + fm.horizontalAdvance(label) + 16
        self.setFixedWidth(max(100, min(220, w)))

    def _display_text(self) -> str:
        name = self.controller_name
        if len(name) > 18:
            name = name[:15] + "..."
        return f"{name} ({self.controller_index})"

    @property
    def placed(self) -> bool:
        return self._placed

    @placed.setter
    def placed(self, value: bool):
        self._placed = value
        self.setCursor(Qt.ForbiddenCursor if value else Qt.OpenHandCursor)
        self.update()

    def flash(self):
        """Trigger a brief visual flash."""
        self._flash = True
        self.update()
        self._flash_timer.start(FLASH_DURATION_MS)

    def _end_flash(self):
        self._flash = False
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        rect = self.rect().adjusted(1, 1, -1, -1)
        color = self._color

        if self._placed:
            # Grayed out
            bg_color = QColor(80, 80, 80)
            painter.setBrush(bg_color)
            painter.setPen(QColor(60, 60, 60))
        else:
            painter.setBrush(color)
            painter.setPen(color.darker(130))

        painter.drawRoundedRect(rect, 8, 8)

        # Flash overlay
        if self._flash:
            painter.setBrush(QColor(255, 255, 255, 100))
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(rect, 8, 8)

        # Gamepad icon on the left
        icon_size = 30
        icon_cx = rect.x() + 8 + icon_size / 2
        icon_cy = rect.center().y()

        if self._placed:
            # Draw grayed-out icon: reduce opacity
            painter.setOpacity(0.35)
        draw_gamepad_icon(painter, icon_cx, icon_cy, icon_size, color, self.controller_index)
        painter.setOpacity(1.0)

        # Text to the right of icon
        text_color = QColor(255, 255, 255) if not self._placed else QColor(120, 120, 120)
        painter.setPen(text_color)
        font = QFont()
        font.setPointSize(9)
        font.setBold(True)
        painter.setFont(font)
        text_x = rect.x() + 8 + icon_size + 6
        text_rect = QRectF(text_x, rect.y(), rect.right() - text_x - 4, rect.height())
        painter.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, self._display_text())

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and not self._placed:
            self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, event):
        if not (event.buttons() & Qt.LeftButton) or self._placed:
            return

        drag = QDrag(self)
        mime = QMimeData()
        data = f"{self.controller_index}\x1f{self.controller_name}\x1f\x1f{self.vid}\x1f{self.pid}"
        mime.setData('application/x-janus-controller', QByteArray(data.encode()))
        drag.setMimeData(mime)
        drag.setPixmap(make_drag_pixmap(self.controller_index, self.controller_name))
        drag.setHotSpot(drag.pixmap().rect().center())

        result = drag.exec(Qt.CopyAction)
        self.setCursor(Qt.OpenHandCursor if not self._placed else Qt.ForbiddenCursor)

    def mouseReleaseEvent(self, event):
        if not self._placed:
            self.setCursor(Qt.OpenHandCursor)


class ControllerDock(QWidget):
    """Horizontal row of draggable controller pills with input flash."""

    controllerPlaced = Signal(int)    # controller_index
    controllerReturned = Signal(int)  # controller_index
    buttonPressed = Signal(int)       # controller_index (forwarded from JoystickMonitor)

    def __init__(self, controller_model: ControllerModel, parent=None):
        super().__init__(parent)
        self._model = controller_model
        self._pills: dict[int, ControllerPill] = {}  # controller_index -> pill
        # Persistent set of placed indices — survives pill rebuilds (e.g. controller
        # temporarily disappearing during hot-plug).  Updated by mark_placed/mark_returned.
        self._placed_indices: set[int] = set()

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(6)

        self._label = QLabel("Controllers:")
        self._label.setStyleSheet("color: #aaa; font-weight: bold;")
        layout.addWidget(self._label)

        self._pill_layout = QHBoxLayout()
        self._pill_layout.setSpacing(4)
        layout.addLayout(self._pill_layout)
        layout.addStretch()

        # Joystick input monitor
        self._joy_monitor = JoystickMonitor(self)
        self._joy_monitor.buttonPressed.connect(self._on_button_pressed)

        self._model.modelReset.connect(self._rebuild_pills)
        self._rebuild_pills()

    def _rebuild_pills(self):
        # Merge placed state: pills + persistent set
        placed_indices = set(self._placed_indices)
        placed_indices.update(idx for idx, pill in self._pills.items() if pill.placed)

        # Clear old pills
        while self._pill_layout.count():
            item = self._pill_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._pills.clear()

        # Create new pills
        js_devices = []  # (index, device_path) for joystick monitor
        for row in range(self._model.rowCount()):
            idx = self._model.get_controller_index(row)
            mi = self._model.index(row)
            name = self._model.data(mi, self._model.NameRole) or ""
            vid = self._model.data(mi, self._model.VidRole) or 0
            pid = self._model.data(mi, self._model.PidRole) or 0
            if idx is None:
                continue
            pill = ControllerPill(idx, name, vid=vid, pid=pid, parent=self)
            if idx in placed_indices:
                pill.placed = True
            self._pills[idx] = pill
            self._pill_layout.addWidget(pill)
            js_devices.append((idx, f"/dev/input/js{idx}" if idx < EVDEV_INDEX_BASE
                               else event_node(idx)))

        # Update joystick monitor with current controllers
        self._joy_monitor.update_controllers(js_devices)

    def _on_button_pressed(self, controller_index: int):
        """Flash the pill for this controller and forward signal."""
        if controller_index in self._pills:
            self._pills[controller_index].flash()
        self.buttonPressed.emit(controller_index)

    def mark_placed(self, controller_index: int):
        """Mark a controller as placed (grayed out in dock)."""
        self._placed_indices.add(controller_index)
        if controller_index in self._pills:
            self._pills[controller_index].placed = True

    def mark_returned(self, controller_index: int):
        """Mark a controller as returned (re-enabled in dock)."""
        self._placed_indices.discard(controller_index)
        if controller_index in self._pills:
            self._pills[controller_index].placed = False

    def get_placed_indices(self) -> set[int]:
        """Return set of controller indices currently placed on slots."""
        return set(self._placed_indices)

    def clear_all_placed(self):
        """Return all controllers to the dock."""
        self._placed_indices.clear()
        for pill in self._pills.values():
            pill.placed = False

    def sync_placed(self, placed_indices: set[int]):
        """Replace the placed set entirely (called after layout migration)."""
        self._placed_indices = set(placed_indices)
        for idx, pill in self._pills.items():
            pill.placed = idx in placed_indices

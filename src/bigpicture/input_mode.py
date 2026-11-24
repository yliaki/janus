"""Mouse/keyboard vs gamepad mode for the Big Picture UI.

`mouse` turns true when the mouse really moves, clicks or turns its wheel (an
application-wide event filter, so it sees events every item swallows) or a
key is pressed (QML sets it), and false after a pad button. Wheel events
never reach the views (nothing scrolls freely, lists scroll by click-and-
drag); each notch is reported as wheel(+1 down / -1 up) instead, which the
games grid uses to step its selection one row. QML shows
clickable buttons instead of controller hints while it's true. press() is
what those buttons call: Main.qml dispatches it like a keyboard press.
"""

from PySide6.QtCore import QEvent, QObject, Qt, Property, Signal, Slot
from PySide6.QtGui import QCursor, QGuiApplication


class InputMode(QObject):
    mouseChanged = Signal()
    pressed = Signal(str)
    wheel = Signal(int)          # notches: +1 down, -1 up

    def __init__(self, parent=None):
        super().__init__(parent)
        self._mouse = False
        self._last = None
        self._wheel = 0              # angle not yet a whole notch (touchpads)
        # No cursor while a pad drives (it would sit in the middle of the TV).
        QGuiApplication.setOverrideCursor(QCursor(Qt.BlankCursor))

    def _get_mouse(self):
        return self._mouse

    def _set_mouse(self, v):
        if bool(v) != self._mouse:
            self._mouse = bool(v)
            if self._mouse:
                QGuiApplication.restoreOverrideCursor()
            else:
                QGuiApplication.setOverrideCursor(QCursor(Qt.BlankCursor))
            self.mouseChanged.emit()

    mouse = Property(bool, _get_mouse, _set_mouse, notify=mouseChanged)

    @Slot(str)
    def press(self, btn):
        self.pressed.emit(btn)

    def eventFilter(self, obj, ev):
        t = ev.type()
        if t == QEvent.MouseMove:
            # A window appearing under a resting cursor gets a move event
            # too: only real movement counts.
            p = ev.globalPosition()
            if self._last is not None and \
                    abs(p.x() - self._last.x()) + abs(p.y() - self._last.y()) > 6:
                self._set_mouse(True)
            self._last = p
        elif t == QEvent.MouseButtonPress:
            self._set_mouse(True)
        elif t == QEvent.Wheel:
            # Never scroll a view directly; report whole notches (120).
            self._set_mouse(True)
            self._wheel += ev.angleDelta().y()
            while abs(self._wheel) >= 120:
                step = -1 if self._wheel > 0 else 1
                self._wheel += 120 * step
                self.wheel.emit(step)
            return True
        return False

"""Monitor joystick button presses via /dev/input/js* (or the event node of a
pad without a js node) for controller identification."""

import os
import struct

from PySide6.QtCore import QObject, QSocketNotifier, Signal

JS_EVENT_SIZE = 8       # struct js_event: uint32 time, int16 value, uint8 type, uint8 number
JS_EVENT_BUTTON = 0x01
JS_EVENT_INIT = 0x80
INPUT_EVENT = struct.Struct('llHHi')   # struct input_event: timeval, type, code, value
EV_KEY = 0x01


class JoystickMonitor(QObject):
    """Monitors /dev/input/js* for button presses, emits controller index on press."""

    buttonPressed = Signal(int)  # controller_index

    def __init__(self, parent=None):
        super().__init__(parent)
        self._notifiers: dict[int, tuple[int, QSocketNotifier]] = {}  # index -> (fd, notifier)
        self._evdev: set[int] = set()   # indices read from an event node

    def update_controllers(self, controllers: list[tuple[int, str]]):
        """Start monitoring new controllers, stop removed ones.

        Args:
            controllers: list of (index, device_path) tuples.
        """
        new_indices = {idx for idx, _path in controllers}
        old_indices = set(self._notifiers.keys())

        # Close removed
        for idx in old_indices - new_indices:
            self._close(idx)

        # Open new
        for idx, device_path in controllers:
            if idx not in self._notifiers:
                self._open(idx, device_path)

    def _open(self, index: int, device_path: str):
        try:
            fd = os.open(device_path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            return

        if '/event' in device_path:
            self._evdev.add(index)
        notifier = QSocketNotifier(fd, QSocketNotifier.Type.Read, self)
        notifier.activated.connect(lambda *_args, idx=index: self._on_readable(idx))
        self._notifiers[index] = (fd, notifier)

    def _on_readable(self, index: int):
        if index not in self._notifiers:
            return
        fd, _notifier = self._notifiers[index]
        if index in self._evdev:
            self._read_evdev(index, fd)
            return
        try:
            while True:
                data = os.read(fd, JS_EVENT_SIZE)
                if len(data) < JS_EVENT_SIZE:
                    break
                _time, value, type_, _number = struct.unpack('IhBB', data)
                # Button press only (not release, not initial state report)
                if (type_ & ~JS_EVENT_INIT) == JS_EVENT_BUTTON and value == 1:
                    if not (type_ & JS_EVENT_INIT):
                        self.buttonPressed.emit(index)
        except BlockingIOError:
            pass  # No more data available
        except OSError:
            self._close(index)

    def _read_evdev(self, index: int, fd: int):
        try:
            while True:
                data = os.read(fd, INPUT_EVENT.size * 32)
                if not data:
                    break
                for off in range(0, len(data) - INPUT_EVENT.size + 1, INPUT_EVENT.size):
                    _s, _us, type_, _code, value = INPUT_EVENT.unpack_from(data, off)
                    if type_ == EV_KEY and value == 1:
                        self.buttonPressed.emit(index)
        except BlockingIOError:
            pass
        except OSError:
            self._close(index)

    def _close(self, index: int):
        self._evdev.discard(index)
        if index in self._notifiers:
            fd, notifier = self._notifiers.pop(index)
            notifier.setEnabled(False)
            notifier.deleteLater()
            try:
                os.close(fd)
            except OSError:
                pass

    def stop_all(self):
        for idx in list(self._notifiers.keys()):
            self._close(idx)

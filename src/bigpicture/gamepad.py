"""Gamepad navigation input for the Big Picture UI.

Reads controllers through SDL2's GameController API (ctypes against the
system libSDL2, so no python3-sdl2 dependency). SDL's mapping database gives
every pad the same A/B/X/Y/D-pad layout, which the raw /dev/input/js* API
does not.

Each SDL device is tied back to its kernel js index (via the evdev node SDL
opened and sysfs), so the UI can bind a player to the same identity the rest
of Janus uses. Pads that list_controllers() doesn't report (Steam's virtual
28de:11ff pads) are ignored: when the launcher runs under Steam, those mirror
a physical pad and would otherwise navigate twice.

SDL HIDAPI is disabled so SDL stays on evdev: it never touches hidraw, where
Steam owns the pads.
"""

import ctypes
import ctypes.util
import os
import subprocess
import sys
import time
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal, Property

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'host'))
from controllers import hidraw_node, index_for_device_node, list_controllers

SDL_INIT_GAMECONTROLLER = 0x00002000
SDL_IGNORE = 0

# SDL_GameControllerButton
BUTTONS = {
    0: 'a', 1: 'b', 2: 'x', 3: 'y', 4: 'back', 5: 'guide', 6: 'start',
    7: 'l3', 8: 'r3', 9: 'lb', 10: 'rb',
    11: 'up', 12: 'down', 13: 'left', 14: 'right',
}
DIRECTIONS = ('up', 'down', 'left', 'right')
AXIS_LEFTX, AXIS_LEFTY = 0, 1
AXIS_TRIGGERLEFT, AXIS_TRIGGERRIGHT = 4, 5
STICK_THRESHOLD = 16000

REPEAT_DELAY = 0.40   # s before a held direction starts repeating
REPEAT_RATE = 0.11    # s between repeats
# Holding Start this long on a pad that isn't playing joins the running
# session (dynamic players), whether or not the launcher has the focus.
JOIN_HOLD_SECONDS = 1.5


def _preload_sdl3() -> str | None:
    """Load libSDL3 ahead of libSDL2; returns why it failed, None if loaded.

    On Arch (Fedora, …) libSDL2 is sdl2-compat, SDL2 on top of SDL3: it
    dlopens libSDL3.so.0 as it loads and, if that fails, prints "Failed
    loading SDL3 library." and exits the whole process — no exception to
    catch. Already loaded, its dlopen just finds it."""
    errors = []
    for name in (ctypes.util.find_library('SDL3'), 'libSDL3.so.0'):
        if not name:
            continue
        try:
            ctypes.CDLL(name, mode=ctypes.RTLD_GLOBAL)
            return None
        except OSError as e:
            errors.append(str(e))
    return '; '.join(errors) or 'libSDL3 not found'


class _GUID(ctypes.Structure):
    _fields_ = [('data', ctypes.c_uint8 * 16)]


def _load_sdl():
    name = ctypes.util.find_library('SDL2-2.0') or 'libSDL2-2.0.so.0'
    sdl3_error = _preload_sdl3()
    if sdl3_error:
        # Real SDL2 doesn't need SDL3; sdl2-compat would kill us. Find out
        # which in a throwaway process.
        probe = subprocess.run(
            [sys.executable, '-I', '-c',
             'import ctypes, sys; ctypes.CDLL(sys.argv[1])', name],
            capture_output=True, text=True)
        if probe.returncode != 0:
            raise OSError(f'{name} needs SDL3, which failed to load '
                          f'({sdl3_error}): {probe.stderr.strip()}')
    lib = ctypes.CDLL(name)
    sig = {
        'SDL_SetHint': (ctypes.c_int, [ctypes.c_char_p, ctypes.c_char_p]),
        'SDL_Init': (ctypes.c_int, [ctypes.c_uint32]),
        'SDL_Quit': (None, []),
        'SDL_GetError': (ctypes.c_char_p, []),
        'SDL_NumJoysticks': (ctypes.c_int, []),
        'SDL_IsGameController': (ctypes.c_int, [ctypes.c_int]),
        'SDL_JoystickGetDeviceInstanceID': (ctypes.c_int32, [ctypes.c_int]),
        'SDL_JoystickPathForIndex': (ctypes.c_char_p, [ctypes.c_int]),
        'SDL_GameControllerOpen': (ctypes.c_void_p, [ctypes.c_int]),
        'SDL_GameControllerClose': (None, [ctypes.c_void_p]),
        'SDL_GameControllerGetAttached': (ctypes.c_int, [ctypes.c_void_p]),
        'SDL_GameControllerGetButton': (ctypes.c_uint8, [ctypes.c_void_p, ctypes.c_int]),
        'SDL_GameControllerGetAxis': (ctypes.c_int16, [ctypes.c_void_p, ctypes.c_int]),
        'SDL_GameControllerUpdate': (None, []),
        'SDL_GameControllerEventState': (ctypes.c_int, [ctypes.c_int]),
        'SDL_JoystickEventState': (ctypes.c_int, [ctypes.c_int]),
        'SDL_FlushEvents': (None, [ctypes.c_uint32, ctypes.c_uint32]),
        # Raw joystick access (the controller setup wizard, setup_wizard.py).
        'SDL_JoystickNameForIndex': (ctypes.c_char_p, [ctypes.c_int]),
        'SDL_JoystickGetDeviceVendor': (ctypes.c_uint16, [ctypes.c_int]),
        'SDL_JoystickGetDeviceProduct': (ctypes.c_uint16, [ctypes.c_int]),
        'SDL_JoystickGetDeviceGUID': (_GUID, [ctypes.c_int]),
        'SDL_JoystickGetGUIDString': (None, [_GUID, ctypes.c_char_p, ctypes.c_int]),
        'SDL_JoystickOpen': (ctypes.c_void_p, [ctypes.c_int]),
        'SDL_JoystickClose': (None, [ctypes.c_void_p]),
        'SDL_JoystickGetAttached': (ctypes.c_int, [ctypes.c_void_p]),
        'SDL_JoystickNumButtons': (ctypes.c_int, [ctypes.c_void_p]),
        'SDL_JoystickNumAxes': (ctypes.c_int, [ctypes.c_void_p]),
        'SDL_JoystickNumHats': (ctypes.c_int, [ctypes.c_void_p]),
        'SDL_JoystickGetButton': (ctypes.c_uint8, [ctypes.c_void_p, ctypes.c_int]),
        'SDL_JoystickGetAxis': (ctypes.c_int16, [ctypes.c_void_p, ctypes.c_int]),
        'SDL_JoystickGetHat': (ctypes.c_uint8, [ctypes.c_void_p, ctypes.c_int]),
        'SDL_JoystickUpdate': (None, []),
        'SDL_GameControllerAddMapping': (ctypes.c_int, [ctypes.c_char_p]),
        'SDL_JoystickGetGUIDFromString': (_GUID, [ctypes.c_char_p]),
        'SDL_GameControllerMappingForGUID': (ctypes.c_void_p, [_GUID]),
        'SDL_free': (None, [ctypes.c_void_p]),
    }
    for fn, (res, args) in sig.items():
        f = getattr(lib, fn)
        f.restype = res
        f.argtypes = args
    return lib


NINTENDO_VID = 0x057E
# Nintendo Switch pads, read passively from hidraw (see _HidPad). Simple 0x3f
# report: byte1 A B X Y L R ZL ZR (bit 0..7; face buttons verified one at a
# time on a real Pro Controller, 2026-10-25), byte2 - + LS RS Home Capture,
# byte3 hat (0 up, clockwise; 8/15 neutral). By label,
# like SDL's Switch driver: Nintendo A is Big Picture's 'a' (confirm).
_SIMPLE_BTNS = {0: 'a', 1: 'b', 2: 'x', 3: 'y', 4: 'lb', 5: 'rb', 6: 'lt', 7: 'rt',
                8: 'back', 9: 'start', 10: 'l3', 11: 'r3', 12: 'guide'}
_SIMPLE_HAT = {0: {'up'}, 1: {'up', 'right'}, 2: {'right'}, 3: {'down', 'right'},
               4: {'down'}, 5: {'down', 'left'}, 6: {'left'}, 7: {'up', 'left'}}
_FULL_BTNS = {  # (byte, mask) of the full 0x30 report
    (3, 0x01): 'y', (3, 0x02): 'x', (3, 0x04): 'b', (3, 0x08): 'a',
    (3, 0x40): 'rb', (3, 0x80): 'rt', (4, 0x01): 'back', (4, 0x02): 'start',
    (4, 0x04): 'r3', (4, 0x08): 'l3', (4, 0x10): 'guide',
    (5, 0x01): 'down', (5, 0x02): 'up', (5, 0x04): 'right', (5, 0x08): 'left',
    (5, 0x40): 'lb', (5, 0x80): 'lt'}


class _HidPad:
    """A Nintendo Switch pad read from its hidraw node, read-only.

    A Pro Controller can be stuck in simple HID mode (0x3f reports), which
    hid-nintendo ignores, so its event node stays silent. SDL's HIDAPI Switch
    driver reads it but takes the device over (handshake, subcommands) and
    Steam then drops the pad (no Steam virtual pad -> nothing in games). Only
    reading hidraw disturbs nobody; Steam reads it in parallel. Parses both
    the simple 0x3f and the full 0x30 report. NOTES.md "Gamepads without a
    js node"."""

    def __init__(self, path, js_index):
        self.path = path
        self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        self.js_index = js_index
        self.state: set[str] = set()
        self.buttons: set[str] = set()
        self.dir_since: dict[str, float] = {}
        self.start_since = None

    def read(self) -> bool:
        """Consume pending reports; False when the device is gone."""
        try:
            while True:
                r = os.read(self.fd, 64)
                if not r:
                    break
                self.state = self._parse(r, self.state)
        except BlockingIOError:
            pass
        except OSError:
            return False
        return True

    @staticmethod
    def _stick(x: int, y_down: int, held: set):
        if x < -0.5:
            held.add('left')
        elif x > 0.5:
            held.add('right')
        if y_down < -0.5:
            held.add('up')
        elif y_down > 0.5:
            held.add('down')

    def _parse(self, r: bytes, old: set) -> set:
        held = set()
        if r[0] == 0x3F and len(r) >= 12:
            bits = r[1] | (r[2] << 8)
            held = {n for b, n in _SIMPLE_BTNS.items() if bits & (1 << b)}
            held |= _SIMPLE_HAT.get(r[3], set())
            lx = int.from_bytes(r[4:6], 'little')
            ly = int.from_bytes(r[6:8], 'little')
            self._stick((lx - 0x8000) / 0x8000, (ly - 0x8000) / 0x8000, held)
        elif r[0] == 0x30 and len(r) >= 9:
            held = {n for (i, m), n in _FULL_BTNS.items() if r[i] & m}
            lx = r[6] | ((r[7] & 0x0F) << 8)
            ly = (r[7] >> 4) | (r[8] << 4)
            self._stick((lx - 2048) / 1600, (2048 - ly) / 1600, held)
        else:
            return old          # subcommand replies etc.
        return held

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass


class _Pad:
    def __init__(self, handle, instance_id, js_index):
        self.handle = handle
        self.instance_id = instance_id
        self.js_index = js_index
        self.buttons: set[str] = set()
        self.dir_since: dict[str, float] = {}   # held direction -> next fire time
        self.start_since = None                 # Start held since (join)


class GamepadInput(QObject):
    """Polls SDL at 60 Hz and emits one signal per button press.

    ``button(name, js_index)`` fires on press; directions also auto-repeat
    while held (D-pad or left stick). Nothing is emitted while ``enabled`` is
    false, so a game played with the same pads can't drive the launcher.
    """

    button = Signal(str, int)
    # Start held JOIN_HOLD_SECONDS (js index), focus or not: a free pad asks
    # to join the running session. Pads Janus input holds send nothing here
    # (grabbed), Steam's and Janus's virtual pads are ignored.
    startHeld = Signal(int)
    # Start pressed while the launcher isn't the active window (its input is
    # off then): the backend brings it to the front unless games are running.
    startInactive = Signal(int)
    enabledChanged = Signal()
    unknownPadsChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._enabled = True
        self._pads: dict[int, _Pad] = {}      # SDL instance id -> pad
        self._hid: dict[int, _HidPad] = {}    # controller index -> Switch pad
        self._known_js: set[int] = set()
        self._last_scan = 0.0
        # Pads SDL has no mapping for ([{js, name}]); the setup wizard's pad
        # (its presses are recorded, not navigation).
        self._unknown: list = []
        self.ignore_js: set[int] = set()
        # GUID -> the mapping SDL had before Janus's own replaced it (its
        # database's or Steam's), to put back when that mapping is removed;
        # GUIDs whose Janus mapping was removed and SDL had none before
        # (SDL can't forget a mapping: they're treated as unmapped here).
        self._before: dict[str, str] = {}
        self._unmapped: set[str] = set()
        self._janus: set[str] = set()      # GUIDs with a Janus mapping in SDL
        try:
            self._sdl = _load_sdl()
        except (OSError, AttributeError) as e:
            print(f'[bigpicture] SDL2 unavailable, gamepad input off: {e}',
                  flush=True)
            self._sdl = None
            return
        self._sdl.SDL_SetHint(b'SDL_JOYSTICK_HIDAPI', b'0')
        # SDL would turn SIGTERM/SIGINT into an SDL_QUIT event that nobody
        # reads, so Steam's "Exit game" (SIGTERM) couldn't close the launcher.
        self._sdl.SDL_SetHint(b'SDL_NO_SIGNAL_HANDLERS', b'1')
        if self._sdl.SDL_Init(SDL_INIT_GAMECONTROLLER) != 0:
            print('[bigpicture] SDL_Init failed: '
                  f'{self._sdl.SDL_GetError().decode()}', flush=True)
            self._sdl = None
            return
        # Janus's own mappings (pad_mappings.py) on top of SDL's database.
        try:
            import pad_mappings
            self._janus = set(pad_mappings.janus_mappings())
            for guid in self._janus:
                before = self._mapping_for(guid)
                if before:
                    self._before[guid] = before
            pad_mappings.apply_to_sdl(self._sdl)
        except Exception as e:
            print(f'[bigpicture] controller mappings: {e}', flush=True)
        # Polled, not event-driven: keep SDL's event queue empty.
        self._sdl.SDL_GameControllerEventState(SDL_IGNORE)
        self._sdl.SDL_JoystickEventState(SDL_IGNORE)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)
        self._timer.start(16)

    def _get_enabled(self):
        return self._enabled

    def _set_enabled(self, value):
        value = bool(value)
        if value == self._enabled:
            return
        self._enabled = value
        # Swallow whatever is held right now, so the button that brought
        # focus back doesn't also fire as a press.
        for hp in self._hid.values():
            hp.buttons = set(hp.state)
        for pad in self._pads.values():
            pad.buttons = self._held(pad)
            pad.dir_since = {d: float('inf') for d in pad.buttons
                             if d in DIRECTIONS}
        self.enabledChanged.emit()

    enabled = Property(bool, _get_enabled, _set_enabled, notify=enabledChanged)

    def _rescan(self):
        sdl = self._sdl
        controllers = list_controllers()
        self._known_js = {c.index for c in controllers}
        # Nintendo Switch pads: read from hidraw (_HidPad), not via SDL.
        switch = {c.index for c in controllers if c.vendor_id == NINTENDO_VID}
        for idx in list(self._hid):
            if idx not in switch:
                self._hid.pop(idx).close()
        for idx in switch - set(self._hid):
            node = hidraw_node(idx)
            try:
                pad = _HidPad(node, idx) if node else None
            except OSError as e:
                print(f'[bigpicture] {node}: {e}', flush=True)
                pad = None
            if pad:
                pad.read()
                pad.buttons = set(pad.state)          # no press on connect
                self._hid[idx] = pad
        self._known_js -= set(self._hid)              # SDL's view of them: ignored
        present = set()
        unknown = []
        for i in range(sdl.SDL_NumJoysticks()):
            iid = sdl.SDL_JoystickGetDeviceInstanceID(i)
            present.add(iid)
            mapped = sdl.SDL_IsGameController(i) and not (
                self._unmapped and self.guid_at(i) in self._unmapped)
            if not mapped:
                path = sdl.SDL_JoystickPathForIndex(i)
                js = index_for_device_node(path.decode() if path else '')
                if js in self._known_js:
                    unknown.append({'js': js, 'name': (sdl.SDL_JoystickNameForIndex(i)
                                                       or b'?').decode(errors='replace')})
            if iid in self._pads or not mapped:
                continue
            path = sdl.SDL_JoystickPathForIndex(i)
            # The pad's controller index: its js number, or the event-only
            # index (controllers.py) for pads the kernel gives no js node.
            js = index_for_device_node(path.decode() if path else '')
            handle = sdl.SDL_GameControllerOpen(i)
            if handle:
                pad = _Pad(handle, iid, js)
                pad.buttons = self._held(pad)    # no press on connect
                self._pads[iid] = pad
        for iid in list(self._pads):
            pad = self._pads[iid]
            if iid not in present or not sdl.SDL_GameControllerGetAttached(pad.handle):
                sdl.SDL_GameControllerClose(pad.handle)
                del self._pads[iid]
        if unknown != self._unknown:
            self._unknown = unknown
            self.unknownPadsChanged.emit()

    @Property('QVariantList', notify=unknownPadsChanged)
    def unknownPads(self):
        """Connected pads SDL has no mapping for: [{js, name}]. Janus input
        can't use them until they're set up (setup_wizard.py)."""
        return list(self._unknown)

    # --- raw access for the controller setup wizard -------------------------

    @property
    def sdl(self):
        return self._sdl

    def device_index(self, js: int) -> int:
        """SDL's device index of controller *js* (controllers.py index), -1."""
        sdl = self._sdl
        for i in range(sdl.SDL_NumJoysticks() if sdl else 0):
            path = sdl.SDL_JoystickPathForIndex(i)
            if index_for_device_node(path.decode() if path else '') == js:
                return i
        return -1

    def guid_at(self, index: int) -> str:
        """SDL's GUID string (lower case) of device *index*."""
        sdl = self._sdl
        buf = ctypes.create_string_buffer(33)
        sdl.SDL_JoystickGetGUIDString(sdl.SDL_JoystickGetDeviceGUID(index), buf, 33)
        return buf.value.decode().lower()

    def guid(self, js: int) -> str:
        """GUID of controller *js*, '' when SDL doesn't have it."""
        i = self.device_index(js)
        return self.guid_at(i) if i >= 0 else ''

    def _mapping_for(self, guid: str) -> str:
        sdl = self._sdl
        p = sdl.SDL_GameControllerMappingForGUID(
            sdl.SDL_JoystickGetGUIDFromString(guid.encode()))
        if not p:
            return ''
        try:
            return ctypes.string_at(p).decode(errors='replace')
        finally:
            sdl.SDL_free(p)

    def add_mapping(self, mapping: str):
        """Use a just-made mapping now (the pad then navigates too)."""
        if self._sdl is not None:
            guid = mapping.split(',', 1)[0].lower()
            # What SDL has now is what removing it goes back to — unless it's
            # an earlier Janus mapping (still in SDL after a removal).
            if guid not in self._before.keys() | self._janus | self._unmapped:
                before = self._mapping_for(guid)
                if before:
                    self._before[guid] = before
            self._unmapped.discard(guid)
            self._janus.add(guid)
            self._sdl.SDL_GameControllerAddMapping(mapping.encode())
            self._drop_pads(guid)             # reopened with the new mapping
            self._last_scan = 0.0

    def remove_mapping(self, guid: str):
        """Forget the Janus mapping of *guid* (pads.json and here): back to
        the mapping SDL had before, or unrecognised."""
        import pad_mappings
        guid = guid.lower()
        pad_mappings.remove_mapping(guid)
        if self._sdl is None:
            return
        self._janus.discard(guid)
        before = self._before.pop(guid, '')
        if before:
            self._sdl.SDL_GameControllerAddMapping(before.encode())
        else:
            self._unmapped.add(guid)
        self._drop_pads(guid)
        self._rescan()                  # unknownPads up to date for the caller
        self._last_scan = time.monotonic()

    def _drop_pads(self, guid: str):
        """Close the open pads of *guid*; the next rescan reopens them."""
        sdl = self._sdl
        for i in range(sdl.SDL_NumJoysticks()):
            iid = sdl.SDL_JoystickGetDeviceInstanceID(i)
            if iid in self._pads and self.guid_at(i) == guid:
                sdl.SDL_GameControllerClose(self._pads.pop(iid).handle)

    def navigators(self) -> set[int]:
        """Controllers whose presses steer Big Picture now (js indexes)."""
        pads = {p.js_index for p in self._pads.values() if p.js_index in self._known_js}
        return (pads | set(self._hid)) - self.ignore_js

    def _held(self, pad: _Pad) -> set[str]:
        sdl = self._sdl
        held = {name for b, name in BUTTONS.items()
                if sdl.SDL_GameControllerGetButton(pad.handle, b)}
        x = sdl.SDL_GameControllerGetAxis(pad.handle, AXIS_LEFTX)
        y = sdl.SDL_GameControllerGetAxis(pad.handle, AXIS_LEFTY)
        if x < -STICK_THRESHOLD:
            held.add('left')
        elif x > STICK_THRESHOLD:
            held.add('right')
        if y < -STICK_THRESHOLD:
            held.add('up')
        elif y > STICK_THRESHOLD:
            held.add('down')
        # Triggers are axes (0..32767); report them as buttons.
        if sdl.SDL_GameControllerGetAxis(pad.handle, AXIS_TRIGGERLEFT) > STICK_THRESHOLD:
            held.add('lt')
        if sdl.SDL_GameControllerGetAxis(pad.handle, AXIS_TRIGGERRIGHT) > STICK_THRESHOLD:
            held.add('rt')
        return held

    def _poll(self):
        sdl = self._sdl
        now = time.monotonic()
        if now - self._last_scan > 1.0:
            self._last_scan = now
            self._rescan()
        sdl.SDL_GameControllerUpdate()
        sdl.SDL_FlushEvents(0, 0xFFFFFFFF)
        for idx, hp in list(self._hid.items()):
            if not hp.read():
                self._hid.pop(idx).close()
        for pad in list(self._pads.values()) + list(self._hid.values()):
            self._check_hold(pad, now)
            if isinstance(pad, _HidPad):
                held = set(pad.state)
                if not self._enabled or pad.js_index in self.ignore_js:
                    self._inactive_start(pad, held)
                    pad.buttons = held
                    continue
                self._emit(pad, held, now)
                continue
            held = self._held(pad)
            # Steam's virtual pads (and anything else not in the canonical
            # controller list) would double every press — skip them. The
            # setup wizard's pad presses are its answers, not navigation.
            if (not self._enabled or pad.js_index not in self._known_js
                    or pad.js_index in self.ignore_js):
                if pad.js_index in self._known_js:
                    self._inactive_start(pad, held)
                pad.buttons = held
                continue
            self._emit(pad, held, now)

    def _inactive_start(self, pad, held: set):
        if (not self._enabled and pad.js_index not in self.ignore_js
                and 'start' in held and 'start' not in pad.buttons):
            self.startInactive.emit(pad.js_index)

    def _check_hold(self, pad, now: float):
        if isinstance(pad, _HidPad):
            if pad.js_index in self.ignore_js:
                return
            down = 'start' in pad.state
        else:
            if pad.js_index not in self._known_js or pad.js_index in self.ignore_js:
                return
            down = bool(self._sdl.SDL_GameControllerGetButton(pad.handle, 6))
        if not down:
            pad.start_since = None
        elif pad.start_since is None:
            pad.start_since = now
        elif pad.start_since != -1 and now - pad.start_since >= JOIN_HOLD_SECONDS:
            pad.start_since = -1               # once per hold
            self.startHeld.emit(pad.js_index)

    def _emit(self, pad, held: set, now: float):
        pressed = held - pad.buttons
        pad.buttons = held
        for name in sorted(pressed):
            if name in DIRECTIONS:
                pad.dir_since[name] = now + REPEAT_DELAY
            self.button.emit(name, pad.js_index)
        for name in DIRECTIONS:
            if name not in held:
                pad.dir_since.pop(name, None)
            elif name not in pressed and now >= pad.dir_since.get(name, now + 1):
                pad.dir_since[name] = now + REPEAT_RATE
                self.button.emit(name, pad.js_index)

    def shutdown(self):
        if self._sdl is None:
            return
        self._timer.stop()
        for pad in self._pads.values():
            self._sdl.SDL_GameControllerClose(pad.handle)
        self._pads.clear()
        for hp in self._hid.values():
            hp.close()
        self._hid.clear()
        self._sdl.SDL_Quit()
        self._sdl = None

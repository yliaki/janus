"""Controller -> keyboard and mouse, per instance (Game.keyboardMouse).

For games that only take keyboard and mouse. Every instance runs in its own
gamescope, so input given to *that* gamescope reaches that one game and
nothing else: no desktop keyboard/mouse is involved, other tiles never see
it, and it works whether or not the tile has the desktop's focus.

The events go into gamescope's libei input socket ($XDG_RUNTIME_DIR/
gamescope-N-ei, the input-emulation server gamescope runs): gamescope takes
them as its own mouse and keyboard, moves its pointer and forwards them to
the game (relative motion for mouse look included). gamescopectl
`cursor_composite 2` then makes it draw the game's cursor at that pointer
(nested, gamescope otherwise leaves the cursor to the desktop, at the
*desktop's* pointer), and hide it as it does for a real mouse: when the game
sets an empty cursor (mouse look) or after 10 s without movement. Fallback
without libei: XTest into the instance's X server (works, cursor invisible).
NOTES.md "Keyboard and mouse from a controller".

The pad read is the player's virtual Xbox pad (Janus input, padd.py), never
grabbed; the game itself doesn't get it (launcher: no pad in its /dev).

Mapping (the handler's Game.keyboardMouse, every field optional):

    {
      leftStick: "wasd" | "mouse" | "arrows" | null,
      rightStick: "mouse" | "wasd" | "arrows" | null,
      mouseSpeed: 1400,        // px/s at full tilt
      deadzone: 0.2,           // stick fraction ignored around the centre
      keepPad: false,          // true: the game still gets the pad as well
                               // (launcher_service); for games with pad
                               // support that need a few keys/clicks on top
      buttons: {a: "space", rt: "mouse1", start: "Escape", up: "1", ...}
    }

Buttons: a b x y lb rb lt rt back start guide l3 r3 up down left right.
Targets: X keysym names ("space", "Escape", "Shift_L", "Tab", "e", "1"),
"mouse1" (left) "mouse2" (middle) "mouse3" (right), "wheelup", "wheeldown"
(repeated while held), "type:<text>" (types the text on press: names a
controller can't type, e.g. a save name).
"""

import os
import select
import struct
import threading
import time
from typing import Callable, Dict, Optional

_EVENT = struct.Struct('llHHi')
EV_KEY, EV_ABS = 1, 3

# Janus's virtual pad (xpad wired Xbox 360 layout, padd.XPAD_*)
_BUTTON_CODES = {0x130: 'a', 0x131: 'b', 0x133: 'x', 0x134: 'y', 0x136: 'lb',
                 0x137: 'rb', 0x13a: 'back', 0x13b: 'start', 0x13c: 'guide',
                 0x13d: 'l3', 0x13e: 'r3'}
ABS_X, ABS_Y, ABS_Z, ABS_RX, ABS_RY, ABS_RZ, HAT_X, HAT_Y = 0, 1, 2, 3, 4, 5, 0x10, 0x11

DEFAULT_SPEED = 1400.0
WHEEL_REPEAT = 0.12          # s between wheel clicks while held
STICK_KEYS = {'wasd': ('w', 'a', 's', 'd'), 'arrows': ('Up', 'Left', 'Down', 'Right')}


class _X:
    """XTest on one X display (python-xlib)."""

    def __init__(self, display: str):
        from Xlib import X, XK, display as xdisplay
        from Xlib.ext import xtest
        self.X, self.XK, self.xtest = X, XK, xtest
        self.d = xdisplay.Display(display)
        if not self.d.has_extension('XTEST'):
            raise OSError(f'{display} has no XTEST extension')
        self._codes: Dict[str, int] = {}

    def keycode(self, name: str) -> int:
        if name not in self._codes:
            sym = self.XK.string_to_keysym(name)
            if not sym and len(name) == 1:
                sym = ord(name)
            self._codes[name] = self.d.keysym_to_keycode(sym) if sym else 0
        return self._codes[name]

    def _raw_key(self, code: int, down: bool):
        self.xtest.fake_input(self.d, self.X.KeyPress if down else self.X.KeyRelease, code)

    def key(self, name: str, down: bool):
        code = self.keycode(name)
        if code:
            self._raw_key(code, down)

    def button(self, n: int, down: bool):
        self.xtest.fake_input(self.d, self.X.ButtonPress if down else self.X.ButtonRelease, n)

    def tap(self, name: str) -> bool:
        """Press and release the key for keysym *name* ("a", "A", "exclam",
        "BackSpace"), holding Shift when the keysym is on the key's second
        level in this display's keymap. False if the keymap lacks it."""
        code = self.keycode(name)
        if not code:
            return False
        sym = self.XK.string_to_keysym(name) or (ord(name) if len(name) == 1 else 0)
        up = self.d.keycode_to_keysym(code, 0) != sym \
            and self.d.keycode_to_keysym(code, 1) == sym
        shift = self.keycode('Shift_L') if up else 0
        if shift:
            self._raw_key(shift, True)
        self._raw_key(code, True)
        self._raw_key(code, False)
        if shift:
            self._raw_key(shift, False)
        self.flush()
        return True

    def type(self, text: str):
        """Type *text* (letters, digits, space, common punctuation)."""
        names = {' ': 'space', '-': 'minus', '_': 'underscore', '.': 'period',
                 ',': 'comma', "'": 'apostrophe', '!': 'exclam', '?': 'question'}
        for ch in text:
            if self.tap(names.get(ch, ch)):
                time.sleep(0.02)

    def move(self, dx: int, dy: int):
        # detail=1: relative motion (what mouse look reads)
        self.xtest.fake_input(self.d, self.X.MotionNotify, detail=1, x=dx, y=dy)

    def flush(self):
        self.d.flush()

    def close(self):
        try:
            self.d.close()
        except Exception:
            pass


class _Ei(_X):
    """Input through gamescope's libei socket (see the module doc). Key names
    are looked up in the instance's X keymap (X keycode - 8 = evdev code)."""

    _LIB = None
    BTN = {1: 0x110, 2: 0x112, 3: 0x111}         # X button -> BTN_LEFT/MIDDLE/RIGHT

    @classmethod
    def _lib(cls):
        if cls._LIB is None:
            import ctypes
            L = ctypes.CDLL('libei.so.1')
            P, I, U32, U64, D, B = (ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32,
                                    ctypes.c_uint64, ctypes.c_double, ctypes.c_bool)
            for n, r, a in (
                    ('ei_new_sender', P, [P]), ('ei_configure_name', None, [P, ctypes.c_char_p]),
                    ('ei_setup_backend_socket', I, [P, ctypes.c_char_p]), ('ei_get_fd', I, [P]),
                    ('ei_dispatch', None, [P]), ('ei_get_event', P, [P]),
                    ('ei_event_get_type', I, [P]), ('ei_event_get_seat', P, [P]),
                    ('ei_event_get_device', P, [P]), ('ei_event_unref', P, [P]),
                    ('ei_device_ref', P, [P]), ('ei_device_start_emulating', None, [P, U32]),
                    ('ei_device_pointer_motion', None, [P, D, D]),
                    ('ei_device_frame', None, [P, U64]), ('ei_now', U64, [P]),
                    ('ei_device_button_button', None, [P, U32, B]),
                    ('ei_device_scroll_discrete', None, [P, ctypes.c_int32, ctypes.c_int32]),
                    ('ei_device_keyboard_key', None, [P, U32, B]), ('ei_unref', P, [P])):
                f = getattr(L, n)
                f.restype, f.argtypes = r, a
            L.ei_seat_bind_capabilities.restype = None
            cls._LIB = L
        return cls._LIB

    def __init__(self, display: str, socket_path: str):
        super().__init__(display)           # the X keymap, for key names
        import ctypes
        L = self.L = self._lib()
        self.ei = L.ei_new_sender(None)
        L.ei_configure_name(self.ei, b'janus-kbm')
        if L.ei_setup_backend_socket(self.ei, socket_path.encode()) != 0:
            raise OSError(f'libei: cannot connect to {socket_path}')
        fd, cand, self.dev, seq = L.ei_get_fd(self.ei), None, None, 1
        end = time.monotonic() + 5
        I, P = ctypes.c_int, ctypes.c_void_p
        while self.dev is None and time.monotonic() < end:
            select.select([fd], [], [], 0.2)
            L.ei_dispatch(self.ei)
            while True:
                ev = L.ei_get_event(self.ei)
                if not ev:
                    break
                t = L.ei_event_get_type(ev)
                if t == 3:                       # SEAT_ADDED: pointer, button, scroll, keyboard
                    # Variadic: no argtypes, so every argument is a ctypes
                    # object (a bare int pointer would be passed as a 32-bit
                    # int: a segfault as soon as the heap is above 4 GB, as
                    # in any thread).
                    L.ei_seat_bind_capabilities(P(L.ei_event_get_seat(ev)), I(1), I(1 << 5),
                                                I(1 << 4), I(1 << 2), P(None))
                elif t == 5:                     # DEVICE_ADDED
                    cand = L.ei_device_ref(L.ei_event_get_device(ev))
                elif t == 8 and cand:            # DEVICE_RESUMED
                    self.dev = cand
                    L.ei_device_start_emulating(self.dev, seq)
                elif t == 2:                     # DISCONNECT
                    L.ei_event_unref(ev)
                    raise OSError('libei: gamescope disconnected')
                L.ei_event_unref(ev)
        if self.dev is None:
            raise OSError('libei: no input device from gamescope')
        self._dirty = False

    def _raw_key(self, code: int, down: bool):
        self.L.ei_device_keyboard_key(self.dev, code - 8, down)
        self._dirty = True

    def button(self, n: int, down: bool):
        if n in (4, 5):                          # wheel up / down, one click
            if down:
                self.L.ei_device_scroll_discrete(self.dev, 0, -120 if n == 4 else 120)
                self._dirty = True
            return
        if n in self.BTN:
            self.L.ei_device_button_button(self.dev, self.BTN[n], down)
            self._dirty = True

    def move(self, dx: int, dy: int):
        self.L.ei_device_pointer_motion(self.dev, float(dx), float(dy))
        self._dirty = True

    def flush(self):
        if self._dirty:
            self.L.ei_device_frame(self.dev, self.L.ei_now(self.ei))
            self._dirty = False
        self.L.ei_dispatch(self.ei)
        while True:                              # drain (pings etc.)
            ev = self.L.ei_get_event(self.ei)
            if not ev:
                break
            self.L.ei_event_unref(ev)

    def close(self):
        try:
            self.L.ei_unref(self.ei)
        except Exception:
            pass
        super().close()


def show_cursor(socket_name: str) -> bool:
    """gamescopectl cursor_composite 2 on the instance's gamescope: it draws
    the game's cursor itself (see the module doc)."""
    import shutil
    import subprocess
    ctl = shutil.which('gamescopectl')
    if not ctl:
        try:
            import prereqs
            gs = prereqs.find_gamescope()
            ctl = os.path.join(os.path.dirname(gs), 'gamescopectl') if gs else None
        except Exception:
            ctl = None
    if not ctl or not os.path.exists(ctl):
        return False
    r = subprocess.run([ctl, 'cursor_composite', '2'], capture_output=True, timeout=5,
                       env=dict(os.environ, GAMESCOPE_WAYLAND_DISPLAY=socket_name))
    return r.returncode == 0


def _target(name: Optional[str]):
    """('key', keysym) | ('button', n) | ('wheel', n) | None."""
    if not name:
        return None
    n = str(name)
    low = n.lower()
    if low in ('mouse1', 'mouse2', 'mouse3'):
        return ('button', int(low[-1]))
    if low == 'wheelup':
        return ('wheel', 4)
    if low == 'wheeldown':
        return ('wheel', 5)
    if low.startswith('type:'):
        return ('type', n[5:])
    return ('key', n)


class KbmEmulator:
    """One instance: reads *node* (its virtual pad) and drives the X display
    *display_fn()* returns (looked up until the game's gamescope is up)."""

    def __init__(self, node: str, display_fn: Callable[[], Optional[str]], mapping: dict,
                 log=print, label: str = '',
                 socket_fn: Callable[[], Optional[str]] = lambda: None):
        self.node, self.display_fn, self.log, self.label = node, display_fn, log, label
        self.socket_fn = socket_fn
        m = mapping or {}
        self.left = m.get('leftStick', 'wasd')
        self.right = m.get('rightStick', 'mouse')
        self.speed = float(m.get('mouseSpeed') or DEFAULT_SPEED)
        self.dead = float(m.get('deadzone') or 0.2)
        self.buttons = {k: _target(v) for k, v in (m.get('buttons') or {}).items()}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name=f'kbm-{label}')
        self._thread.start()

    def stop(self):
        self._stop.set()

    # --- state -> X ------------------------------------------------------------
    def _press(self, x: _X, target, down: bool):
        if target is None:
            return
        kind, v = target
        if kind == 'key':
            x.key(v, down)
        elif kind == 'button':
            x.button(v, down)
        elif kind == 'wheel' and down:
            x.button(v, True)
            x.button(v, False)
        elif kind == 'type' and down:
            x.type(v)

    def _stick_keys(self, x: _X, mode, fx: float, fy: float, held: dict, which: str):
        keys = STICK_KEYS.get(mode)
        if not keys:
            return
        up, left, down, right = keys
        want = {up: fy < -0.5, down: fy > 0.5, left: fx < -0.5, right: fx > 0.5}
        for k, on in want.items():
            if held.get((which, k), False) != on:
                held[(which, k)] = on
                x.key(k, on)

    def _run(self):
        x = None
        fd = None
        try:
            how = ''
            while not self._stop.is_set():
                disp, sock = self.display_fn(), self.socket_fn()
                if disp:
                    rt = os.environ.get('XDG_RUNTIME_DIR', '')
                    ei_path = os.path.join(rt, sock + '-ei') if sock and rt else ''
                    if ei_path and os.path.exists(ei_path):
                        try:
                            x = _Ei(disp, ei_path)
                            how = f'gamescope {sock} (libei)'
                            if show_cursor(sock):
                                how += ', cursor drawn by gamescope'
                            break
                        except Exception as e:  # noqa: BLE001
                            self.log(f'[kbm] {self.label}: libei on {ei_path}: {e}; XTest instead')
                    try:
                        x = _X(disp)
                        how = f'{disp} (XTest, no visible cursor)'
                        break
                    except Exception as e:  # noqa: BLE001 — Xwayland not up yet
                        self.log(f'[kbm] {self.label}: {disp}: {e}')
                self._stop.wait(1.0)
            if x is None:
                return
            fd = os.open(self.node, os.O_RDONLY | os.O_NONBLOCK)
            self.log(f'[kbm] {self.label}: {self.node} -> keyboard/mouse via {how}')
            abs_ = {ABS_X: 0, ABS_Y: 0, ABS_RX: 0, ABS_RY: 0, ABS_Z: 0, ABS_RZ: 0, HAT_X: 0, HAT_Y: 0}
            pressed: Dict[str, bool] = {}
            held: dict = {}
            wheel_next: Dict[str, float] = {}
            frac = [0.0, 0.0]
            last = time.monotonic()
            while not self._stop.is_set():
                r, _, _ = select.select([fd], [], [], 1 / 120)
                if r:
                    try:
                        data = os.read(fd, _EVENT.size * 64)
                    except BlockingIOError:
                        data = b''
                    except OSError:
                        break                      # pad gone (player left)
                    for off in range(0, len(data) - _EVENT.size + 1, _EVENT.size):
                        _, _, t, code, val = _EVENT.unpack_from(data, off)
                        if t == EV_KEY and code in _BUTTON_CODES:
                            self._button(x, _BUTTON_CODES[code], bool(val), pressed, wheel_next)
                        elif t == EV_ABS and code in abs_:
                            abs_[code] = val
                            if code in (ABS_Z, ABS_RZ):
                                self._button(x, 'lt' if code == ABS_Z else 'rt', val > 100,
                                             pressed, wheel_next)
                            elif code == HAT_X:
                                self._button(x, 'left', val < 0, pressed, wheel_next)
                                self._button(x, 'right', val > 0, pressed, wheel_next)
                            elif code == HAT_Y:
                                self._button(x, 'up', val < 0, pressed, wheel_next)
                                self._button(x, 'down', val > 0, pressed, wheel_next)
                now = time.monotonic()
                dt, last = min(now - last, 0.05), now
                # held wheel buttons repeat
                for b, t_next in list(wheel_next.items()):
                    if now >= t_next:
                        self._press(x, self.buttons.get(b), True)
                        wheel_next[b] = now + WHEEL_REPEAT
                # sticks
                sticks = (('l', self.left, abs_[ABS_X], abs_[ABS_Y]),
                          ('r', self.right, abs_[ABS_RX], abs_[ABS_RY]))
                for which, mode, ax, ay in sticks:
                    fx, fy = ax / 32767.0, ay / 32767.0
                    if mode == 'mouse':
                        mag = (fx * fx + fy * fy) ** 0.5
                        if mag > self.dead:
                            scale = ((mag - self.dead) / (1 - self.dead)) ** 1.6 / mag
                            frac[0] += fx * scale * self.speed * dt
                            frac[1] += fy * scale * self.speed * dt
                            dx, dy = int(frac[0]), int(frac[1])
                            if dx or dy:
                                frac[0] -= dx
                                frac[1] -= dy
                                x.move(dx, dy)
                    else:
                        self._stick_keys(x, mode, fx, fy, held, which)
                x.flush()
        except Exception as e:  # noqa: BLE001
            self.log(f'[kbm] {self.label}: stopped: {e}')
        finally:
            if x is not None:
                try:                                # release whatever is held
                    for (_, k), on in list(held.items()) if 'held' in locals() else []:
                        if on:
                            x.key(k, False)
                    for b, on in list(pressed.items()) if 'pressed' in locals() else []:
                        if on:
                            self._press(x, self.buttons.get(b), False)
                    x.flush()
                except Exception:
                    pass
                x.close()
            if fd is not None:
                os.close(fd)

    def _button(self, x, name, down, pressed, wheel_next):
        if pressed.get(name, False) == down:
            return
        pressed[name] = down
        target = self.buttons.get(name)
        if target is None:
            return
        if target[0] == 'wheel':
            if down:
                self._press(x, target, True)
                wheel_next[name] = time.monotonic() + 0.35
            else:
                wheel_next.pop(name, None)
            return
        self._press(x, target, down)

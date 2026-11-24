"""On-screen keyboard for Janus.

Controller-driven OSK that pops up over a gamescope tile when the player
presses L3+R3 on their controller. D-pad navigates (hold to repeat), A types
the selected key, B (or L3+R3 again) closes, X toggles shift, Y = space,
LB = backspace, Start = Enter.

How it reaches the screen and the game (NOTES.md "On-Screen Keyboard"):

* The gamescope tiles are **XWayland** windows (steam.py sets
  SDL_VIDEODRIVER=x11), so the OSK runs on XWayland too (QT_QPA_PLATFORM=xcb,
  forced in main()) as an **override-redirect** window. As a native Wayland
  client its position was ignored (KWin placed it, often off-centre or on
  another screen) and it had no way to stay above the tile.
* Keys go **into the instance's gamescope**, through its libei socket (as
  kbm.py does), else XTest into the instance's own X server. Neither needs
  the tile to be focused, so nothing is activated or raised per key and the
  OSK stays on top. Only without either (standalone, no gamescope) does it
  fall back to `xdotool key` on the host display.
* The keyboard is painted at a size computed from the tile: it is always
  centred in the tile and never extends past it.

Standalone:
    python -m src.host.osk
    # Any controller's L3+R3 summons the OSK; keys go to the focused window.

Per-instance:
    python -m src.host.osk --bind 0:12345 --bind 1:12346
    # js0 -> gamescope (process) 12345; js1 -> gamescope 12346.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import re
import select
import struct
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ioctl(fd, EVIOCGRAB, 1) → exclusive access. Other readers of the same
# /dev/input/eventN see no events until we ungrab (0). This is how we
# freeze the bound controller's input to the game while the OSK is open.
EVIOCGRAB = 0x40044590

from PySide6.QtCore import QObject, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QApplication, QWidget


# ── evdev constants ──────────────────────────────────────────────────

EV_KEY = 0x01
EV_ABS = 0x03

BTN_SOUTH      = 0x130   # A / Cross
BTN_EAST       = 0x131   # B / Circle
BTN_NORTH      = 0x133   # X (xpad layout)
BTN_WEST       = 0x134   # Y
BTN_TL         = 0x136   # LB
BTN_TR         = 0x137   # RB
BTN_SELECT     = 0x13a
BTN_START      = 0x13b
BTN_THUMBL     = 0x13d   # L3
BTN_THUMBR     = 0x13e   # R3
BTN_DPAD_UP    = 0x220
BTN_DPAD_DOWN  = 0x221
BTN_DPAD_LEFT  = 0x222
BTN_DPAD_RIGHT = 0x223

ABS_HAT0X = 0x10
ABS_HAT0Y = 0x11

EVENT_FMT = '@llHHi'
EVENT_SIZE = struct.calcsize(EVENT_FMT)

# Directions: -2 left, -1 up, +1 down, +2 right
_DPAD_DIR = {BTN_DPAD_UP: -1, BTN_DPAD_DOWN: +1, BTN_DPAD_LEFT: -2, BTN_DPAD_RIGHT: +2}


# ── keyboard layout ──────────────────────────────────────────────────

class Key:
    """One key: label/keysym, and the shifted label/keysym (None = same)."""
    __slots__ = ('label', 'sym', 'slabel', 'ssym', 'width', 'action')

    def __init__(self, label, sym=None, slabel=None, ssym=None, width=1.0, action=None):
        self.label, self.sym = label, sym or label
        self.slabel, self.ssym = slabel, ssym
        self.width = width
        self.action = action            # 'shift' = toggle shift instead of typing

    def text(self, shift: bool) -> str:
        return (self.slabel or self.label) if shift else self.label

    def keysym(self, shift: bool) -> str:
        return (self.ssym or self.sym) if shift else self.sym


def _letter(c):
    return Key(c, c, c.upper(), c.upper())


def _chars(pairs):
    """'1!' style pairs with their X keysym names."""
    names = {'!': 'exclam', '@': 'at', '#': 'numbersign', '$': 'dollar', '%': 'percent',
             '^': 'asciicircum', '&': 'ampersand', '*': 'asterisk', '(': 'parenleft',
             ')': 'parenright', '-': 'minus', '_': 'underscore', '=': 'equal',
             '+': 'plus', "'": 'apostrophe', '"': 'quotedbl', ',': 'comma',
             '<': 'less', '.': 'period', '>': 'greater', '/': 'slash', '?': 'question',
             ';': 'semicolon', ':': 'colon'}
    return [Key(a, names.get(a, a), b, names.get(b, b)) for a, b in pairs]


# US-style rows; shifted symbols shown when shift is on. Rows are centred and
# may differ in width (key widths in units of one key).
KEY_ROWS: list[list[Key]] = [
    _chars(['1!', '2@', '3#', '4$', '5%', '6^', '7&', '8*', '9(', '0)'])
    + [Key('⌫', 'BackSpace', width=1.5)],
    [_letter(c) for c in 'qwertyuiop'] + _chars(['-_']) ,
    [_letter(c) for c in 'asdfghjkl'] + _chars(["'\""]) + [Key('↵', 'Return', width=1.5)],
    [Key('⇧', 'Shift_L', width=1.5, action='shift')] + [_letter(c) for c in 'zxcvbnm']
    + _chars([',<', '.>', '/?']),
    [Key('Esc', 'Escape', width=1.5), Key('Tab', 'Tab', width=1.5),
     Key('←', 'Left'), Key('→', 'Right'), Key('Space', 'space', width=4.0)]
    + _chars([';:', '=+']),
]


def _row_units(row) -> float:
    return sum(k.width for k in row) + 0.1 * (len(row) - 1)   # 0.1 = gap


def key_rects(unit: float) -> list[list[QRectF]]:
    """Each key's rect (keyboard coordinates, origin = top-left of the key
    area) for a key size of *unit* px. Rows centred on the widest one."""
    gap = unit * 0.1
    widest = max(_row_units(r) for r in KEY_ROWS) * unit
    out = []
    for r, row in enumerate(KEY_ROWS):
        x = (widest - _row_units(row) * unit) / 2
        y = r * (unit + gap)
        rects = []
        for k in row:
            rects.append(QRectF(x, y, k.width * unit, unit))
            x += k.width * unit + gap
        out.append(rects)
    return out


def nav_target(row: int, col: int, direction: int, want_x: float | None = None):
    """(row, col, want_x) after moving *direction* from (row, col): left/right
    wrap within the row, up/down go to the key of the next row whose centre
    is nearest *want_x* (the column the player started moving vertically
    from, so going up then down returns to the same key)."""
    rects = key_rects(1.0)
    cx = rects[row][col].center().x()
    if direction in (-2, +2):
        n = len(KEY_ROWS[row])
        return row, (col + (1 if direction > 0 else -1)) % n, None
    target_x = cx if want_x is None else want_x
    row = (row + (1 if direction > 0 else -1)) % len(KEY_ROWS)
    col = min(range(len(KEY_ROWS[row])),
              key=lambda c: abs(rects[row][c].center().x() - target_x))
    return row, col, target_x


# ── sysfs / device discovery ─────────────────────────────────────────

def find_gamepads() -> list[tuple[int, str]]:
    """Return [(controller_index, event_device_path), ...] for connected
    gamepads: the controllers.py list (Steam's virtual pads left out), event
    node of each, including pads without a js node."""
    from controllers import event_node, list_controllers
    out = []
    for c in list_controllers():
        ev = event_node(c.index)
        if ev:
            out.append((c.index, ev))
    if os.environ.get('JANUS_OSK_VIRTUAL') == '1':
        # Janus input mode: the input daemon holds the physical pads (grabbed);
        # the OSK reads — and grabs — each player's virtual Xbox pad instead
        # (padd.py), bound by its js number.
        from controllers import JANUS_PHYS_PREFIX
        for js in sorted(n for n in os.listdir('/sys/class/input') if n.startswith('js')):
            dev = f'/sys/class/input/{js}/device'
            try:
                with open(f'{dev}/phys') as f:
                    if not f.read().startswith(JANUS_PHYS_PREFIX):
                        continue
            except OSError:
                continue
            ev = event_node(int(js[2:]))
            if ev:
                out.append((int(js[2:]), ev))
    return out


# ── window / instance lookup ─────────────────────────────────────────

def _children_of(pid: int) -> list[int]:
    try:
        with open(f'/proc/{pid}/task/{pid}/children') as f:
            return [int(x) for x in f.read().split()]
    except (OSError, ValueError):
        return []


def _all_descendants(pid: int, depth: int = 4) -> list[int]:
    seen = []
    frontier = [pid]
    for _ in range(depth):
        nxt = []
        for p in frontier:
            kids = _children_of(p)
            seen.extend(kids)
            nxt.extend(kids)
        frontier = nxt
    return seen


def instance_wmclass(pid: int) -> str | None:
    """The tile's SDL_VIDEO_X11_WMCLASS (janus-p<N>) from the environment of
    the bound gamescope process or one of its children."""
    for p in [pid] + _all_descendants(pid, depth=2):
        try:
            with open(f'/proc/{p}/environ', 'rb') as f:
                m = re.search(rb'(?:^|\0)SDL_VIDEO_X11_WMCLASS=([^\0]+)', f.read())
        except OSError:
            continue
        if m:
            return m.group(1).decode()
    return None


def _xdotool(*args: str) -> str:
    try:
        return subprocess.run(['xdotool', *args], capture_output=True, text=True,
                              timeout=2).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ''


def find_gamescope_window(pid: int, wmclass: str | None = None,
                          timeout: float = 8.0) -> str | None:
    """The tile's host X window: by its WM_CLASS (janus-p<N>, exact) when
    known, else any window of pid or a descendant."""
    deadline = time.time() + timeout
    while True:
        if wmclass:
            wids = _xdotool('search', '--classname', f'^{re.escape(wmclass)}$').split()
            if wids:
                return wids[-1]
        candidates: list[str] = []
        for proc in [pid] + _all_descendants(pid):
            candidates.extend(_xdotool('search', '--pid', str(proc)).split())
        if candidates:
            return candidates[-1]
        if time.time() >= deadline:
            return None
        time.sleep(0.25)


def get_window_geometry(wid: str) -> tuple[int, int, int, int] | None:
    """(x, y, w, h) of an X window in Qt logical pixels (xdotool gives X
    pixels; Qt's xcb backend divides them by the device pixel ratio)."""
    d = {}
    for line in _xdotool('getwindowgeometry', '--shell', wid).splitlines():
        if '=' in line:
            k, v = line.split('=', 1)
            d[k] = v
    try:
        x, y, w, h = (int(d[k]) for k in ('X', 'Y', 'WIDTH', 'HEIGHT'))
    except (KeyError, ValueError):
        return None
    if w <= 1 or h <= 1:
        return None                       # unmapped / not yet sized
    app = QApplication.instance()
    ratio = 1.0
    if app is not None and app.primaryScreen() is not None:
        ratio = app.primaryScreen().devicePixelRatio() or 1.0
    if ratio != 1.0:
        return (round(x / ratio), round(y / ratio), round(w / ratio), round(h / ratio))
    return x, y, w, h


# ── key injection ────────────────────────────────────────────────────

class Injector:
    """Sends keys into one instance: its gamescope's libei socket, else XTest
    on its nested X display, else (standalone) xdotool on the host display
    with the target window activated first."""

    def __init__(self, wmclass: str | None, host_wid: str | None):
        self.wmclass, self.host_wid = wmclass, host_wid
        self._dev = None
        self.how = ''

    def _connect(self):
        if self._dev is not None or not self.wmclass:
            return self._dev
        import kbm
        import resize
        disp = resize.nested_display(self.wmclass)
        sock = resize.gamescope_socket(self.wmclass)
        rt = os.environ.get('XDG_RUNTIME_DIR', '')
        if disp:
            ei = os.path.join(rt, sock + '-ei') if sock and rt else ''
            if ei and os.path.exists(ei):
                try:
                    self._dev, self.how = kbm._Ei(disp, ei), f'libei {sock}'
                except Exception as e:  # noqa: BLE001
                    print(f'[osk] {self.wmclass}: libei: {e}; XTest instead', flush=True)
            if self._dev is None:
                try:
                    self._dev, self.how = kbm._X(disp), f'XTest {disp}'
                except Exception as e:  # noqa: BLE001
                    print(f'[osk] {self.wmclass}: XTest on {disp}: {e}', flush=True)
        if self._dev is not None:
            print(f'[osk] {self.wmclass}: keys via {self.how}', flush=True)
        return self._dev

    def tap(self, keysym: str) -> None:
        dev = self._connect()
        if dev is not None:
            try:
                if not dev.tap(keysym):
                    print(f'[osk] {self.wmclass}: no key for {keysym!r}', flush=True)
                return
            except Exception as e:  # noqa: BLE001 — gamescope gone / restarted
                print(f'[osk] {self.wmclass}: send failed ({e}); reconnecting', flush=True)
                self.close()
                dev = self._connect()
                if dev is not None:
                    dev.tap(keysym)
                    return
        # Standalone: host display, focused window.
        if self.host_wid:
            subprocess.run(['xdotool', 'windowactivate', '--sync', self.host_wid],
                           capture_output=True, timeout=2)
        subprocess.run(['xdotool', 'key', '--clearmodifiers', keysym],
                       capture_output=True, timeout=2)

    def close(self):
        if self._dev is not None:
            try:
                self._dev.close()
            except Exception:  # noqa: BLE001
                pass
        self._dev = None


# ── evdev monitor thread ─────────────────────────────────────────────

class GamepadMonitor(QObject):
    """Reads gamepad event devices and emits per-controller Qt signals."""

    summon    = Signal(int)        # js_index → toggle OSK for this controller
    nav       = Signal(int, int)   # js_index, dir (press)
    nav_up    = Signal(int, int)   # js_index, dir (release)
    select    = Signal(int)        # A
    cancel    = Signal(int)        # B
    shift     = Signal(int)        # X
    space     = Signal(int)        # Y
    backspace = Signal(int)        # LB
    enter     = Signal(int)        # Start

    def __init__(self):
        super().__init__()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._pressed: dict[int, set[int]] = {}
        self._hat: dict[tuple[int, int], int] = {}     # (js, axis) → last value
        # js_index → open fd, so the controller can EVIOCGRAB a specific pad.
        self._js_fds: dict[int, int] = {}
        self._fd_lock = threading.Lock()
        self._grabbed: set[int] = set()

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def grab(self, js_index: int) -> bool:
        """EVIOCGRAB the pad so the game stops seeing its input."""
        with self._fd_lock:
            fd = self._js_fds.get(js_index)
        if fd is None:
            print(f'[osk] grab: no fd for js{js_index}', flush=True)
            return False
        try:
            fcntl.ioctl(fd, EVIOCGRAB, 1)
            self._grabbed.add(js_index)
            print(f'[osk] grabbed js{js_index} (game frozen for this pad)', flush=True)
            return True
        except OSError as e:
            print(f'[osk] grab failed for js{js_index}: {e}', flush=True)
            return False

    def ungrab(self, js_index: int) -> None:
        with self._fd_lock:
            fd = self._js_fds.get(js_index)
        if fd is None:
            return
        try:
            fcntl.ioctl(fd, EVIOCGRAB, 0)
        except OSError as e:
            print(f'[osk] ungrab failed for js{js_index}: {e}', flush=True)
        self._grabbed.discard(js_index)
        print(f'[osk] ungrabbed js{js_index}', flush=True)

    def _open(self, js_index: int, dev_path: str, fds: dict) -> None:
        try:
            fd = os.open(dev_path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as e:
            print(f'[osk] cannot open {dev_path}: {e}', file=sys.stderr)
            return
        fds[fd] = (js_index, dev_path)
        with self._fd_lock:
            self._js_fds[js_index] = fd
        print(f'[osk] watching js{js_index} -> {dev_path}', flush=True)

    def _forget_fd(self, fd: int, fds: dict) -> None:
        info = fds.pop(fd, None)
        if info is not None:
            js_index, _ = info
            with self._fd_lock:
                if self._js_fds.get(js_index) == fd:
                    self._js_fds.pop(js_index, None)
            self._grabbed.discard(js_index)
        try:
            os.close(fd)
        except OSError:
            pass

    def _run(self):
        fds: dict[int, tuple[int, str]] = {}
        for js_index, dev_path in find_gamepads():
            self._open(js_index, dev_path, fds)
        if not fds:
            print('[osk] no gamepads found yet', file=sys.stderr)
        last_rescan = time.time()
        while not self._stop.is_set():
            try:
                readable, _, _ = select.select(list(fds), [], [], 1.0)
            except (ValueError, OSError):
                readable = []
            for fd in readable:
                js_index, _ = fds[fd]
                try:
                    data = os.read(fd, EVENT_SIZE * 64)
                except OSError:
                    self._forget_fd(fd, fds)
                    continue
                self._handle_events(js_index, data)
            if time.time() - last_rescan > 3.0:     # hot-plug
                last_rescan = time.time()
                current = {p for _, p in fds.values()}
                for js_index, dev_path in find_gamepads():
                    if dev_path not in current:
                        self._open(js_index, dev_path, fds)

    def _handle_events(self, js_index: int, data: bytes):
        pressed = self._pressed.setdefault(js_index, set())
        for off in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
            _, _, ev_type, code, value = struct.unpack(EVENT_FMT, data[off:off + EVENT_SIZE])
            if ev_type == EV_KEY:
                self._handle_key(js_index, pressed, code, value)
            elif ev_type == EV_ABS and code in (ABS_HAT0X, ABS_HAT0Y):
                self._handle_hat(js_index, code, value)

    def _handle_key(self, js_index: int, pressed: set[int], code: int, value: int):
        if value == 0:
            pressed.discard(code)
            if code in _DPAD_DIR:
                self.nav_up.emit(js_index, _DPAD_DIR[code])
            return
        if value != 1:                 # autorepeat from the kernel: ours instead
            return
        pressed.add(code)
        if code in (BTN_THUMBL, BTN_THUMBR) and {BTN_THUMBL, BTN_THUMBR} <= pressed:
            print(f'[osk] js{js_index}: L3+R3 chord → summon', flush=True)
            self.summon.emit(js_index)
            return
        if code in _DPAD_DIR:
            self.nav.emit(js_index, _DPAD_DIR[code])
            return
        sig = {BTN_SOUTH: self.select, BTN_EAST: self.cancel, BTN_NORTH: self.shift,
               BTN_WEST: self.space, BTN_TL: self.backspace, BTN_START: self.enter}.get(code)
        if sig is not None:
            sig.emit(js_index)

    def _handle_hat(self, js_index: int, code: int, value: int):
        """D-pad as HAT axes (xpad / Janus's virtual pad, PS, generic USB)."""
        neg, pos = (-2, +2) if code == ABS_HAT0X else (-1, +1)
        old = self._hat.get((js_index, code), 0)
        if value == old:
            return
        self._hat[(js_index, code)] = value
        if old:
            self.nav_up.emit(js_index, neg if old < 0 else pos)
        if value:
            self.nav.emit(js_index, neg if value < 0 else pos)


# ── OSK widget ───────────────────────────────────────────────────────

C_PANEL = QColor(26, 26, 30, 240)
C_BORDER = QColor(92, 92, 255)
C_KEY = QColor(42, 42, 53)
C_KEY_EDGE = QColor(68, 68, 68)
C_SEL = QColor(92, 92, 255)
C_TEXT = QColor(240, 240, 240)
C_HINT = QColor(170, 170, 187)
C_ON = QColor(60, 140, 90)


class OSKWindow(QWidget):
    """Override-redirect keyboard, painted at a size that fits its tile."""

    MAX_UNIT = 56          # key size cap (px)
    MIN_UNIT = 14
    HINT = 'A type · B close · X shift · Y space · LB ⌫ · Start ↵'

    def __init__(self):
        super().__init__()
        # Override-redirect (xcb): KWin doesn't manage it, so it keeps the
        # position we give it, stays above the managed tiles and never takes
        # focus.
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                            | Qt.X11BypassWindowManagerHint | Qt.Tool
                            | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFocusPolicy(Qt.NoFocus)
        self._row, self._col, self._want_x = 1, 0, None
        self._shift = False
        self._unit = float(self.MAX_UNIT)
        self._show_hint = True

    # — geometry —

    def _metrics(self, unit: float, hint: bool):
        pad = unit * 0.22
        rows = len(KEY_ROWS)
        keys_w = max(_row_units(r) for r in KEY_ROWS) * unit
        keys_h = rows * unit + (rows - 1) * unit * 0.1
        hint_h = unit * 0.45 if hint else 0.0       # linear in unit (see position_over)
        return pad, keys_w + 2 * pad, keys_h + 2 * pad + hint_h

    def position_over(self, geom: tuple[int, int, int, int]):
        """Size the keyboard to the tile (x, y, w, h) and centre it there:
        the largest key size up to MAX_UNIT at which it fits inside the tile
        with a margin. It never extends past the tile."""
        x, y, w, h = geom
        margin = max(4, min(w, h) // 40)
        avail_w, avail_h = max(1, w - 2 * margin), max(1, h - 2 * margin)
        # The size is linear in the unit: solve for the largest that fits.
        _, w1, h1 = self._metrics(1.0, True)
        unit = min(self.MAX_UNIT, avail_w / w1, avail_h / h1)
        hint = unit >= 26
        if not hint:
            _, w1, h1 = self._metrics(1.0, False)
            unit = min(self.MAX_UNIT, avail_w / w1, avail_h / h1)
        unit = max(1.0, unit)
        if unit < self.MIN_UNIT:
            print(f'[osk] tile {w}x{h} is very small: keys {unit:.0f}px', flush=True)
        self._unit, self._show_hint = unit, hint
        _, kw, kh = self._metrics(unit, hint)
        kw, kh = min(int(kw), avail_w), min(int(kh), avail_h)
        self.setFixedSize(kw, kh)
        self.move(x + (w - kw) // 2, y + (h - kh) // 2)
        self.update()

    # — navigation / state —

    def move_cursor(self, direction: int):
        self._row, self._col, self._want_x = nav_target(self._row, self._col,
                                                        direction, self._want_x)
        self.update()

    def toggle_shift(self):
        self._shift = not self._shift
        self.update()

    def press_selected(self) -> str | None:
        """The keysym to send for the selected key (None: it was shift)."""
        key = KEY_ROWS[self._row][self._col]
        if key.action == 'shift':
            self.toggle_shift()
            return None
        return key.keysym(self._shift)

    # — painting —

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        u = self._unit
        pad, _, _ = self._metrics(u, self._show_hint)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        p.setPen(QPen(C_BORDER, max(1.0, u / 28)))
        p.setBrush(C_PANEL)
        p.drawRoundedRect(r, pad, pad)

        font = QFont()
        font.setBold(True)
        rad = u * 0.12
        for ri, (row, rects) in enumerate(zip(KEY_ROWS, key_rects(u))):
            for ci, (key, kr) in enumerate(zip(row, rects)):
                kr = kr.translated(pad, pad)
                sel = ri == self._row and ci == self._col
                on = key.action == 'shift' and self._shift
                p.setPen(QPen(QColor('white') if sel else C_KEY_EDGE, 2 if sel else 1))
                p.setBrush(C_SEL if sel else (C_ON if on else C_KEY))
                p.drawRoundedRect(kr, rad, rad)
                text = key.text(self._shift)
                font.setPixelSize(max(6, int(u * (0.28 if len(text) > 2 else 0.38))))
                p.setFont(font)
                p.setPen(C_TEXT)
                p.drawText(kr, Qt.AlignCenter, text)

        if self._show_hint:
            font.setBold(False)
            font.setPixelSize(max(8, int(u * 0.22)))
            p.setFont(font)
            p.setPen(C_HINT)
            hr = QRectF(pad, self.height() - pad - u * 0.3, self.width() - 2 * pad, u * 0.3)
            p.drawText(hr, Qt.AlignCenter, self.HINT)
        p.end()


# ── controller (one OSK per controller, lazy-shown) ──────────────────

class OSKController(QObject):
    """Owns the OSK windows and routes monitor signals to them.

    bindings: kernel_js_index → gamescope process PID. The tile's window is
    found by its WM_CLASS (from the process's environment) and re-resolved
    when it disappears. Without bindings (standalone) any pad summons an OSK
    over the screen and keys go to the focused window.
    """

    _TRACK_INTERVAL_MS = 125      # follow a moved/resized tile at 8 Hz
    _REPEAT_DELAY_MS = 380        # D-pad hold → repeat
    _REPEAT_MS = 85

    def __init__(self, bindings: dict[int, int], monitor: 'GamepadMonitor | None' = None):
        super().__init__()
        self._bindings = bindings
        self._monitor = monitor
        self._wmclass: dict[int, str | None] = {}
        self._wid_cache: dict[int, str] = {}
        self._windows: dict[int, OSKWindow] = {}
        self._injectors: dict[int, Injector] = {}
        self._visible: dict[int, bool] = {}
        self._last_geom: dict[int, tuple[int, int, int, int]] = {}
        self._repeat: dict[int, tuple[int, QTimer]] = {}

        self._tracker = QTimer(self)
        self._tracker.setInterval(self._TRACK_INTERVAL_MS)
        self._tracker.timeout.connect(self._tick)

    def _window(self, js_index: int) -> OSKWindow:
        if js_index not in self._windows:
            self._windows[js_index] = OSKWindow()
            self._visible[js_index] = False
        return self._windows[js_index]

    def _resolve_wid(self, js_index: int) -> str | None:
        pid = self._bindings.get(js_index)
        if pid is None:
            return None
        cached = self._wid_cache.get(js_index)
        if cached and get_window_geometry(cached) is not None:
            return cached
        if not self._wmclass.get(js_index):
            self._wmclass[js_index] = instance_wmclass(pid)
        wid = find_gamescope_window(pid, self._wmclass[js_index], timeout=1.5)
        if wid:
            self._wid_cache[js_index] = wid
        return wid

    def _injector(self, js_index: int) -> Injector:
        inj = self._injectors.get(js_index)
        wmclass = self._wmclass.get(js_index)
        wid = self._wid_cache.get(js_index)
        if inj is None or inj.wmclass != wmclass:
            if inj is not None:
                inj.close()
            inj = self._injectors[js_index] = Injector(wmclass, wid)
        inj.host_wid = wid
        return inj

    def _hide(self, js_index: int):
        if not self._visible.get(js_index):
            return
        self._stop_repeat(js_index)
        self._windows[js_index].hide()
        self._visible[js_index] = False
        self._last_geom.pop(js_index, None)
        if self._monitor is not None:
            self._monitor.ungrab(js_index)
        if not any(self._visible.values()):
            self._tracker.stop()

    def _allowed(self, js_index: int) -> bool:
        """With bindings, unbound controllers are inert (else their chord
        would pop an OSK over the whole screen)."""
        return not self._bindings or js_index in self._bindings

    def on_summon(self, js_index: int):
        if not self._allowed(js_index):
            return
        if self._visible.get(js_index):
            self._hide(js_index)
            return
        wid = self._resolve_wid(js_index)
        geom = get_window_geometry(wid) if wid else None
        if geom is None:
            if self._bindings:
                print(f'[osk] js{js_index}: tile window not found', flush=True)
                return
            s = QApplication.primaryScreen().geometry()
            geom = (s.x(), s.y(), s.width(), s.height())
        w = self._window(js_index)
        w.position_over(geom)
        w.show()
        w.raise_()
        self._visible[js_index] = True
        self._last_geom[js_index] = geom
        if self._monitor is not None:
            self._monitor.grab(js_index)
        if not self._tracker.isActive():
            self._tracker.start()

    def _tick(self):
        """Follow each visible OSK's tile; hide it if the tile is gone."""
        for js_index, visible in list(self._visible.items()):
            if not visible:
                continue
            wid = self._wid_cache.get(js_index)
            if not wid:
                continue                  # standalone: nothing to track
            geom = get_window_geometry(wid)
            if geom is None:
                self._wid_cache.pop(js_index, None)
                new = self._resolve_wid(js_index)
                geom = get_window_geometry(new) if new else None
                if geom is None:
                    print(f'[osk] js{js_index}: tile window vanished, hiding', flush=True)
                    self._hide(js_index)
                    continue
            if geom != self._last_geom.get(js_index):
                self._windows[js_index].position_over(geom)
                self._last_geom[js_index] = geom
            self._windows[js_index].raise_()

    # — D-pad with hold-to-repeat —

    def on_nav(self, js_index: int, direction: int):
        if not self._visible.get(js_index):
            return
        self._windows[js_index].move_cursor(direction)
        self._stop_repeat(js_index)
        t = QTimer(self)
        t.setSingleShot(True)

        def fire(js=js_index, d=direction, timer=t):
            if not self._visible.get(js):
                return
            self._windows[js].move_cursor(d)
            timer.start(self._REPEAT_MS)
        t.timeout.connect(fire)
        t.start(self._REPEAT_DELAY_MS)
        self._repeat[js_index] = (direction, t)

    def on_nav_up(self, js_index: int, direction: int):
        cur = self._repeat.get(js_index)
        if cur and cur[0] == direction:
            self._stop_repeat(js_index)

    def _stop_repeat(self, js_index: int):
        cur = self._repeat.pop(js_index, None)
        if cur:
            cur[1].stop()
            cur[1].deleteLater()

    # — keys —

    def _send(self, js_index: int, keysym: str):
        self._injector(js_index).tap(keysym)
        self._windows[js_index].raise_()

    def on_select(self, js_index: int):
        if not self._visible.get(js_index):
            return
        keysym = self._windows[js_index].press_selected()
        if keysym:
            self._send(js_index, keysym)

    def on_cancel(self, js_index: int):
        self._hide(js_index)

    def on_shift(self, js_index: int):
        if self._visible.get(js_index):
            self._windows[js_index].toggle_shift()

    def _on_named(self, js_index: int, keysym: str):
        if self._visible.get(js_index):
            self._send(js_index, keysym)

    def on_space(self, js_index: int):
        self._on_named(js_index, 'space')

    def on_backspace(self, js_index: int):
        self._on_named(js_index, 'BackSpace')

    def on_enter(self, js_index: int):
        self._on_named(js_index, 'Return')


# ── entry point ──────────────────────────────────────────────────────

def _parse_bindings(specs: list[str]) -> dict[int, int]:
    """Parse '0:12345' (js_index:gamescope_pid) into {js_index: pid}."""
    out: dict[int, int] = {}
    for spec in specs:
        js_str, _, pid_str = spec.partition(':')
        try:
            js_index, pid = int(js_str), int(pid_str)
        except ValueError:
            print(f'[osk] bad --bind {spec!r}, expected js:pid', file=sys.stderr)
            continue
        wmclass = instance_wmclass(pid)
        wid = find_gamescope_window(pid, wmclass, timeout=2)
        print(f'[osk] js{js_index} → pid {pid} ({wmclass or "no wmclass"}, '
              f'window {wid or "not found yet"})', flush=True)
        out[js_index] = pid
    return out


def main():
    parser = argparse.ArgumentParser(description='On-screen keyboard for Janus')
    parser.add_argument(
        '--bind', action='append', default=[], metavar='JS:PID',
        help='Map a controller index to a gamescope process. Repeatable.',
    )
    args = parser.parse_args()

    # The tiles are XWayland windows; only as an X client can the OSK sit at
    # a given position above them (override-redirect). See the module doc.
    if os.environ.get('DISPLAY'):
        os.environ['QT_QPA_PLATFORM'] = 'xcb'

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    print(f'[osk] Qt platform: {app.platformName()}', flush=True)

    bindings = _parse_bindings(args.bind)
    if not bindings:
        print(f'[osk] standalone mode — {len(find_gamepads())} controller(s); '
              f'OSK targets the focused window', flush=True)

    mon = GamepadMonitor()
    ctrl = OSKController(bindings, monitor=mon)
    for sig, slot in ((mon.summon, ctrl.on_summon), (mon.nav, ctrl.on_nav),
                      (mon.nav_up, ctrl.on_nav_up), (mon.select, ctrl.on_select),
                      (mon.cancel, ctrl.on_cancel), (mon.shift, ctrl.on_shift),
                      (mon.space, ctrl.on_space), (mon.backspace, ctrl.on_backspace),
                      (mon.enter, ctrl.on_enter)):
        sig.connect(slot, Qt.QueuedConnection)
    mon.start()

    if bindings:
        # Exit with the games: the launcher stops us at session end, but not
        # if it dies first, and a leftover child keeps the Steam shortcut
        # that started Janus "running".
        def _alive(pid):
            try:     # a zombie (exited, not yet reaped) counts as gone
                with open(f'/proc/{pid}/stat') as f:
                    return f.read().rsplit(')', 1)[1].split()[0] != 'Z'
            except (OSError, IndexError):
                return False

        def _quit_when_games_gone():
            if not any(_alive(pid) for pid in bindings.values()):
                print('[osk] bound games exited, quitting', flush=True)
                app.quit()
        games = QTimer()
        games.timeout.connect(_quit_when_games_gone)
        games.start(2000)

    print('[osk] ready. Press L3+R3 on a controller to summon.', flush=True)
    sys.exit(app.exec())


if __name__ == '__main__':
    main()

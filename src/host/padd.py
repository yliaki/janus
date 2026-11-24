#!/usr/bin/env python3
"""Janus input daemon: one virtual Xbox 360 pad per player (Janus input mode).

Each player's physical pad is read through SDL's GameController API (its
mapping database, Steam's and Janus's own mappings: pad_mappings.py) and
written to a uinput pad that is byte-for-byte xpad's wired Xbox 360 pad
(USB 045e:028e v0x0110, "Microsoft X-Box 360 pad", phys "janus/p<N>"). Every
game sees an Xbox pad, and each instance runs in a private /dev that holds
only its own one (input_ns.py). NOTES.md "Janus-owned input isolation".

Two processes, so a crash in the SDL side never costs a game its pad:

  serve   the holder: creates the virtual pads and keeps their fds for the
          whole session (a uinput device lives as long as any fd to it is
          open), runs `remap` with those fds and restarts it if it dies.
          Exits when the session process that started it is gone.
  remap   SDL → virtual pads: grabs the physical pads (EVIOCGRAB, so Steam's
          desktop layout and other apps don't see them), forwards rumble,
          re-attaches a pad that reconnects by its hardware identity,
          Nintendo pads stuck in simple HID mode are read from hidraw.
  probe   prints every pad SDL sees as JSON (with Janus's mappings): which
          ones it can map (pad_mappings.unrecognised()).

Library side: PadDaemon (start a holder for a session, get the nodes).
"""

import ctypes
import ctypes.util
import fcntl
import json
import os
import select
import signal
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

JANUS_PHYS_PREFIX = 'janus/'
RUN_ROOT = Path.home() / '.local' / 'share' / 'janus' / 'run'
LOG_ROOT = Path.home() / '.local' / 'share' / 'janus' / 'logs'


def _log(*a):
    print(f'[padd {time.strftime("%H:%M:%S")}]', *a, flush=True)


# --- uinput ------------------------------------------------------------------

def _IOC(d, t, nr, size):
    return (d << 30) | (size << 16) | (ord(t) << 8) | nr


UI_SET_EVBIT, UI_SET_KEYBIT, UI_SET_ABSBIT, UI_SET_FFBIT = (
    _IOC(1, 'U', n, 4) for n in (100, 101, 103, 107))
UI_SET_PHYS = _IOC(1, 'U', 108, 8)
UI_DEV_SETUP, UI_ABS_SETUP = _IOC(1, 'U', 3, 92), _IOC(1, 'U', 4, 28)
UI_DEV_CREATE = 0x5501
_FF_UPLOAD_SIZE = 4 + 4 + 48 + 48            # struct uinput_ff_upload (x86-64)
UI_BEGIN_FF_UPLOAD = _IOC(3, 'U', 200, _FF_UPLOAD_SIZE)
UI_END_FF_UPLOAD = _IOC(1, 'U', 201, _FF_UPLOAD_SIZE)
UI_BEGIN_FF_ERASE, UI_END_FF_ERASE = _IOC(3, 'U', 202, 12), _IOC(1, 'U', 203, 12)
EVIOCGRAB = _IOC(1, 'E', 0x90, 4)
EV_SYN, EV_KEY, EV_ABS, EV_FF, EV_UINPUT = 0, 1, 3, 0x15, 0x0101
FF_RUMBLE = 0x50
UI_FF_UPLOAD, UI_FF_ERASE = 1, 2
_EVENT = struct.Struct('llHHi')

# xpad's wired Xbox 360 pad
XPAD_KEYS = (0x130, 0x131, 0x133, 0x134, 0x136, 0x137, 0x13a, 0x13b, 0x13c, 0x13d, 0x13e)
XPAD_ABS = ((0x00, -32768, 32767, 16, 128), (0x01, -32768, 32767, 16, 128),
            (0x03, -32768, 32767, 16, 128), (0x04, -32768, 32767, 16, 128),
            (0x02, 0, 255, 0, 0), (0x05, 0, 255, 0, 0),
            (0x10, -1, 1, 0, 0), (0x11, -1, 1, 0, 0))
XPAD_ID = (0x03, 0x045e, 0x028e, 0x0110)     # bus USB, vid, pid, version
XPAD_NAME = b'Microsoft X-Box 360 pad'


def create_xbox_pad(tag: str):
    """A new uinput Xbox 360 pad; returns (fd, [/dev/input nodes])."""
    fd = os.open('/dev/uinput', os.O_RDWR | os.O_NONBLOCK)
    try:
        for ev in (EV_KEY, EV_ABS, EV_FF):
            fcntl.ioctl(fd, UI_SET_EVBIT, ev)
        for code in XPAD_KEYS:
            fcntl.ioctl(fd, UI_SET_KEYBIT, code)
        for code, lo, hi, fuzz, flat in XPAD_ABS:
            fcntl.ioctl(fd, UI_SET_ABSBIT, code)
            fcntl.ioctl(fd, UI_ABS_SETUP, struct.pack('HH6i', code, 0, 0, lo, hi, fuzz, flat, 0))
        fcntl.ioctl(fd, UI_SET_FFBIT, FF_RUMBLE)
        fcntl.ioctl(fd, UI_SET_PHYS, (JANUS_PHYS_PREFIX + tag).encode() + b'\0')
        fcntl.ioctl(fd, UI_DEV_SETUP, struct.pack('HHHH80sI', *XPAD_ID, XPAD_NAME, 16))
        fcntl.ioctl(fd, UI_DEV_CREATE)
        return fd, uinput_nodes(fd)
    except OSError:
        os.close(fd)
        raise


def uinput_nodes(fd: int) -> List[str]:
    buf = bytearray(64)
    fcntl.ioctl(fd, _IOC(2, 'U', 44, 64), buf)          # UI_GET_SYSNAME
    sysname = buf.split(b'\0')[0].decode()
    base = f'/sys/devices/virtual/input/{sysname}'
    for _ in range(100):                                 # handlers appear async
        try:
            names = [n for n in os.listdir(base) if n.startswith(('event', 'js'))]
        except OSError:
            names = []
        if any(n.startswith('event') for n in names) and any(n.startswith('js') for n in names):
            break
        time.sleep(0.02)
    return sorted('/dev/input/' + n for n in names)


def wait_accessible(nodes: List[str], timeout: float = 5.0) -> bool:
    """Until udev has given the nodes their ACL (uaccess) and they open."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if all(os.access(n, os.R_OK) for n in nodes):
            return True
        time.sleep(0.05)
    return False


class Output:
    """Writing side of a virtual Xbox pad (fd from the holder)."""

    # SDL_GameControllerButton -> xpad key
    BUTTONS = {0: 0x130, 1: 0x131, 2: 0x133, 3: 0x134, 4: 0x13a, 5: 0x13c, 6: 0x13b,
               7: 0x13d, 8: 0x13e, 9: 0x136, 10: 0x137}
    DPAD = {11: (0x11, -1), 12: (0x11, 1), 13: (0x10, -1), 14: (0x10, 1)}
    STICKS = {0: 0x00, 1: 0x01, 2: 0x03, 3: 0x04}       # SDL axis -> ABS
    TRIGGERS = {4: 0x02, 5: 0x05}

    def __init__(self, fd: int):
        self.fd = fd
        self.effects: Dict[int, tuple] = {}
        self.hat = {0x10: set(), 0x11: set()}
        self.rumble = None          # callback(strong, weak, ms) of the attached source
        self.state: Dict[tuple, int] = {}   # (type, code) -> value last written

    def write(self, events):
        now = time.time()
        s, us = int(now), int((now % 1) * 1e6)
        data = b''.join(_EVENT.pack(s, us, t, c, v) for t, c, v in events)
        for t, c, v in events:
            self.state[(t, c)] = v
        try:
            os.write(self.fd, data + _EVENT.pack(s, us, EV_SYN, 0, 0))
        except BlockingIOError:
            pass

    def neutral(self):
        self.hat = {0x10: set(), 0x11: set()}
        self.write([(EV_KEY, c, 0) for c in XPAD_KEYS] +
                   [(EV_ABS, c, 0) for c, *_ in XPAD_ABS])

    def button_events(self, b: int, down: bool):
        if b in self.BUTTONS:
            return [(EV_KEY, self.BUTTONS[b], int(bool(down)))]
        if b in self.DPAD:
            axis, sign = self.DPAD[b]
            (self.hat[axis].add if down else self.hat[axis].discard)(sign)
            h = self.hat[axis]
            return [(EV_ABS, axis, next(iter(h)) if len(h) == 1 else 0)]
        return []

    def axis_events(self, a: int, v: int):
        if a in self.STICKS:
            return [(EV_ABS, self.STICKS[a], v)]
        if a in self.TRIGGERS:
            return [(EV_ABS, self.TRIGGERS[a], max(0, v) * 255 // 32767)]
        return []

    def ff_pump(self):
        """Force-feedback traffic from the game: uploads/erases answered here,
        plays forwarded to the source pad."""
        while True:
            try:
                data = os.read(self.fd, _EVENT.size * 16)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return
            if not data:
                return
            for off in range(0, len(data) - _EVENT.size + 1, _EVENT.size):
                _, _, t, code, val = _EVENT.unpack_from(data, off)
                try:
                    self._ff_event(t, code, val)
                except OSError as e:
                    _log(f'force feedback: {e}')

    def _ff_event(self, t, code, val):
        if t == EV_UINPUT and code == UI_FF_UPLOAD:
            req = bytearray(struct.pack('Ii', val, 0) + bytes(96))
            fcntl.ioctl(self.fd, UI_BEGIN_FF_UPLOAD, req)
            etype, eid, _d, _tb, _ti, length, _delay = struct.unpack_from('HhHHHHH', req, 8)
            strong, weak = struct.unpack_from('HH', req, 8 + 16)
            if etype == FF_RUMBLE:
                self.effects[eid] = (strong, weak, length)
            struct.pack_into('i', req, 4, 0)
            fcntl.ioctl(self.fd, UI_END_FF_UPLOAD, bytes(req))
        elif t == EV_UINPUT and code == UI_FF_ERASE:
            req = bytearray(struct.pack('IiI', val, 0, 0))
            fcntl.ioctl(self.fd, UI_BEGIN_FF_ERASE, req)
            self.effects.pop(struct.unpack_from('I', req, 8)[0], None)
            fcntl.ioctl(self.fd, UI_END_FF_ERASE, bytes(req))
        elif t == EV_FF and code in self.effects and self.rumble is not None:
            strong, weak, length = self.effects[code]
            if val:
                self.rumble(strong, weak, length or 0xFFFF)
            else:
                self.rumble(0, 0, 0)


# --- SDL ---------------------------------------------------------------------

SDL_INIT_GAMECONTROLLER = 0x2000
CONTROLLERAXISMOTION, CONTROLLERBUTTONDOWN, CONTROLLERBUTTONUP = 0x650, 0x651, 0x652
CONTROLLERDEVICEADDED, CONTROLLERDEVICEREMOVED = 0x653, 0x654
JOYDEVICEADDED = 0x605


class SDLEvent(ctypes.Union):
    _fields_ = [('type', ctypes.c_uint32), ('raw', ctypes.c_uint8 * 56)]

    def i32(self, off):
        return int.from_bytes(bytes(self.raw[off:off + 4]), 'little', signed=True)

    def i16(self, off):
        return int.from_bytes(bytes(self.raw[off:off + 2]), 'little', signed=True)


class SDLGUID(ctypes.Structure):
    _fields_ = [('data', ctypes.c_uint8 * 16)]


def load_sdl():
    """System libSDL2 via ctypes (libSDL3 first: sdl2-compat exits the
    process if it can't load it — see bigpicture/gamepad.py)."""
    for name in (ctypes.util.find_library('SDL3'), 'libSDL3.so.0'):
        if name:
            try:
                ctypes.CDLL(name, mode=ctypes.RTLD_GLOBAL)
                break
            except OSError:
                pass
    lib = ctypes.CDLL(ctypes.util.find_library('SDL2-2.0') or 'libSDL2-2.0.so.0')
    P, I, C = ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p
    sig = {
        'SDL_SetHint': (I, [C, C]), 'SDL_Init': (I, [ctypes.c_uint32]),
        'SDL_Quit': (None, []), 'SDL_GetError': (C, []),
        'SDL_NumJoysticks': (I, []), 'SDL_IsGameController': (I, [I]),
        'SDL_JoystickNameForIndex': (C, [I]), 'SDL_JoystickPathForIndex': (C, [I]),
        'SDL_JoystickGetDeviceInstanceID': (ctypes.c_int32, [I]),
        'SDL_JoystickGetDeviceVendor': (ctypes.c_uint16, [I]),
        'SDL_JoystickGetDeviceProduct': (ctypes.c_uint16, [I]),
        'SDL_JoystickGetDeviceGUID': (SDLGUID, [I]),
        'SDL_JoystickGetGUIDString': (None, [SDLGUID, C, I]),
        'SDL_GameControllerOpen': (P, [I]), 'SDL_GameControllerClose': (None, [P]),
        'SDL_GameControllerName': (C, [P]),
        'SDL_GameControllerGetButton': (ctypes.c_uint8, [P, I]),
        'SDL_GameControllerGetAxis': (ctypes.c_int16, [P, I]),
        'SDL_GameControllerRumble': (I, [P, ctypes.c_uint16, ctypes.c_uint16, ctypes.c_uint32]),
        'SDL_GameControllerHasRumble': (I, [P]),
        'SDL_GameControllerEventState': (I, [I]),
        'SDL_PollEvent': (I, [ctypes.POINTER(SDLEvent)]),
        'SDL_PumpEvents': (None, []),
        'SDL_GameControllerAddMapping': (I, [C]),
    }
    for fn, (res, args) in sig.items():
        f = getattr(lib, fn)
        f.restype, f.argtypes = res, args
    return lib


def init_sdl(sdl):
    # evdev only: SDL's HIDAPI drivers talk to the pad over hidraw (handshake,
    # subcommands) and take it away from Steam. NOTES.md "Gamepads without a
    # js node".
    for k, v in ((b'SDL_JOYSTICK_HIDAPI', b'0'), (b'SDL_NO_SIGNAL_HANDLERS', b'1'),
                 (b'SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS', b'1'),
                 (b'SDL_GAMECONTROLLER_ALLOW_STEAM_VIRTUAL_GAMEPAD', b'0')):
        sdl.SDL_SetHint(k, v)
    # Steam's environment can carry pad filters meant for its games.
    for var in ('SDL_GAMECONTROLLER_IGNORE_DEVICES', 'SDL_GAMECONTROLLER_IGNORE_DEVICES_EXCEPT',
                'SDL_GAMECONTROLLERCONFIG', 'SteamVirtualGamepadInfo'):
        os.environ.pop(var, None)
    # The extra mappings go in before SDL_Init: a pad that only they make a
    # controller is enumerated at init, and SDL sends no CONTROLLERDEVICEADDED
    # for it when its mapping is added afterwards.
    import pad_mappings
    extra = list(pad_mappings.janus_mappings().values())
    if extra:
        sdl.SDL_SetHint(b'SDL_GAMECONTROLLERCONFIG', '\n'.join(extra).encode())
    if sdl.SDL_Init(SDL_INIT_GAMECONTROLLER) != 0:
        raise OSError(f'SDL_Init: {sdl.SDL_GetError().decode()}')
    pad_mappings.apply_to_sdl(sdl)


def sdl_guid(sdl, index: int) -> str:
    buf = ctypes.create_string_buffer(33)
    sdl.SDL_JoystickGetGUIDString(sdl.SDL_JoystickGetDeviceGUID(index), buf, 33)
    return buf.value.decode()


# --- device identity ------------------------------------------------------------

def _sysfs(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ''


def node_identity(node: str) -> dict:
    """vid/pid/uniq/phys/name of /dev/input/eventN, from sysfs."""
    dev = f'/sys/class/input/{os.path.basename(node)}/device'
    try:
        vid = int(_sysfs(f'{dev}/id/vendor') or '0', 16)
        pid = int(_sysfs(f'{dev}/id/product') or '0', 16)
    except ValueError:
        vid = pid = 0
    return {'node': node, 'vid': vid, 'pid': pid, 'uniq': _sysfs(f'{dev}/uniq'),
            'phys': _sysfs(f'{dev}/phys'), 'name': _sysfs(f'{dev}/name')}


def hidraw_of(node: str) -> str:
    """/dev/hidrawN of the HID device behind /dev/input/eventN, '' if none."""
    try:
        dev = os.path.realpath(f'/sys/class/input/{os.path.basename(node)}/device')
        hid = os.path.dirname(os.path.dirname(dev))
        for h in sorted(os.listdir(f'{hid}/hidraw')):
            if h.startswith('hidraw'):
                return f'/dev/{h}'
    except OSError:
        pass
    return ''


def pick_player(players: List[dict], ident: dict) -> Optional[dict]:
    """Which free player a newly seen pad belongs to (None: nobody's).

    At session start a player takes exactly the pad it was given (its event
    node). Afterwards a pad that comes back is recognised by serial/MAC, else
    by its USB port, else — a serial-less pad on another port — it takes the
    first orphaned player of the same model (only players that already had a
    pad: a stranger's identical pad can't take a seat at start).

    A pad that comes back on the same USB port as another model is the same
    pad in another mode: some pads re-enumerate on a replug (MACHENIKE G3V2:
    36e8:3001, back as an Xbox 360 pad 045e:028e). Only for USB ports: a
    Bluetooth phys is the adapter's address, the same for every pad."""
    orphans = [p for p in players if p.get('attached') is None]
    free = [p for p in orphans if (p['vid'], p['pid']) == (ident['vid'], ident['pid'])]
    if not free:
        if ident['phys'].startswith('usb-'):
            for p in orphans:
                if p.get('seen') and p['phys'] == ident['phys']:
                    return p
        return None
    for p in free:
        if not p.get('seen') and p['node'] == ident['node']:
            return p
    if ident['uniq']:
        for p in free:
            if p['uniq'] and p['uniq'] == ident['uniq']:
                return p
    if ident['phys']:
        for p in free:
            if p['phys'] and p['phys'] == ident['phys']:
                return p
    if not ident['uniq']:
        for p in free:
            if p.get('seen') and not p['uniq']:
                return p
    return None


# --- Nintendo pads in simple HID mode (hidraw) ------------------------------

NINTENDO_VID = 0x057e
# 0x3f "simple HID" report, by label (the button printed A = Xbox A, as SDL's
# default for Nintendo pads): byte1 A B X Y L R ZL ZR, byte2 - + LS RS Home
# Capture, byte3 hat, then 4 x 16-bit sticks (0x8000 = centre).
# Verified on a real Pro Controller for Big Picture (bigpicture/gamepad.py).
_SIMPLE_BUTTONS = {0: 0, 1: 1, 2: 2, 3: 3, 4: 9, 5: 10, 8: 4, 9: 6, 10: 7, 11: 8, 12: 5}
_SIMPLE_HAT = {0: (0, -1), 1: (1, -1), 2: (1, 0), 3: (1, 1), 4: (0, 1),
               5: (-1, 1), 6: (-1, 0), 7: (-1, -1)}


def parse_simple_report(r: bytes) -> Optional[dict]:
    """A Switch pad's 0x3f report -> {'buttons': {sdl button: bool},
    'hat': (x, y), 'axes': {sdl axis: value}}; None for other reports
    (0x30 full reports mean hid-nintendo is working: evdev carries them)."""
    if len(r) < 12 or r[0] != 0x3F:
        return None
    bits = r[1] | (r[2] << 8)
    buttons = {sdl: bool(bits & (1 << b)) for b, sdl in _SIMPLE_BUTTONS.items()}

    def axis(off):
        v = int.from_bytes(r[off:off + 2], 'little') - 0x8000
        return max(-32768, min(32767, v))
    axes = {0: axis(4), 1: axis(6), 2: axis(8), 3: axis(10),
            4: 32767 if bits & (1 << 6) else 0, 5: 32767 if bits & (1 << 7) else 0}
    return {'buttons': buttons, 'hat': _SIMPLE_HAT.get(r[3], (0, 0)), 'axes': axes}


# --- remapper ------------------------------------------------------------------

class Remapper:
    def __init__(self, config: dict, fds: Dict[int, int], status_path: Optional[str]):
        import pad_mappings
        self.status_path = status_path
        self.players = []
        for p in config['players']:
            out = Output(fds[p['player']])
            self.players.append(dict(p, out=out, attached=None, seen=False, gc=None,
                                     iid=None, hid_fd=None, swap=pad_mappings.positional(
                                         p['vid'], p['pid'])))
        self.by_iid: Dict[int, dict] = {}
        self.sdl = load_sdl()
        init_sdl(self.sdl)
        self.sdl.SDL_GameControllerEventState(1)
        self.own_nodes = {n for p in config['players'] for n in p.get('outputs', [])}
        self.sdl_fds: List[int] = []
        self._write_status()
        # The pads already there (not left to SDL's ADDED events).
        for i in range(self.sdl.SDL_NumJoysticks()):
            if self.sdl.SDL_IsGameController(i):
                self.attach(i)

    # status for the UIs (pad attached or waiting for its reconnect)
    def _write_status(self):
        if not self.status_path:
            return
        data = {'pid': os.getpid(), 'players': {
            str(p['player']): {'attached': p['attached'] is not None,
                               'source': p['attached'] or '', 'via': p.get('via', ''),
                               'name': p['name']} for p in self.players}}
        tmp = self.status_path + '.tmp'
        try:
            with open(tmp, 'w') as f:
                json.dump(data, f)
            os.replace(tmp, self.status_path)
        except OSError:
            pass

    def _refresh_sdl_fds(self):
        """The evdev fds SDL holds (what select() waits on)."""
        fds = []
        for fd in os.listdir('/proc/self/fd'):
            try:
                if os.readlink(f'/proc/self/fd/{fd}').startswith('/dev/input/event'):
                    fds.append(int(fd))
            except OSError:
                pass
        self.sdl_fds = fds

    def _grab(self, path: str):
        """EVIOCGRAB the fd SDL opened for *path*: nobody else (Steam's
        desktop layout, other apps) reads the pad meanwhile. Through SDL's
        own fd — a second fd's grab would starve SDL."""
        try:
            rdev = os.stat(path).st_rdev
        except OSError:
            return
        for fd in os.listdir('/proc/self/fd'):
            try:
                if (os.readlink(f'/proc/self/fd/{fd}') == path
                        and os.fstat(int(fd)).st_rdev == rdev):
                    fcntl.ioctl(int(fd), EVIOCGRAB, 1)
                    return
            except OSError as e:
                _log(f'grab {path}: {e}')

    def attach(self, index: int):
        sdl = self.sdl
        path = (sdl.SDL_JoystickPathForIndex(index) or b'').decode()
        if not path or path in self.own_nodes:
            return
        ident = node_identity(path)
        if ident['phys'].startswith(JANUS_PHYS_PREFIX) or ident['vid'] == 0x28de:
            return
        iid = sdl.SDL_JoystickGetDeviceInstanceID(index)
        if iid in self.by_iid:
            return
        p = pick_player(self.players, ident)
        if p is None:
            return
        gc = sdl.SDL_GameControllerOpen(index)
        if not gc:
            _log(f'P{p["player"]}: SDL could not open {path}: {sdl.SDL_GetError().decode()}')
            return
        if (p['vid'], p['pid']) != (ident['vid'], ident['pid']):
            _log(f'P{p["player"]}: same USB port, now {ident["vid"]:04x}:{ident["pid"]:04x} '
                 f'(was {p["vid"]:04x}:{p["pid"]:04x}): the pad changed mode')
        p.update(gc=gc, iid=iid, attached=path, seen=True, via='sdl',
                 vid=ident['vid'], pid=ident['pid'],
                 uniq=p['uniq'] or ident['uniq'], phys=ident['phys'] or p['phys'], node=path)
        self.by_iid[iid] = p
        p['out'].rumble = lambda s, w, ms, gc=gc: sdl.SDL_GameControllerRumble(gc, s, w, ms)
        self._grab(path)
        if ident['vid'] == NINTENDO_VID:
            self._open_hidraw(p, path)
        self._sync(p)
        self._refresh_sdl_fds()
        _log(f'P{p["player"]}: attached {path} ({sdl.SDL_GameControllerName(gc).decode()})')
        self._write_status()

    def _open_hidraw(self, p, path):
        hr = hidraw_of(path)
        if not hr:
            return
        try:
            p['hid_fd'] = os.open(hr, os.O_RDONLY | os.O_NONBLOCK)
            p['hid_state'] = None
        except OSError as e:
            _log(f'P{p["player"]}: {hr}: {e} (simple-mode fallback off)')

    def detach(self, iid: int):
        p = self.by_iid.pop(iid, None)
        if p is None:
            return
        self.sdl.SDL_GameControllerClose(p['gc'])
        if p.get('hid_fd') is not None:
            os.close(p['hid_fd'])
            p['hid_fd'] = None
        p.update(gc=None, iid=None, attached=None)
        p['out'].rumble = None
        p['out'].neutral()
        self._refresh_sdl_fds()
        _log(f'P{p["player"]}: pad gone; its Xbox pad stays (neutral) until it is back')
        self._write_status()

    def _btn(self, p, b):
        return {0: 1, 1: 0, 2: 3, 3: 2}.get(b, b) if p['swap'] else b

    def _sync(self, p):
        """Write the attached pad's whole current state (after (re)attach)."""
        sdl, gc, out = self.sdl, p['gc'], p['out']
        out.neutral()
        ev = []
        for b in range(15):
            if sdl.SDL_GameControllerGetButton(gc, b):
                ev += out.button_events(self._btn(p, b), True)
        for a in range(6):
            ev += out.axis_events(a, sdl.SDL_GameControllerGetAxis(gc, a))
        if ev:
            out.write(ev)

    def _desired(self, p) -> Dict[tuple, int]:
        """The attached pad's whole current state as Xbox events, from SDL."""
        sdl, gc, out = self.sdl, p['gc'], p['out']
        want = {(EV_KEY, c): 0 for c in XPAD_KEYS}
        for b in range(11):
            if sdl.SDL_GameControllerGetButton(gc, b):
                for t, c, v in out.button_events(self._btn(p, b), True):
                    want[(t, c)] = v
        get = lambda b: bool(sdl.SDL_GameControllerGetButton(gc, b))
        want[(EV_ABS, 0x10)] = get(14) - get(13)        # D-pad as xpad's hat
        want[(EV_ABS, 0x11)] = get(12) - get(11)
        for a in range(6):
            for t, c, v in out.axis_events(a, sdl.SDL_GameControllerGetAxis(gc, a)):
                want[(t, c)] = v
        return want

    def _reconcile(self):
        """While the pads are quiet: make each virtual pad match its source.
        Events are written as they come (so quick taps survive); this catches
        the ones SDL never sends — a control already held when SDL opened the
        pad gets no "released" event (seen as a D-pad stuck after a remapper
        restart)."""
        for p in self.players:
            if p['gc'] is None or p.get('via') == 'hidraw':
                continue
            out = p['out']
            want = self._desired(p)
            diff = [(t, c, v) for (t, c), v in want.items() if out.state.get((t, c), 0) != v]
            if diff:
                out.hat = {0x10: {want[(EV_ABS, 0x10)]} - {0},
                           0x11: {want[(EV_ABS, 0x11)]} - {0}}
                out.write(diff)

    def _hid_read(self, p):
        while True:
            try:
                r = os.read(p['hid_fd'], 64)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                os.close(p['hid_fd'])
                p['hid_fd'] = None
                return
            st = parse_simple_report(r)
            if st is None:
                continue
            if p.get('via') != 'hidraw':
                p['via'] = 'hidraw'
                _log(f'P{p["player"]}: pad in simple HID mode, reading its hidraw')
                self._write_status()
            out, old = p['out'], p.get('hid_state')
            ev = []
            for b, down in st['buttons'].items():
                if old is None or old['buttons'].get(b) != down:
                    ev += out.button_events(self._btn(p, b), down)
            if old is None or old['hat'] != st['hat']:
                ev += [(EV_ABS, 0x10, st['hat'][0]), (EV_ABS, 0x11, st['hat'][1])]
            for a, v in st['axes'].items():
                if old is None or old['axes'].get(a) != v:
                    ev += out.axis_events(a, v)
            p['hid_state'] = st
            if ev:
                out.write(ev)

    def run(self):
        sdl, ev = self.sdl, SDLEvent()
        outs = {p['out'].fd: p['out'] for p in self.players}
        last_pump = 0.0
        while True:
            hid = {p['hid_fd']: p for p in self.players if p.get('hid_fd') is not None}
            try:
                ready, _, _ = select.select(list(outs) + list(hid) + self.sdl_fds, [], [], 0.25)
            except OSError:            # an fd SDL closed under us
                self._refresh_sdl_fds()
                continue
            for fd in ready:
                if fd in outs:
                    outs[fd].ff_pump()
                elif fd in hid:
                    self._hid_read(hid[fd])
            if not ready or any(fd in self.sdl_fds for fd in ready) \
                    or time.monotonic() - last_pump > 0.25:
                last_pump = time.monotonic()
                self._drain(ev)
            if not ready:
                self._reconcile()

    def _drain(self, ev):
        sdl = self.sdl
        while sdl.SDL_PollEvent(ctypes.byref(ev)):
            t = ev.type
            if t == CONTROLLERDEVICEADDED:
                self.attach(ev.i32(8))
            elif t == JOYDEVICEADDED and sdl.SDL_IsGameController(ev.i32(8)):
                self.attach(ev.i32(8))
            elif t == CONTROLLERDEVICEREMOVED:
                self.detach(ev.i32(8))
            elif t in (CONTROLLERBUTTONDOWN, CONTROLLERBUTTONUP):
                p = self.by_iid.get(ev.i32(8))
                if p and p.get('via') != 'hidraw':
                    p['out'].write(p['out'].button_events(self._btn(p, ev.raw[12]), ev.raw[13]))
            elif t == CONTROLLERAXISMOTION:
                p = self.by_iid.get(ev.i32(8))
                if p and p.get('via') != 'hidraw':
                    p['out'].write(p['out'].axis_events(ev.raw[12], ev.i16(16)))


def cmd_remap(args):
    # Die with the holder: it owns the session (a lone remapper would keep
    # the pads' fds, so the pads, alive forever).
    holder = os.getppid()
    try:
        ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGTERM)   # PR_SET_PDEATHSIG
    except (OSError, AttributeError):
        pass
    if os.getppid() != holder:
        sys.exit(0)
    config = json.loads(Path(args.config).read_text())
    fds = {int(k): int(v) for k, v in
           (x.split(':') for x in os.environ['JANUS_PADD_FDS'].split(',') if x)}
    for fd in fds.values():
        os.set_blocking(fd, False)
    Remapper(config, fds, args.status).run()


# --- holder ------------------------------------------------------------------------

def cmd_serve(args):
    """Create the virtual pads, hand them to `remap`, keep them for the session."""
    config = json.loads(Path(args.config).read_text())
    parent = os.getppid()
    fds: Dict[int, int] = {}

    def add(p):
        fd, nodes = create_xbox_pad(f'p{p["player"]}')
        fds[p['player']] = fd
        p['outputs'] = nodes
        _log(f'P{p["player"]}: virtual Xbox pad {" ".join(nodes)} '
             f'for {p["name"]} ({p["node"]})')
    for p in config['players']:
        add(p)
    wait_accessible([n for p in config['players'] for n in p['outputs']])
    # The remapper skips these nodes (not some player's physical pad).
    Path(args.config).write_text(json.dumps(config))
    rev = 0

    def change_players():
        """Dynamic players: the session's new player list is in the request
        file. New players get a virtual pad, gone ones lose theirs (their
        physical pad is released: it can join again)."""
        nonlocal rev
        try:
            req = json.loads(Path(args.config + '.request').read_text())
        except (OSError, ValueError) as e:
            _log(f'player change: {e}')
            return
        want = {int(p['player']): p for p in req['players']}
        keep = [p for p in config['players'] if p['player'] in want]
        for p in config['players']:
            if p['player'] not in want:
                os.close(fds.pop(p['player']))
                _log(f'P{p["player"]}: left; virtual pad removed, {p["name"]} released')
        have = {p['player'] for p in keep}
        new = [dict(p) for n, p in want.items() if n not in have]
        for p in new:
            try:
                add(p)
            except OSError as e:
                _log(f'P{p["player"]}: no virtual pad: {e}')
                continue
            keep.append(p)
        wait_accessible([n for p in new for n in p.get('outputs', [])])
        config['players'] = keep
        Path(args.config).write_text(json.dumps(config))
        rev = int(req.get('rev', rev + 1))
        write_state()

    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    reload = False

    def hup(*_):
        nonlocal reload
        reload = True
    signal.signal(signal.SIGHUP, hup)
    players_changed = False

    def usr1(*_):
        nonlocal players_changed
        players_changed = True
    signal.signal(signal.SIGUSR1, usr1)

    def write_state():
        state = {'pid': os.getpid(), 'rev': rev,
                 'players': {str(p['player']): p['outputs'] for p in config['players']}}
        tmp = args.state + '.tmp'
        Path(tmp).write_text(json.dumps(state))
        os.replace(tmp, args.state)
    write_state()

    child, started, backoff, restarting = None, 0.0, 0.2, False
    while not stopping:
        if players_changed:
            players_changed = False
            change_players()
            if child is not None:       # the remapper takes the new set
                restarting = True
                child.terminate()
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
                child = None
                restarting = False
        if child is None:
            env = dict(os.environ, JANUS_PADD_FDS=','.join(
                f'{k}:{v}' for k, v in fds.items()))
            child = subprocess.Popen(
                [sys.executable, __file__, 'remap', '--config', args.config,
                 '--status', args.status], pass_fds=list(fds.values()), env=env)
            started = time.monotonic()
        try:
            rc = child.wait(timeout=0.1)     # a dead remapper is replaced at once
        except subprocess.TimeoutExpired:
            rc = None
        if os.getppid() != parent:
            _log('session process gone; removing the virtual pads')
            break
        if reload and rc is None:
            reload, restarting = False, True
            _log('reloading mappings: restarting the remapper')
            child.terminate()
            continue
        if rc is not None:
            child = None
            if stopping:
                break
            if restarting:              # our own restart: straight away
                restarting = False
                continue
            if time.monotonic() - started > 2:
                backoff = 0.2           # it had been running: not a crash loop
            _log(f'remapper exited ({rc}); restarting in {backoff:g}s')
            time.sleep(backoff)
            backoff = min(backoff * 2, 5.0)
    if child is not None:
        child.terminate()
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            child.kill()
    for fd in fds.values():
        os.close(fd)                    # destroys the virtual pads
    _log('stopped')


def cmd_probe(args):
    sdl = load_sdl()
    init_sdl(sdl)
    sdl.SDL_PumpEvents()
    out = []
    for i in range(sdl.SDL_NumJoysticks()):
        out.append({'path': (sdl.SDL_JoystickPathForIndex(i) or b'').decode(),
                    'name': (sdl.SDL_JoystickNameForIndex(i) or b'').decode(errors='replace'),
                    'guid': sdl_guid(sdl, i),
                    'vid': sdl.SDL_JoystickGetDeviceVendor(i),
                    'pid': sdl.SDL_JoystickGetDeviceProduct(i),
                    'gamecontroller': bool(sdl.SDL_IsGameController(i))})
    print(json.dumps(out))


# --- library side: one daemon per session ------------------------------------

class PadDaemonError(RuntimeError):
    pass


class PadDaemon:
    """The input daemon of one session. players: [(player number,
    ControllerInfo)]. start() returns {player: [/dev/input nodes]} — the
    nodes that player's instance may see."""

    def __init__(self, game_key, players, verbose: bool = False):
        self.game_key = str(game_key)
        self.players = [self._player(number, c) for number, c in players]
        self._rev = 0
        self.verbose = verbose
        self.proc: Optional[subprocess.Popen] = None
        run = RUN_ROOT / self.game_key
        run.mkdir(parents=True, exist_ok=True)
        self.config_path = run / 'padd-config.json'
        self.state_path = run / 'padd-state.json'
        self.status_path = run / 'padd-status.json'
        log_dir = LOG_ROOT / self.game_key
        log_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = log_dir / 'padd.log'
        self.nodes: Dict[int, List[str]] = {}

    @staticmethod
    def _player(number, c) -> dict:
        from controllers import event_node
        node = event_node(c.index)
        if not node:
            raise PadDaemonError(f'Controller {c.index} ({c.name}) has no event node')
        ident = node_identity(node)
        return {'player': int(number), 'node': node,
                'vid': c.vendor_id or ident['vid'],
                'pid': c.product_id or ident['pid'],
                'uniq': c.uniq or ident['uniq'],
                'phys': c.phys or ident['phys'],
                'name': c.name or ident['name']}

    def start(self, timeout: float = 10.0) -> Dict[int, List[str]]:
        for f in (self.state_path, self.status_path):
            try:
                f.unlink()
            except OSError:
                pass
        self.config_path.write_text(json.dumps({'players': self.players}))
        log = open(self.log_path, 'a')
        self.proc = subprocess.Popen(
            [sys.executable, __file__, 'serve', '--config', str(self.config_path),
             '--state', str(self.state_path), '--status', str(self.status_path)],
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        log.close()
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.proc.poll() is not None:
                raise PadDaemonError(self._failure('the input daemon exited'))
            try:
                state = json.loads(self.state_path.read_text())
                if state.get('pid') == self.proc.pid:
                    self.nodes = {int(k): v for k, v in state['players'].items()}
                    print(f'[janus] Janus input: ' + ', '.join(
                        f'P{k} → {" ".join(v)}' for k, v in sorted(self.nodes.items())),
                        flush=True)
                    return self.nodes
            except (OSError, ValueError):
                pass
            time.sleep(0.05)
        self.stop()
        raise PadDaemonError(self._failure('the input daemon did not start'))

    def _failure(self, what: str) -> str:
        try:
            tail = self.log_path.read_text(errors='replace').strip().splitlines()[-6:]
        except OSError:
            tail = []
        hint = ''
        if any('uinput' in l and ('Permission' in l or 'denied' in l) for l in tail):
            hint = ('\n\n/dev/uinput isn\'t writable. Steam\'s udev rules '
                    '(steam-devices) give it to the logged-in user.')
        return f'{what}.\n' + '\n'.join(tail) + hint

    def set_players(self, players, timeout: float = 10.0) -> Dict[int, List[str]]:
        """Dynamic players: change the session's players to *players*
        [(player number, ControllerInfo)] — the ones kept keep their virtual
        pad, new ones get one, the others' pads are released. Returns the new
        {player: nodes}. Raises PadDaemonError."""
        if not self.running():
            raise PadDaemonError('the input daemon is not running')
        current = {p['player']: p for p in self.players}
        wanted = []
        for number, c in players:
            number = int(number)
            wanted.append(current.get(number) or self._player(number, c))
        self.players = wanted
        self._rev += 1
        req = Path(str(self.config_path) + '.request')
        tmp = req.with_suffix('.tmp')
        tmp.write_text(json.dumps({'rev': self._rev, 'players': wanted}))
        os.replace(tmp, req)
        self.proc.send_signal(signal.SIGUSR1)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.proc.poll() is not None:
                raise PadDaemonError(self._failure('the input daemon exited'))
            try:
                state = json.loads(self.state_path.read_text())
                if state.get('pid') == self.proc.pid and state.get('rev', 0) >= self._rev:
                    self.nodes = {int(k): v for k, v in state['players'].items()}
                    return self.nodes
            except (OSError, ValueError):
                pass
            time.sleep(0.05)
        raise PadDaemonError(self._failure('the input daemon did not answer'))

    def add_player(self, number: int, controller) -> List[str]:
        """A player joins mid-session: their virtual pad's nodes."""
        nodes = self.set_players(
            [(p['player'], None) for p in self.players] + [(number, controller)])
        if int(number) not in nodes:
            raise PadDaemonError(self._failure(f'no virtual pad for P{number}'))
        return nodes[int(number)]

    def remove_player(self, number: int):
        """A player left mid-session: their pad is released (they can join again)."""
        if any(p['player'] == int(number) for p in self.players):
            self.set_players([(p['player'], None) for p in self.players
                              if p['player'] != int(number)])

    def status(self) -> dict:
        try:
            return json.loads(self.status_path.read_text()).get('players', {})
        except (OSError, ValueError):
            return {}

    def reload(self):
        """Re-read the mappings (restarts the remapper; the pads stay)."""
        if self.running():
            self.proc.send_signal(signal.SIGHUP)

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        if self.proc is None:
            return
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.proc = None


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('serve')
    s.add_argument('--config', required=True)
    s.add_argument('--state', required=True)
    s.add_argument('--status', required=True)
    r = sub.add_parser('remap')
    r.add_argument('--config', required=True)
    r.add_argument('--status')
    sub.add_parser('probe')
    args = ap.parse_args()
    {'serve': cmd_serve, 'remap': cmd_remap, 'probe': cmd_probe}[args.cmd](args)


if __name__ == '__main__':
    main()

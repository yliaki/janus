#!/usr/bin/env python3
"""Prototype remapper: physical pad (any SDL GameController) -> uinput Xbox 360 pad.

    remap.py [--hidapi] SPEC [SPEC ...]
SPEC = VID:PID[#k] (hex; #k = k-th such pad) — one persistent output per SPEC.
The output exists for the daemon's lifetime; when the source drops out it goes
neutral and the source is re-attached when a matching pad reappears.
"""
import ctypes, fcntl, os, select, struct, sys, threading, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sdlctl
from sdlctl import Event

def _IOC(d, t, nr, size): return (d << 30) | (size << 16) | (ord(t) << 8) | nr
UI_SET_EVBIT, UI_SET_KEYBIT, UI_SET_ABSBIT, UI_SET_FFBIT = (_IOC(1, 'U', n, 4) for n in (100, 101, 103, 107))
UI_SET_PHYS = _IOC(1, 'U', 108, 8)
UI_DEV_SETUP, UI_ABS_SETUP = _IOC(1, 'U', 3, 92), _IOC(1, 'U', 4, 28)
UI_DEV_CREATE, UI_DEV_DESTROY = 0x5501, 0x5502
UI_GET_SYSNAME = lambda n: _IOC(2, 'U', 44, n)
FF_UPLOAD_SIZE = 4 + 4 + 48 + 48
UI_BEGIN_FF_UPLOAD, UI_END_FF_UPLOAD = _IOC(3, 'U', 200, FF_UPLOAD_SIZE), _IOC(1, 'U', 201, FF_UPLOAD_SIZE)
UI_BEGIN_FF_ERASE, UI_END_FF_ERASE = _IOC(3, 'U', 202, 12), _IOC(1, 'U', 203, 12)
EV_SYN, EV_KEY, EV_ABS, EV_FF, EV_UINPUT = 0, 1, 3, 0x15, 0x0101
FF_RUMBLE, FF_GAIN = 0x50, 0x60
UI_FF_UPLOAD, UI_FF_ERASE = 1, 2

# SDL_GameControllerButton -> xpad key code
BUTTONS = {0: 0x130, 1: 0x131, 2: 0x133, 3: 0x134, 4: 0x13a, 5: 0x13c, 6: 0x13b,
           7: 0x13d, 8: 0x13e, 9: 0x136, 10: 0x137}
DPAD = {11: (0x11, -1), 12: (0x11, 1), 13: (0x10, -1), 14: (0x10, 1)}
STICKS = {0: 0, 1: 1, 2: 3, 3: 4}          # SDL axis -> ABS_X, ABS_Y, ABS_RX, ABS_RY
TRIGGERS = {4: 2, 5: 5}                    # -> ABS_Z, ABS_RZ (0..255)


class XboxOut:
    """A uinput device that looks exactly like xpad's wired Xbox 360 pad."""

    def __init__(self, tag):
        self.fd = os.open('/dev/uinput', os.O_RDWR | os.O_NONBLOCK)
        fd = self.fd
        for ev in (EV_KEY, EV_ABS, EV_FF):
            fcntl.ioctl(fd, UI_SET_EVBIT, ev)
        for code in sorted(BUTTONS.values()):
            fcntl.ioctl(fd, UI_SET_KEYBIT, code)
        for code, lo, hi, fuzz, flat in ((0, -32768, 32767, 16, 128), (1, -32768, 32767, 16, 128),
                                         (3, -32768, 32767, 16, 128), (4, -32768, 32767, 16, 128),
                                         (2, 0, 255, 0, 0), (5, 0, 255, 0, 0),
                                         (0x10, -1, 1, 0, 0), (0x11, -1, 1, 0, 0)):
            fcntl.ioctl(fd, UI_SET_ABSBIT, code)
            fcntl.ioctl(fd, UI_ABS_SETUP, struct.pack('HH6i', code, 0, 0, lo, hi, fuzz, flat, 0))
        fcntl.ioctl(fd, UI_SET_FFBIT, FF_RUMBLE)
        fcntl.ioctl(fd, UI_SET_PHYS, f'janus/{tag}'.encode() + b'\0')
        fcntl.ioctl(fd, UI_DEV_SETUP, struct.pack('HHHH80sI', 3, 0x045e, 0x028e, 0x0110,
                                                  b'Microsoft X-Box 360 pad', 16))
        fcntl.ioctl(fd, UI_DEV_CREATE)
        buf = bytearray(64)
        fcntl.ioctl(fd, UI_GET_SYSNAME(64), buf)
        sysname = buf.split(b'\0')[0].decode()
        self.nodes = sorted('/dev/input/' + n for n in os.listdir(f'/sys/devices/virtual/input/{sysname}')
                            if n.startswith(('event', 'js')))
        self.effects = {}          # id -> (strong, weak, length_ms)
        self.hat = {0x10: set(), 0x11: set()}
        self.rumble = None         # callback(strong, weak, ms)

    def write(self, events):
        now = time.time()
        s, us = int(now), int((now % 1) * 1e6)
        data = b''.join(struct.pack('llHHi', s, us, t, c, v) for t, c, v in events)
        os.write(self.fd, data + struct.pack('llHHi', s, us, EV_SYN, 0, 0))

    def neutral(self):
        ev = [(EV_KEY, c, 0) for c in BUTTONS.values()]
        ev += [(EV_ABS, c, 0) for c in (0, 1, 3, 4, 2, 5, 0x10, 0x11)]
        self.hat = {0x10: set(), 0x11: set()}
        self.write(ev)

    def button(self, b, down):
        if b in BUTTONS:
            self.write([(EV_KEY, BUTTONS[b], int(down))])
        elif b in DPAD:
            axis, sign = DPAD[b]
            (self.hat[axis].add if down else self.hat[axis].discard)(sign)
            h = self.hat[axis]
            self.write([(EV_ABS, axis, 0 if len(h) != 1 else next(iter(h)))])

    def axis(self, a, v):
        if a in STICKS:
            self.write([(EV_ABS, STICKS[a], v)])
        elif a in TRIGGERS:
            self.write([(EV_ABS, TRIGGERS[a], max(0, v) * 255 // 32767)])

    def ff_pump(self):
        """Handle force-feedback traffic from the game (uinput upload/erase/play)."""
        while True:
            try:
                data = os.read(self.fd, 24 * 16)
            except BlockingIOError:
                return
            for off in range(0, len(data), 24):
                _, _, t, code, val = struct.unpack_from('llHHi', data, off)
                if t == EV_UINPUT and code == UI_FF_UPLOAD:
                    req = bytearray(struct.pack('Ii', val, 0) + bytes(96))
                    fcntl.ioctl(self.fd, UI_BEGIN_FF_UPLOAD, req)
                    etype, eid, _dir, _tb, _ti, length, _delay = struct.unpack_from('HhHHHHH', req, 8)
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
                elif t == EV_FF and code in self.effects and self.rumble:
                    strong, weak, length = self.effects[code]
                    if val:
                        rc = self.rumble(strong, weak, length or 0xFFFF)
                        print(f'rumble -> source strong={strong} weak={weak} {length}ms rc={rc}', flush=True)
                    else:
                        self.rumble(0, 0, 0)


EVIOCGRAB = _IOC(1, 'E', 0x90, 4)


def grab_sdl_fd(path):
    """EVIOCGRAB the evdev fd SDL opened for `path` (SDL never grabs itself)."""
    rdev = os.stat(path).st_rdev
    for fd in os.listdir('/proc/self/fd'):
        try:
            st = os.fstat(int(fd))
        except OSError:
            continue
        if st.st_rdev == rdev and os.path.realpath(f'/proc/self/fd/{fd}') == path:
            fcntl.ioctl(int(fd), EVIOCGRAB, 1)
            print(f'grabbed {path} via SDL fd {fd}', flush=True)


def main():
    argv = sys.argv[1:]
    hidapi = '--hidapi' in argv
    specs = [a for a in argv if not a.startswith('--')]
    sdl = sdlctl.load()
    sdl.SDL_SetHint(b'SDL_JOYSTICK_HIDAPI', b'1' if hidapi else b'0')
    sdl.SDL_SetHint(b'SDL_NO_SIGNAL_HANDLERS', b'1')
    sdl.SDL_SetHint(b'SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS', b'1')
    if sdl.SDL_Init(sdlctl.SDL_INIT_GAMECONTROLLER):
        sys.exit(sdl.SDL_GetError())
    sdl.SDL_GameControllerEventState(1)

    slots = []
    for i, spec in enumerate(specs):
        vp, _, k = spec.partition('#')
        vid, pid = (int(x, 16) for x in vp.split(':'))
        out = XboxOut(f'p{i + 1}')
        slots.append({'vid': vid, 'pid': pid, 'k': int(k or 0), 'out': out, 'gc': None, 'iid': None})
        print(f'slot {i + 1}: {spec} -> {" ".join(out.nodes)}', flush=True)
    own = {n for s in slots for n in s['out'].nodes}
    by_iid = {}

    def attach(index):
        path = (sdl.SDL_JoystickPathForIndex(index) or b'').decode()
        if path in own or sdl.SDL_JoystickGetDeviceVendor(index) == 0x28de:
            return
        vid, pid = sdl.SDL_JoystickGetDeviceVendor(index), sdl.SDL_JoystickGetDeviceProduct(index)
        iid = sdl.SDL_JoystickGetDeviceInstanceID(index)
        if iid in by_iid:
            return
        # first free slot for this VID:PID (slots of one VID:PID fill in #k order)
        for s in sorted(slots, key=lambda s: s['k']):
            if s['gc'] is None and (s['vid'], s['pid']) == (vid, pid):
                gc = sdl.SDL_GameControllerOpen(index)
                if not gc:
                    print('open failed', sdl.SDL_GetError(), flush=True)
                    return
                s['gc'], s['iid'] = gc, iid
                if '--grab' in sys.argv:
                    grab_sdl_fd(path)
                by_iid[iid] = s
                s['out'].rumble = lambda st, wk, ms, gc=gc: sdl.SDL_GameControllerRumble(gc, st, wk, ms)
                print(f'attached {path} ({sdl.SDL_GameControllerName(gc).decode()}) '
                      f'rumble={sdl.SDL_GameControllerHasRumble(gc)}', flush=True)
                return

    ev = Event()
    ff_fds = {s['out'].fd: s['out'] for s in slots}
    while True:
        # FF traffic from the games (non-blocking)
        r, _, _ = select.select(list(ff_fds), [], [], 0)
        for fd in r:
            ff_fds[fd].ff_pump()
        if not sdl.SDL_WaitEventTimeout(ctypes.byref(ev), 4):
            continue
        t = ev.type
        if t == sdlctl.CONTROLLERDEVICEADDED:
            attach(ev.i32(8))
        elif t == sdlctl.CONTROLLERDEVICEREMOVED:
            s = by_iid.pop(ev.i32(8), None)
            if s:
                sdl.SDL_GameControllerClose(s['gc'])
                s['gc'] = s['iid'] = None
                s['out'].neutral()
                print('detached; output kept, neutral', flush=True)
        elif t in (sdlctl.CONTROLLERBUTTONDOWN, sdlctl.CONTROLLERBUTTONUP):
            s = by_iid.get(ev.i32(8))
            if s:
                s['out'].button(ev.u8(12), ev.u8(13))
        elif t == sdlctl.CONTROLLERAXISMOTION:
            s = by_iid.get(ev.i32(8))
            if s:
                s['out'].axis(ev.u8(12), ev.i16(16))


if __name__ == '__main__':
    main()

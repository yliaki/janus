#!/usr/bin/env python3
"""Virtual test gamepads via /dev/uinput, for testing without real pads.

Steam picks them up like real controllers (and gives them Steam virtual
pads), so a whole split-screen session can be driven from a script: launch
with `--tile <their js index>` (`main.py list`), send presses, screenshot.
/dev/uinput is writable for the logged-in user through Steam's udev rules.
See NOTES.md "Terraria / tModLoader / Factorio on Steam Input".

    scripts/fakepad.py FIFO NAME:VID:PID [NAME:VID:PID ...]
    scripts/fakepad.py /tmp/pads 'Logitech Gamepad F310:046d:c21d' \
                                 'Microsoft Xbox One S Controller:045e:02ea'

Creates one xpad-like pad per argument (pick VID:PIDs SDL knows, different
from the real pads plugged in), then reads commands from FIFO, one per line:
    press <pad#> <button>        button down + up (a b x y lb rb back start guide l3 r3)
    hold <pad#> <button> <sec>
    dpad <pad#> up|down|left|right
    stick <pad#> l|r <x> <y> <sec>   tilt a stick (x,y in -1..1, y down = +1) for sec
    trigger <pad#> lt|rt [sec]
    chord <pad#> <button> <button> ...   hold them together (OSK: chord 0 l3 r3)
    unplug <pad#>                remove the pad (as pulling its cable)
    plug <pad#> [NAME:VID:PID]   create it again (a new event/js node) on the same fake USB
                                 port, as itself or as another model (a pad switching mode)
Every pad gets phys "usb-fakepad-<pad#>/input0", like a pad on its own USB port.
    quit                         remove the pads and exit
e.g. `echo 'dpad 0 down' > /tmp/pads`.

Keyboard mode: drive ONE pad from this terminal, no FIFO (the terminal must
have keyboard focus; its keys never reach the games):

    scripts/fakepad.py --keys ['NAME:VID:PID']   # default: a Logitech F310

    arrows / hjkl D-pad   Enter / a  A      b  B       x  X     y  Y
    o  L3+R3 (on-screen keyboard)            q  LB      e  RB
    s  Start   v  Back (View)   1 / 3  L3 / R3 alone     Ctrl-C / Esc Esc  quit
"""
import fcntl
import os
import struct
import sys
import time

UI_SET_EVBIT, UI_SET_KEYBIT, UI_SET_ABSBIT = 0x40045564, 0x40045565, 0x40045567
UI_DEV_SETUP, UI_ABS_SETUP = 0x405c5503, 0x401c5504
UI_SET_PHYS = 0x4008556c
UI_DEV_CREATE, UI_DEV_DESTROY = 0x5501, 0x5502
EV_SYN, EV_KEY, EV_ABS = 0, 1, 3
BTN = {'a': 0x130, 'b': 0x131, 'x': 0x133, 'y': 0x134, 'lb': 0x136, 'rb': 0x137,
       'back': 0x13a, 'start': 0x13b, 'guide': 0x13c, 'l3': 0x13d, 'r3': 0x13e}
ABS = {0: (-32768, 32767), 1: (-32768, 32767), 3: (-32768, 32767), 4: (-32768, 32767),
       2: (0, 255), 5: (0, 255), 0x10: (-1, 1), 0x11: (-1, 1)}
DPAD = {'up': (0x11, -1), 'down': (0x11, 1), 'left': (0x10, -1), 'right': (0x10, 1)}


def make_pad(name, vid, pid, phys=''):
    fd = os.open('/dev/uinput', os.O_WRONLY | os.O_NONBLOCK)
    if phys:
        fcntl.ioctl(fd, UI_SET_PHYS, phys.encode() + b'\0')
    fcntl.ioctl(fd, UI_SET_EVBIT, EV_KEY)
    fcntl.ioctl(fd, UI_SET_EVBIT, EV_ABS)
    for code in BTN.values():
        fcntl.ioctl(fd, UI_SET_KEYBIT, code)
    for code, (lo, hi) in ABS.items():
        fcntl.ioctl(fd, UI_SET_ABSBIT, code)
        flat = 128 if hi > 255 else 0
        fcntl.ioctl(fd, UI_ABS_SETUP, struct.pack('HH6i', code, 0, 0, lo, hi, 16 if flat else 0, flat, 0))
    setup = struct.pack('HHHH80sI', 3, vid, pid, 0x110, name.encode()[:79], 0)
    fcntl.ioctl(fd, UI_DEV_SETUP, setup)
    fcntl.ioctl(fd, UI_DEV_CREATE)
    return fd


def emit(fd, typ, code, val):
    t = time.time()
    os.write(fd, struct.pack('llHHi', int(t), int((t % 1) * 1e6), typ, code, val))
    os.write(fd, struct.pack('llHHi', int(t), int((t % 1) * 1e6), EV_SYN, 0, 0))


def chord(fd, buttons, sec=0.15):
    for b in buttons:
        emit(fd, EV_KEY, BTN[b], 1)
    time.sleep(sec)
    for b in reversed(buttons):
        emit(fd, EV_KEY, BTN[b], 0)


def dpad(fd, direction):
    code, val = DPAD[direction]
    emit(fd, EV_ABS, code, val)
    time.sleep(0.08)
    emit(fd, EV_ABS, code, 0)


KEYS = {  # terminal key -> ('btn', [buttons]) | ('dpad', dir)
    '\x1b[A': ('dpad', 'up'), '\x1b[B': ('dpad', 'down'),
    '\x1b[C': ('dpad', 'right'), '\x1b[D': ('dpad', 'left'),
    'k': ('dpad', 'up'), 'j': ('dpad', 'down'), 'l': ('dpad', 'right'), 'h': ('dpad', 'left'),
    '\r': ('btn', ['a']), '\n': ('btn', ['a']), 'a': ('btn', ['a']), 'b': ('btn', ['b']),
    'x': ('btn', ['x']), 'y': ('btn', ['y']), 'q': ('btn', ['lb']), 'e': ('btn', ['rb']),
    's': ('btn', ['start']), 'v': ('btn', ['back']), 'o': ('btn', ['l3', 'r3']),
    '1': ('btn', ['l3']), '3': ('btn', ['r3']),
}


def keys_mode(spec):
    import select
    import termios
    import tty
    name, vid, pid = spec.split(':')
    fd = make_pad(name, int(vid, 16), int(pid, 16))
    print(f'created "{name}" ({vid}:{pid}). Give it a moment to appear '
          f'(main.py list), then press keys here. Ctrl-C quits.\r', flush=True)
    old = termios.tcgetattr(0)
    tty.setraw(0)
    try:
        while True:
            k = os.read(0, 1).decode(errors='replace')
            if k == '\x03':
                break
            if k == '\x1b':                      # escape sequence (arrows) or Esc
                r, _, _ = select.select([0], [], [], 0.05)
                if r:
                    k += os.read(0, 2).decode(errors='replace')
                elif select.select([0], [], [], 0.6)[0] and os.read(0, 1) == b'\x1b':
                    break                          # Esc Esc
                else:
                    continue
            act = KEYS.get(k)
            if act is None:
                continue
            if act[0] == 'dpad':
                dpad(fd, act[1])
            else:
                chord(fd, act[1])
            print(f'{time.strftime("%T")} {act[0]} {act[1]}\r', flush=True)
    finally:
        termios.tcsetattr(0, termios.TCSADRAIN, old)
        fcntl.ioctl(fd, UI_DEV_DESTROY)
        os.close(fd)
        print('pad removed')


def main():
    if len(sys.argv) > 1 and sys.argv[1] == '--keys':
        return keys_mode(sys.argv[2] if len(sys.argv) > 2 else 'Logitech Gamepad F310:046d:c21d')
    fifo, specs = sys.argv[1], sys.argv[2:]
    pads, ids = [], []
    for spec in specs:
        name, vid, pid = spec.split(':')
        ids.append((name, int(vid, 16), int(pid, 16)))
        pads.append(make_pad(*ids[-1], phys=f'usb-fakepad-{len(pads)}/input0'))
    print(f'created {len(pads)} pad(s)', flush=True)
    if not os.path.exists(fifo):
        os.mkfifo(fifo)
    try:
        while True:
            with open(fifo) as f:
                for line in f:
                    w = line.split()
                    if not w:
                        continue
                    if w[0] == 'quit':
                        return
                    fd = pads[int(w[1])]
                    if w[0] == 'unplug':
                        if fd is not None:
                            fcntl.ioctl(fd, UI_DEV_DESTROY)
                            os.close(fd)
                            pads[int(w[1])] = None
                    elif w[0] == 'plug':
                        if fd is None:
                            n = int(w[1])
                            if len(w) > 2:
                                name, vid, pid = ' '.join(w[2:]).split(':')
                                ids[n] = (name, int(vid, 16), int(pid, 16))
                            pads[n] = make_pad(*ids[n], phys=f'usb-fakepad-{n}/input0')
                    elif fd is None:
                        print(f'pad {w[1]} is unplugged: {line.strip()} ignored', flush=True)
                    elif w[0] in ('press', 'hold'):
                        emit(fd, EV_KEY, BTN[w[2]], 1)
                        time.sleep(float(w[3]) if w[0] == 'hold' else 0.12)
                        emit(fd, EV_KEY, BTN[w[2]], 0)
                    elif w[0] == 'dpad':
                        dpad(fd, w[2])
                    elif w[0] == 'chord':
                        chord(fd, w[2:])
                    elif w[0] == 'stick':
                        cx, cy = (0, 1) if w[2] == 'l' else (3, 4)
                        emit(fd, EV_ABS, cx, int(float(w[3]) * 32767))
                        emit(fd, EV_ABS, cy, int(float(w[4]) * 32767))
                        time.sleep(float(w[5]))
                        emit(fd, EV_ABS, cx, 0)
                        emit(fd, EV_ABS, cy, 0)
                    elif w[0] == 'trigger':
                        code = 2 if w[2] == 'lt' else 5
                        emit(fd, EV_ABS, code, 255)
                        time.sleep(float(w[3]) if len(w) > 3 else 0.12)
                        emit(fd, EV_ABS, code, 0)
                    print(f'{time.strftime("%T")} {line.strip()}', flush=True)
                    time.sleep(0.15)
    finally:
        for fd in pads:
            if fd is not None:
                fcntl.ioctl(fd, UI_DEV_DESTROY)
                os.close(fd)


if __name__ == '__main__':
    main()

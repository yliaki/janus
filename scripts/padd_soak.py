#!/usr/bin/env python3
"""Soak test of Janus input's daemon (src/host/padd.py) with fake pads.

Two identical serial-less uinput pads (like the PS1 "Twin USB" adapter), one
player each. For --minutes, random button / D-pad / stick input on both while,
every few seconds, one pad is unplugged and replugged, the remapper is
SIGKILLed or the mappings are reloaded. A reader on each virtual Xbox pad
checks that

  - every press reaches its own player's pad and never the other one,
  - nothing stays held (state is neutral once the source lets go),
  - the virtual pads never disappear (same nodes for the whole run).

No game, Steam or Big Picture needed; /dev/uinput must be writable.

    scripts/padd_soak.py [--minutes 5] [--seed 1]
"""

import argparse
import fcntl
import os
import random
import select
import signal
import struct
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src' / 'host'))
from controllers import list_controllers  # noqa: E402
from padd import PadDaemon  # noqa: E402

EV = struct.Struct('llHHi')
UI_SET_EVBIT, UI_SET_KEYBIT, UI_SET_ABSBIT = 0x40045564, 0x40045565, 0x40045567
UI_DEV_SETUP, UI_ABS_SETUP, UI_DEV_CREATE, UI_DEV_DESTROY = 0x405c5503, 0x401c5504, 0x5501, 0x5502
# Twin USB PS2 Adapter (0810:0001) as SDL maps it: b2=A b1=B b3=X b0=Y,
# hat = D-pad, ABS_X/ABS_Y = left stick (0..255).
SRC_BUTTONS = {0x122: 0x130, 0x121: 0x131, 0x123: 0x133, 0x120: 0x134}  # source -> Xbox key


class FakeTwin:
    def __init__(self, n):
        self.n = n
        self.fd = None

    def plug(self):
        fd = os.open('/dev/uinput', os.O_WRONLY | os.O_NONBLOCK)
        fcntl.ioctl(fd, UI_SET_EVBIT, 1)
        fcntl.ioctl(fd, UI_SET_EVBIT, 3)
        for c in range(0x120, 0x12c):
            fcntl.ioctl(fd, UI_SET_KEYBIT, c)
        for c, lo, hi in ((0, 0, 255), (1, 0, 255), (2, 0, 255), (5, 0, 255),
                          (0x10, -1, 1), (0x11, -1, 1)):
            fcntl.ioctl(fd, UI_SET_ABSBIT, c)
            fcntl.ioctl(fd, UI_ABS_SETUP, struct.pack(
                'HH6i', c, 0, 128 if hi == 255 else 0, lo, hi, 0, 8 if hi == 255 else 0, 0))
        fcntl.ioctl(fd, UI_DEV_SETUP, struct.pack('HHHH80sI', 3, 0x0810, 1, 0x0110,
                                                  b'Twin USB Joystick', 0))
        fcntl.ioctl(fd, UI_DEV_CREATE)
        self.fd = fd
        for c in (0, 1, 2, 5):
            self.emit(3, c, 128)

    def unplug(self):
        if self.fd is not None:
            fcntl.ioctl(self.fd, UI_DEV_DESTROY)
            os.close(self.fd)
            self.fd = None

    def emit(self, t, c, v):
        now = time.time()
        s, us = int(now), int((now % 1) * 1e6)
        os.write(self.fd, EV.pack(s, us, t, c, v) + EV.pack(s, us, 0, 0, 0))


class Reader(threading.Thread):
    """Records the key presses / hat / stick state seen on one virtual pad."""

    def __init__(self, node):
        super().__init__(daemon=True)
        self.fd = os.open(node, os.O_RDONLY | os.O_NONBLOCK)
        self.presses = []          # (key, time)
        self.keys, self.abs = {}, {}
        self.stop = False

    def run(self):
        while not self.stop:
            if not select.select([self.fd], [], [], 0.1)[0]:
                continue
            try:
                data = os.read(self.fd, EV.size * 64)
            except OSError:
                continue
            for off in range(0, len(data), EV.size):
                _, _, t, c, v = EV.unpack_from(data, off)
                if t == 1:
                    self.keys[c] = v
                    if v:
                        self.presses.append((c, time.monotonic()))
                elif t == 3:
                    self.abs[c] = v


def remapper_pids():
    out = []
    for p in Path('/proc').iterdir():
        if not p.name.isdigit():
            continue
        try:
            argv = (p / 'cmdline').read_bytes().split(b'\0')
        except OSError:
            continue
        if len(argv) > 2 and argv[1].endswith(b'padd.py') and argv[2] == b'remap':
            out.append(int(p.name))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--minutes', type=float, default=5)
    ap.add_argument('--seed', type=int, default=1)
    ap.add_argument('--calm', action='store_true', help='no replugs / restarts')
    args = ap.parse_args()
    rnd = random.Random(args.seed)

    pads = [FakeTwin(0), FakeTwin(1)]
    for p in pads:
        p.plug()
        time.sleep(0.3)
    time.sleep(1.5)
    twins = [c for c in list_controllers() if (c.vendor_id, c.product_id) == (0x0810, 1)]
    if len(twins) < 2:
        sys.exit('fake pads not found')
    twins = twins[-2:]                   # ours: the newest
    daemon = PadDaemon('soak', [(1, twins[0]), (2, twins[1])])
    nodes = daemon.start()
    # which fake pad is which player: by event node order (both created in order)
    readers = {1: Reader(nodes[1][0]), 2: Reader(nodes[2][0])}
    for r in readers.values():
        r.start()
    time.sleep(1.5)

    def held_at_rest(pl):
        r = readers[pl]
        return [k for k, v in r.keys.items() if v] + \
               [f'abs{c}={v}' for c, v in r.abs.items() if c in (0x10, 0x11) and v]

    # Calibrate: which fake pad drives player 1.
    pads[0].emit(1, 0x122, 1); time.sleep(0.15); pads[0].emit(1, 0x122, 0); time.sleep(0.4)
    owner = {0: 1 if readers[1].presses else 2}
    owner[1] = 3 - owner[0]
    for r in readers.values():
        r.presses.clear()
    print(f'fake pad 0 -> P{owner[0]}, fake pad 1 -> P{owner[1]}; nodes {nodes}')

    # A press right after a deliberate remapper kill may be lost (the
    # remapper is restarting: ~0.5 s); anything else lost is a failure.
    RESTART_WINDOW = 2.0
    stats = dict(sent=0, delivered=0, crosstalk=0, lost=0, lost_restarting=0, stuck=0, replugs=0,
                 remapper_kills=0, reloads=0)
    cpu0 = None
    end = time.monotonic() + args.minutes * 60
    next_disruption = time.monotonic() + rnd.uniform(4, 8)
    proc_cpu = lambda pid: sum(int(x) for x in Path(f'/proc/{pid}/stat').read_text().split()[13:15])
    t_start = time.monotonic()
    last_disruption = [t_start, 'start']
    while time.monotonic() < end:
        # a burst of input on a random pad
        i = rnd.randrange(2)
        pad, me, other = pads[i], owner[i], owner[1 - i]
        if pad.fd is None:
            time.sleep(0.05)
            continue
        kind = rnd.choice(('button', 'button', 'hat', 'stick'))
        before_me = len(readers[me].presses)
        before_other = len(readers[other].presses)
        if kind == 'button':
            src = rnd.choice(list(SRC_BUTTONS))
            pad.emit(1, src, 1)
            time.sleep(rnd.uniform(0.03, 0.12))
            pad.emit(1, src, 0)
            stats['sent'] += 1
            time.sleep(0.12)
            got = [k for k, _ in readers[me].presses[before_me:]]
            if SRC_BUTTONS[src] in got:
                stats['delivered'] += 1
            elif (last_disruption[1] in ('kill', 'reload')
                  and time.monotonic() - last_disruption[0] < RESTART_WINDOW):
                stats['lost_restarting'] += 1
            else:
                stats['lost'] += 1
                print(f'  lost: pad {i} {hex(src)} -> P{me} saw {got} '
                      f'({time.monotonic() - last_disruption[0]:.1f}s after {last_disruption[1]})')
            if len(readers[other].presses) != before_other:
                stats['crosstalk'] += 1
                print(f'  CROSSTALK: pad {i} press reached P{other}')
        elif kind == 'hat':
            axis, v = rnd.choice(((0x10, -1), (0x10, 1), (0x11, -1), (0x11, 1)))
            pad.emit(3, axis, v)
            time.sleep(0.08)
            pad.emit(3, axis, 0)
            time.sleep(0.1)
        else:
            pad.emit(3, 0, rnd.choice((0, 255)))
            time.sleep(0.08)
            pad.emit(3, 0, 128)
            time.sleep(0.1)
        # all let go: nothing may stay held on either virtual pad
        if pad.fd is not None:
            stuck = held_at_rest(me)
            if stuck:
                time.sleep(0.3)
                stuck = held_at_rest(me)
                if stuck:
                    stats['stuck'] += 1
                    print(f'  STUCK on P{me}: {stuck} ({time.monotonic() - last_disruption[0]:.1f}s '
                          f'after {last_disruption[1]})')

        if time.monotonic() >= next_disruption and not args.calm:
            what = rnd.choice(('replug', 'replug', 'kill', 'reload'))
            if what == 'replug':
                j = rnd.randrange(2)
                what = f'replug of pad {j} (P{owner[j]})'
                pads[j].unplug()
                time.sleep(rnd.uniform(0.3, 2.0))
                pads[j].plug()
                time.sleep(1.2)              # SDL notices (≤ 0.25 s) + udev
                stats['replugs'] += 1
            elif what == 'kill':
                for pid in remapper_pids():
                    os.kill(pid, signal.SIGKILL)
                time.sleep(rnd.uniform(0, 1.5))   # input may come in while it restarts
                stats['remapper_kills'] += 1
            else:
                daemon.reload()
                time.sleep(1.5)
                stats['reloads'] += 1
            last_disruption[:] = [time.monotonic(), what]
            next_disruption = time.monotonic() + rnd.uniform(4, 8)
        if cpu0 is None and time.monotonic() - t_start > 5:
            holder = daemon.proc.pid
            cpu0 = (time.monotonic(), proc_cpu(holder),
                    {pid: proc_cpu(pid) for pid in remapper_pids()})

    alive = all(os.path.exists(n) for ns in nodes.values() for n in ns)
    status = daemon.status()
    for r in readers.values():
        r.stop = True
    # idle CPU of the remapper: 10 s without input
    rp = remapper_pids()
    c1 = {pid: proc_cpu(pid) for pid in rp}
    time.sleep(10)
    idle = sum(proc_cpu(pid) - c1[pid] for pid in rp if Path(f'/proc/{pid}').exists()) / 10
    daemon.stop()
    for p in pads:
        p.unplug()
    gone = not any(os.path.exists(n) for ns in nodes.values() for n in ns)
    print('\n' + ', '.join(f'{k}={v}' for k, v in stats.items()))
    print(f'virtual pads lived through the run: {alive}; removed at stop: {gone}')
    print(f'status at end: {status}')
    print(f'remapper idle CPU: {idle:.1f}% of a core')
    ok = (stats['crosstalk'] == 0 and stats['stuck'] == 0 and stats['lost'] == 0
          and alive and gone)
    print(f'(lost_restarting: presses within {RESTART_WINDOW:g} s of a deliberate '
          f'remapper kill/reload — expected)')
    print('PASS' if ok else 'FAIL')
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Read every input device except EXCLUDE names for N seconds; print events + new devices."""
import os, select, struct, sys, time, glob
secs = float(sys.argv[1]); skip = set(sys.argv[2:])
def name(n):
    try: return open(f'/sys/class/input/{n}/device/name').read().strip()
    except OSError: return '?'
fds = {}
def scan():
    for p in glob.glob('/dev/input/event*'):
        n = os.path.basename(p)
        if n in skip or p in [v[0] for v in fds.values()]: continue
        try: fd = os.open(p, os.O_RDONLY | os.O_NONBLOCK)
        except OSError: continue
        fds[fd] = (p, name(n))
        if scan.started: print('NEW DEVICE', p, fds[fd][1], flush=True)
scan.started = False; scan(); scan.started = True
end = time.time() + secs; last = 0
while time.time() < end:
    if time.time() - last > 0.5: scan(); last = time.time()
    for fd in select.select(list(fds), [], [], 0.1)[0]:
        try: d = os.read(fd, 24 * 64)
        except OSError: continue
        for o in range(0, len(d), 24):
            _, _, t, c, v = struct.unpack_from('llHHi', d, o)
            if t in (1, 2, 3): print(f'{fds[fd][0]} [{fds[fd][1]}] type={t} code={c:#x} val={v}', flush=True)

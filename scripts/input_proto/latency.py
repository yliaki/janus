import os, select, struct, sys, threading, time, statistics
src, out, fifo = sys.argv[1:4]
fs, fo = os.open(src, os.O_RDONLY), os.open(out, os.O_RDONLY)
def drive():
    time.sleep(0.5)
    with open(fifo, 'w', buffering=1) as f:
        for i in range(200):
            f.write(f'btn 2 {(i + 1) % 2}\n'); time.sleep(0.013 + (i % 7) * 0.001)
threading.Thread(target=drive, daemon=True).start()
ts = {fs: [], fo: []}
end = time.time() + 5
while time.time() < end:
    for fd in select.select([fs, fo], [], [], 0.1)[0]:
        d = os.read(fd, 24 * 32)
        for o in range(0, len(d), 24):
            s, us, t, c, v = struct.unpack_from('llHHi', d, o)
            if t == 1 and c in (0x122, 0x130):
                ts[fd].append(s + us / 1e6)
lat = [(b - a) * 1000 for a, b in zip(ts[fs], ts[fo])]
print(f'n={len(ts[fs])}/{len(ts[fo])} latency ms: median {statistics.median(lat):.2f} p95 {sorted(lat)[int(.95*len(lat))]:.2f} max {max(lat):.2f}')

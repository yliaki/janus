"""utype.py TEXT : type lowercase letters/space, then Enter, via a uinput keyboard."""
import fcntl, os, struct, sys, time
def _IOC(d, t, nr, size): return (d << 30) | (size << 16) | (ord(t) << 8) | nr
KEYS = dict(zip('qwertyuiop', range(16, 26))) | dict(zip('asdfghjkl', range(30, 39))) | dict(zip('zxcvbnm', range(44, 51))) | {' ': 57, '\n': 28, '_': 12}
fd = os.open('/dev/uinput', os.O_WRONLY)
fcntl.ioctl(fd, _IOC(1,'U',100,4), 1)
for k in set(KEYS.values()) | {1, 42}: fcntl.ioctl(fd, _IOC(1,'U',101,4), k)
fcntl.ioctl(fd, _IOC(1,'U',3,92), struct.pack('HHHH80sI', 3, 0x1234, 0x5678, 1, b'janus test keyboard', 0))
fcntl.ioctl(fd, 0x5501); time.sleep(1)
def ev(t,c,v): os.write(fd, struct.pack('llHHi',0,0,t,c,v))
def tap(k, shift=False):
    if shift: ev(1,42,1)
    ev(1,k,1); ev(0,0,0); time.sleep(0.03); ev(1,k,0)
    if shift: ev(1,42,0)
    ev(0,0,0); time.sleep(0.03)
for ch in sys.argv[1] + '\n':
    tap(KEYS[ch], shift=(ch == '_'))
time.sleep(0.5); fcntl.ioctl(fd, 0x5502)

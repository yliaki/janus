import fcntl, os, struct, sys, time
def _IOC(d, t, nr, size): return (d << 30) | (size << 16) | (ord(t) << 8) | nr
fd = os.open('/dev/uinput', os.O_WRONLY)
fcntl.ioctl(fd, _IOC(1,'U',100,4), 1)
for k in (28, 1): fcntl.ioctl(fd, _IOC(1,'U',101,4), k)
fcntl.ioctl(fd, _IOC(1,'U',3,92), struct.pack('HHHH80sI', 3, 0x1234, 0x5678, 1, b'janus test keyboard', 0))
fcntl.ioctl(fd, 0x5501); time.sleep(1)
def ev(t,c,v): os.write(fd, struct.pack('llHHi',0,0,t,c,v))
for _ in range(int(sys.argv[1]) if len(sys.argv) > 1 else 1):
    K=int(sys.argv[2]) if len(sys.argv)>2 else 28; ev(1,K,1); ev(0,0,0); time.sleep(0.05); ev(1,K,0); ev(0,0,0); time.sleep(0.3)
time.sleep(0.5); fcntl.ioctl(fd, 0x5502)

import os, socket, sys, time, struct
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from remap import XboxOut
sock = '\0janus-fdhold-test'
def talk(msg, fds=()):
    c = socket.socket(socket.AF_UNIX); c.connect(sock); socket.send_fds(c, [msg], list(fds)); return c
if sys.argv[1] == 'create':
    out = XboxOut('ptest'); print('created', out.nodes, flush=True)
    talk(b'put', [out.fd]).recv(2); print('handed to holder; daemon exits now', flush=True)
else:
    c = talk(b'get'); _, fds, _, _ = socket.recv_fds(c, 16, 8); fd = fds[0]
    t = time.time(); s, us = int(t), int((t % 1) * 1e6)
    os.write(fd, struct.pack('llHHi', s, us, 1, 0x130, 1) + struct.pack('llHHi', s, us, 0, 0, 0))
    os.write(fd, struct.pack('llHHi', s, us, 1, 0x130, 0) + struct.pack('llHHi', s, us, 0, 0, 0))
    print('new daemon got fd back and wrote A press', flush=True)

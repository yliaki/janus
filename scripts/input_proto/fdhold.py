"""fdhold.py SOCK: keep uinput fds alive; 'put' stores the received fd, 'get' sends them back."""
import os, socket, sys
srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
try: os.unlink('\0janus-fdhold-test') if not '\0janus-fdhold-test'.startswith('\0') else None
except FileNotFoundError: pass
srv.bind('\0janus-fdhold-test'.replace('\\0', '\0')); srv.listen()
held = []
while True:
    c, _ = srv.accept()
    msg, fds, _, _ = socket.recv_fds(c, 16, 8)
    if msg == b'put': held += fds; c.send(b'ok')
    elif msg == b'get': socket.send_fds(c, [b'fds'], held)
    c.close()

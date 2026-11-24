#!/usr/bin/env python3
"""Fake 'Twin USB PS2 Adapter' (0810:0001): DirectInput layout, 12 buttons
BTN_TRIGGER..BTN_BASE6, ABS_X/Y/Z/RZ 0..255, hat.  SDL maps it via its DB
(a:b2 b:b1 x:b3 y:b0 ...).

    weirdpad.py FIFO [NAME]
FIFO commands: 'btn N 0|1', 'abs CODE VALUE', 'quit'
"""
import fcntl, os, struct, sys, time

UI_SET_EVBIT, UI_SET_KEYBIT, UI_SET_ABSBIT = 0x40045564, 0x40045565, 0x40045567
UI_DEV_SETUP, UI_ABS_SETUP, UI_DEV_CREATE = 0x405c5503, 0x401c5504, 0x5501


def make(name):
    fd = os.open('/dev/uinput', os.O_WRONLY | os.O_NONBLOCK)
    fcntl.ioctl(fd, UI_SET_EVBIT, 1)
    fcntl.ioctl(fd, UI_SET_EVBIT, 3)
    for c in range(0x120, 0x12c):
        fcntl.ioctl(fd, UI_SET_KEYBIT, c)
    for c, lo, hi in ((0, 0, 255), (1, 0, 255), (2, 0, 255), (5, 0, 255), (0x10, -1, 1), (0x11, -1, 1)):
        fcntl.ioctl(fd, UI_SET_ABSBIT, c)
        fcntl.ioctl(fd, UI_ABS_SETUP, struct.pack('HH6i', c, 0, 128 if hi == 255 else 0, lo, hi, 0, 8 if hi == 255 else 0, 0))
    fcntl.ioctl(fd, UI_DEV_SETUP, struct.pack('HHHH80sI', 3, 0x0810, 0x0001, 0x0110, name.encode(), 0))
    fcntl.ioctl(fd, UI_DEV_CREATE)
    return fd


def emit(fd, t, c, v):
    now = time.time()
    s, us = int(now), int((now % 1) * 1e6)
    os.write(fd, struct.pack('llHHi', s, us, t, c, v) + struct.pack('llHHi', s, us, 0, 0, 0))


def main():
    fifo = sys.argv[1]
    fd = make(sys.argv[2] if len(sys.argv) > 2 else 'Twin USB Joystick')
    for c in (0, 1, 2, 5):
        emit(fd, 3, c, 128)
    print('weirdpad up', flush=True)
    if not os.path.exists(fifo):
        os.mkfifo(fifo)
    while True:
        with open(fifo) as f:
            for line in f:
                w = line.split()
                if not w:
                    continue
                if w[0] == 'quit':
                    return
                if w[0] == 'btn':
                    emit(fd, 1, 0x120 + int(w[1]), int(w[2]))
                elif w[0] == 'abs':
                    emit(fd, 3, int(w[1], 0), int(w[2]))


main()

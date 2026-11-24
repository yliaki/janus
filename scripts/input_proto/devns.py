#!/usr/bin/env python3
"""Prototype: run a command with a private /dev that shows only chosen input nodes.

    devns.py [--allow /dev/input/eventN ...] -- CMD ARGS...

/dev becomes a tmpfs re-populated with every host /dev entry except input/,
hidraw*, uinput; /dev/input is a tmpfs holding only the --allow nodes.
"""
import os
import sys

HIDE = ('input', 'uinput')


def bwrap_args(allow):
    a = ['bwrap', '--dev-bind', '/', '/', '--tmpfs', '/dev']
    for name in sorted(os.listdir('/dev')):
        p = '/dev/' + name
        if name in HIDE or name.startswith('hidraw'):
            continue
        if os.path.islink(p):
            a += ['--symlink', os.readlink(p), p]
        else:
            a += ['--dev-bind', p, p]
    a += ['--dir', '/dev/input']
    for node in allow:
        a += ['--dev-bind', node, node]
    return a


def main():
    argv = sys.argv[1:]
    allow = []
    while argv and argv[0] != '--':
        if argv[0] == '--allow':
            allow.append(argv[1]); argv = argv[2:]
        else:
            sys.exit('bad arg ' + argv[0])
    cmd = argv[1:]
    os.execvp('bwrap', bwrap_args(allow) + ['--'] + cmd)


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Dynamic players in Janus input's daemon (padd.py), with fake pads.

P1 starts alone; P2 joins mid-session, P1 leaves (its pad must be released:
nobody grabs it any more), then P1's pad joins again as P3. After every step
each remaining player's presses must reach their own virtual pad only.

    scripts/padd_players_test.py
"""

import fcntl
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src' / 'host'))
from padd_soak import FakeTwin, Reader  # noqa: E402
from controllers import list_controllers, event_node  # noqa: E402
from padd import PadDaemon, EVIOCGRAB  # noqa: E402

A_SRC, A_XBOX = 0x122, 0x130


def press(pad):
    pad.emit(1, A_SRC, 1)
    time.sleep(0.05)
    pad.emit(1, A_SRC, 0)
    time.sleep(0.3)


def grabbed(node) -> bool:
    fd = os.open(node, os.O_RDONLY | os.O_NONBLOCK)
    try:
        fcntl.ioctl(fd, EVIOCGRAB, 1)
        fcntl.ioctl(fd, EVIOCGRAB, 0)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)


def main():
    pads = [FakeTwin(i) for i in range(2)]
    for p in pads:
        p.plug()
        time.sleep(0.3)
    time.sleep(1.5)
    twins = [c for c in list_controllers() if (c.vendor_id, c.product_id) == (0x0810, 1)][-2:]
    if len(twins) < 2:
        sys.exit('fake pads not found')
    fails = []

    def check(what, cond):
        print(('ok   ' if cond else 'FAIL ') + what, flush=True)
        if not cond:
            fails.append(what)

    daemon = PadDaemon('padd-players-test', [(1, twins[0])])
    try:
        nodes = daemon.start()
        time.sleep(1.0)
        readers = {1: Reader(nodes[1][0])}
        readers[1].start()

        def presses_reach(pad_i, player):
            before = {p: len(r.presses) for p, r in readers.items()}
            press(pads[pad_i])
            got = {p: len(r.presses) - before[p] for p, r in readers.items()}
            return got.get(player, 0) > 0 and all(
                n == 0 for p, n in got.items() if p != player)

        check('P1 alone: pad 0 -> P1', presses_reach(0, 1))

        new = daemon.add_player(2, twins[1])
        check('P2 joined: got a virtual pad', bool(new))
        time.sleep(1.5)
        readers[2] = Reader(new[0])
        readers[2].start()
        check('P1 still there, same nodes', daemon.nodes.get(1) == nodes[1])
        check('after join: pad 0 -> P1', presses_reach(0, 1))
        check('after join: pad 1 -> P2', presses_reach(1, 2))

        daemon.remove_player(1)
        time.sleep(1.5)
        readers.pop(1).stop = True
        check('P1 left: its virtual pad is gone', not os.path.exists(nodes[1][0]))
        check('P1 left: its pad is released', not grabbed(event_node(twins[0].index)))
        check('P2 still grabbed', grabbed(event_node(twins[1].index)))
        check('after leave: pad 1 -> P2', presses_reach(1, 2))

        again = daemon.add_player(3, twins[0])
        time.sleep(1.5)
        readers[3] = Reader(again[0])
        readers[3].start()
        check('pad 0 rejoined as P3: grabbed again', grabbed(event_node(twins[0].index)))
        check('after rejoin: pad 0 -> P3', presses_reach(0, 3))
        check('after rejoin: pad 1 -> P2', presses_reach(1, 2))
    finally:
        daemon.stop()
        for p in pads:
            p.unplug()
    print('PASS' if not fails else f'{len(fails)} failure(s)')
    sys.exit(1 if fails else 0)


if __name__ == '__main__':
    main()

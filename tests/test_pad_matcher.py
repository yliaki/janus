#!/usr/bin/env python3
"""Unit tests for src/host/pad_matcher.py (identical pads -> Steam handles).
Fake pads and presses; no devices are opened, files go to a temp $HOME.

    python3 tests/test_pad_matcher.py
"""

import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'src' / 'host'))

os.environ['HOME'] = tempfile.mkdtemp(prefix='janus-padmatch-test-')

import pad_matcher  # noqa: E402
from pad_matcher import PadMatcher, WINDOW  # noqa: E402

fails = 0


def check(cond, msg):
    global fails
    print(('  ok: ' if cond else 'FAIL: ') + msg)
    fails += not cond


TWIN = (0x0810, 0x0001)
H2, H3 = '0x000028101d48d305', '0x000038101d48d305'
# The adapter's halves: event24/event25. Steam index 2 = event25, 3 = event24
# (reversed, as measured 2026-10-17).
pads = [{'js': 2, 'event': 24, 'vid': TWIN[0], 'pid': TWIN[1], 'name': 'Twin USB Joystick'},
        {'js': 3, 'event': 25, 'vid': TWIN[0], 'pid': TWIN[1], 'name': 'Twin USB Joystick'}]
slots = [{'slot': 0, 'vid': TWIN[0], 'pid': TWIN[1], 'handle': H2, 'index': 2},
         {'slot': 2, 'vid': TWIN[0], 'pid': TWIN[1], 'handle': H3, 'index': 3}]
virt = {0: {'js': 0, 'event': 3, 'vid': 0x28de, 'pid': 0x11ff, 'name': 'Microsoft X-Box 360 pad 0'},
        2: {'js': 5, 'event': 27, 'vid': 0x28de, 'pid': 0x11ff, 'name': 'Microsoft X-Box 360 pad 2'}}

check(pad_matcher._steam_index(H2, *TWIN) == 2 and pad_matcher._steam_index(H3, *TWIN) == 3,
      'Steam index read from the handle')
check(pad_matcher._steam_index('0x136e83001d48d305', 0x36e8, 0x3001) == 1,
      'Steam index of a MACHENIKE handle')

m = PadMatcher(1, [])
m.inst_event = {1: 25}                       # player 1 = the PS1 on the 2nd half
groups = m._groups(pads, slots)
check(list(groups) == [TWIN], 'the adapter is an ambiguous group')
check(m._assignment(*groups[TWIN]) == {24: H2, 25: H3},
      'before any press: rank order (event order <-> Steam index), one-to-one')


def press(m, t, phys, virtual):
    """A physical press at t, Steam's virtual press 20 ms later."""
    pr = [[t, 'phys', phys, False], [t + 0.02, 'virt', virtual, False]]
    m._settle(pr, groups, slots, t + WINDOW + 0.01)


press(m, 10.0, pads[1], virt[0])
check(25 not in m.confirmed, 'one matching press is not enough')
press(m, 11.0, pads[1], virt[0])
check(m.confirmed.get(25) == H2, 'two matching presses confirm event25 = slot 0 handle')
check(m._assignment(*groups[TWIN]) == {25: H2, 24: H3},
      'the empty port follows by elimination, still one-to-one')
written = pad_matcher.pad_map_path(1, 1).read_text()
check(written.strip() == f'handle={H2}', 'instance 1 got its handle written')

# Two pads of the group pressed together: no vote.
m2 = PadMatcher(1, [])
m2.inst_event = {1: 25}
for t in (20.0, 21.0, 22.0):
    pr = [[t, 'phys', pads[1], False], [t + 0.05, 'phys', pads[0], False],
          [t + 0.02, 'virt', virt[0], False]]
    m2._settle(pr, groups, slots, t + WINDOW + 0.1)
check(not m2.confirmed, 'simultaneous presses on two identical pads are ignored')

# Two virtual pads react: no vote.
m3 = PadMatcher(1, [])
m3.inst_event = {1: 25}
for t in (30.0, 31.0):
    pr = [[t, 'phys', pads[1], False], [t + 0.02, 'virt', virt[0], False],
          [t + 0.03, 'virt', virt[2], False]]
    m3._settle(pr, groups, slots, t + WINDOW + 0.01)
check(not m3.confirmed, 'ambiguous virtual reaction is ignored')

# A virtual press outside the window doesn't count.
m4 = PadMatcher(1, [])
m4.inst_event = {1: 25}
for t in (40.0, 41.0):
    pr = [[t, 'phys', pads[1], False], [t + WINDOW + 0.05, 'virt', virt[0], False]]
    m4._settle(pr, groups, slots, t + 2 * WINDOW)
check(not m4.confirmed, 'a late virtual press is not a match')

# A handle moves to the pad that proves it, freeing the other.
m5 = PadMatcher(1, [])
m5.confirmed = {24: H2}
press(m5, 50.0, pads[1], virt[0])
press(m5, 51.0, pads[1], virt[0])
check(m5.confirmed == {25: H2}, 'a handle belongs to one pad only')

# Three identical pads, one a spare (the adapter's empty port): confirming
# two of them places the third by elimination.
three = [{'event': e} for e in (26, 27, 28)]
hs = [{'handle': h} for h in ('H4', 'H5', 'H6')]
m6 = PadMatcher(1, [])
check(m6._assignment(three, hs) == {26: 'H4', 27: 'H5', 28: 'H6'},
      'no confirmation: rank order')
m6.confirmed = {26: 'H6', 27: 'H4'}
check(m6._assignment(three, hs) == {26: 'H6', 27: 'H4', 28: 'H5'},
      'two confirmed (one the spare): the third by elimination')

print('ALL PASS' if not fails else f'{fails} FAILED')
sys.exit(1 if fails else 0)

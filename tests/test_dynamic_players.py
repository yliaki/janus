#!/usr/bin/env python3
"""Dynamic players: layouts per player count, gamescope sizes for resizable
games, the size file. (The input daemon side: scripts/padd_players_test.py;
the whole flow needs a game: NOTES.md "Dynamic players".)

    python3 tests/test_dynamic_players.py
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src' / 'host'))

FAILS = []


def check(what, cond):
    print(('  ok: ' if cond else '  FAIL: ') + what)
    if not cond:
        FAILS.append(what)


class Mon:
    def __init__(self, name, x, y, w, h, primary=False):
        self.name, self.x, self.y, self.width, self.height, self.primary = name, x, y, w, h, primary


def test_layouts():
    import layouts
    one = [Mon('A', 0, 0, 1920, 1200, True)]
    for n in range(1, layouts.MAX_PLAYERS + 1):
        rects = layouts.default_rects(one, n)
        check(f'{n} players: {n} tiles', len(rects) == n)
        check(f'{n} players: tiles inside the monitor',
              all(0 <= x and 0 <= y and x + w <= 1920 and y + h <= 1200 for x, y, w, h, _ in rects))
    check('2 players: top and bottom by default',
          layouts.default_rects(one, 2) == [(0, 0, 1920, 600, 'A'), (0, 600, 1920, 600, 'A')])
    check('2 players, side by side first',
          layouts.default_rects(one, 2, True)[1] == (960, 0, 960, 1200, 'A'))
    pref = layouts.layout_options(one, 3)[2]['id']
    check('a preferred layout id is used',
          layouts.default_rects(one, 3, preferred=pref)[0] == (0, 0, 640, 1200, 'A'))
    two = [Mon('L', 0, 0, 1920, 1080, True), Mon('R', 1920, 0, 2560, 1440)]
    check('several monitors: tiles on the primary first',
          layouts.default_rects(two, 1)[0][4] == 'L')


def test_size_args():
    from split_session import gamescope_size_args as g
    check('plain tile', g(1920, 600) == ['-W', '1920', '-H', '600', '-w', '1920', '-h', '600'])
    check('resizable: screen monitor-sized',
          g(1920, 600, nested=(1920, 1200))[:8] == ['-W', '1920', '-H', '600', '-w', '1920', '-h', '1200'])
    check('resizable + render scale: still upscaled with the filter',
          '-F' in g(1920, 600, 50, 'fsr', 2, nested=(1920, 1200)))


def test_size_file():
    import resize
    with tempfile.TemporaryDirectory() as d:
        resize.RUN_ROOT = Path(d)
        resize.write_size(105600, 2, 1920, 600)
        p = resize.size_file(105600, 2)
        check('size file path', p == Path(d) / '105600' / 'i2.size')
        check('size file content', p.read_text().strip() == '1920x600')
        resize.write_size(105600, 2, 960, 1200)
        check('rewritten in place', p.read_text().strip() == '960x1200')

    class S:
        def __init__(self, v): self.v = v
        def get(self, k, d=None): return self.v if k == 'resizeGame' else d
    check('mode: known values', [resize.mode(S(m)) for m in resize.MODES] == list(resize.MODES))
    check('mode: unknown -> None', resize.mode(S('bogus')) is None and resize.mode(None) is None)


if __name__ == '__main__':
    for t in (test_layouts, test_size_args, test_size_file):
        print(t.__name__)
        t()
    print('ALL PASS' if not FAILS else f'{len(FAILS)} FAILED')
    sys.exit(1 if FAILS else 0)

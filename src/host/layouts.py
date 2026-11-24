"""Split layouts per player count, shared by Big Picture (its layout picker)
and the launcher's dynamic players (re-flow when a player leaves or joins).

A layout is a list of tiles (monitor index, x, y, w, h), rects normalized to
their monitor; tile_rects() turns them into desktop pixels. Monitors are
objects with name, x, y, width, height, primary (screen_info.MonitorInfo).
"""

MAX_PLAYERS = 8

# Split layouts per player count, as rects normalized to one monitor.
LAYOUTS = {
    1: [('Full screen', [(0, 0, 1, 1)])],
    2: [('Top and bottom', [(0, 0, 1, .5), (0, .5, 1, .5)]),
        ('Side by side', [(0, 0, .5, 1), (.5, 0, .5, 1)])],
    3: [('One top, two bottom', [(0, 0, 1, .5), (0, .5, .5, .5), (.5, .5, .5, .5)]),
        ('One left, two right', [(0, 0, .5, 1), (.5, 0, .5, .5), (.5, .5, .5, .5)]),
        ('Three columns', [(0, 0, 1 / 3, 1), (1 / 3, 0, 1 / 3, 1), (2 / 3, 0, 1 / 3, 1)]),
        ('Quad, one empty', [(0, 0, .5, .5), (.5, 0, .5, .5), (0, .5, .5, .5)])],
    4: [('Quad', [(0, 0, .5, .5), (.5, 0, .5, .5), (0, .5, .5, .5), (.5, .5, .5, .5)]),
        ('Four columns', [(i / 4, 0, .25, 1) for i in range(4)])],
}


def _grid(cols, rows, n):
    """The first n cells of a cols x rows grid, row by row."""
    return [(c / cols, r / rows, 1 / cols, 1 / rows)
            for r in range(rows) for c in range(cols)][:n]


def _rows(*counts):
    """One row per entry, each split into that many equal tiles."""
    h = 1 / len(counts)
    return [(c / k, i * h, 1 / k, h) for i, k in enumerate(counts) for c in range(k)]


# 5-8 players. With 7-8 the 3x3 grid comes first: its tiles are 16:9, which
# every game handles; 4-column tiles are taller than wide, which some games
# letterbox or clamp.
LAYOUTS.update({
    5: [('Two top, three bottom', _rows(2, 3)),
        ('Three by two, one empty', _grid(3, 2, 5))],
    6: [('Three by two', _grid(3, 2, 6)),
        ('Two by three', _grid(2, 3, 6))],
    7: [('Three by three, two empty', _grid(3, 3, 7)),
        ('Three top, four bottom', _rows(3, 4)),
        ('Four by two, one empty', _grid(4, 2, 7))],
    8: [('Three by three, one empty', _grid(3, 3, 8)),
        ('Four by two', _grid(4, 2, 8))],
})
assert all(len(rects) == n for n, opts in LAYOUTS.items() for _, rects in opts)


def _ordered_monitors(monitors):
    """Monitor indices, primary first, then left to right."""
    return sorted(range(len(monitors)),
                  key=lambda i: (not monitors[i].primary, monitors[i].x, monitors[i].y))


# Top and bottom comes first (the default): most games' UI copes better with
# a wide tile than a tall one. Game.sideBySideFirst puts these first instead.
SIDE_BY_SIDE = {'Side by side', 'One left, two right'}


def _layouts(c, side_by_side_first):
    if not side_by_side_first:
        return LAYOUTS[c]
    return sorted(LAYOUTS[c], key=lambda l: l[0] not in SIDE_BY_SIDE)


def layout_options(monitors, n, side_by_side_first=False):
    """Layouts for n players: [{'id', 'name', 'tiles': [(mon, x, y, w, h)]}],
    rects normalized to their monitor. Every single-screen split on each
    monitor (primary first) plus, with several monitors, the players spread
    over the screens. The first one is the default."""
    n = max(1, min(n, MAX_PLAYERS))
    order = _ordered_monitors(monitors)
    multi = len(monitors) > 1

    def single(mi):
        m = monitors[mi]
        return [{'id': f'{m.name}/{name}',
                 'name': f'{name}  ·  {m.name}' if multi else name,
                 'tiles': [(mi, *r) for r in rects]}
                for name, rects in _layouts(n, side_by_side_first)]

    opts = single(order[0])
    if multi and n > 1:
        k = len(order)
        counts = [n // k + (1 if i < n % k else 0) for i in range(k)]
        used = [(mi, c) for mi, c in zip(order, counts) if c]
        seen = set()
        for variant in range(max(len(LAYOUTS[c]) for _, c in used)):
            tiles, names = [], []
            for mi, c in used:
                vname, rects = _layouts(c, side_by_side_first)[min(variant, len(LAYOUTS[c]) - 1)]
                tiles += [(mi, *r) for r in rects]
                names.append(vname)
            label = ('One per screen' if all(c == 1 for _, c in used)
                     else 'Across screens: ' + ' / '.join(dict.fromkeys(names)))
            if label not in seen:
                seen.add(label)
                opts.append({'id': 'across/' + label, 'name': label, 'tiles': tiles})
    for mi in order[1:]:
        opts += single(mi)
    return opts


def tile_rects(monitors, layout):
    """A layout's tiles in desktop px: [(x, y, w, h, monitor name)]."""
    out = []
    for mi, x, y, w, h in layout['tiles']:
        m = monitors[mi]
        out.append((m.x + round(x * m.width), m.y + round(y * m.height),
                    round(w * m.width), round(h * m.height), m.name))
    return out


def default_rects(monitors, n, side_by_side_first=False, preferred=None):
    """The tiles for n players: the layout whose id is *preferred* if there is
    one for n, else the default (first) one."""
    opts = layout_options(monitors, n, side_by_side_first)
    layout = next((o for o in opts if o['id'] == preferred), opts[0])
    return tile_rects(monitors, layout)

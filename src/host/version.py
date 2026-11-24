"""Janus's version, and comparing versions.

An installed copy (zip or .deb, scripts/package.py) carries a VERSION file
(`<BASE>+git<date>.<hash>[.dirty]`); a git checkout reports BASE + "+dev".
Only the numeric part before "+" or "-" counts in comparisons, so a
checkout counts as BASE.

Handlers can say which Janus they need: `Game.minimumJanusVersion = "0.2";`
(a plain line, read without running the script, like Game.appid). Janus
refuses to load a handler that needs a newer one, and the handler hub
client picks the newest version it can run.
"""

import re
from pathlib import Path
from typing import Optional, Tuple

BASE = '1.0.0'           # bump for releases; scripts/package.py uses it

_ROOT = Path(__file__).resolve().parent.parent.parent
MIN_DECL = re.compile(
    r'''^[ \t]*Game\.minimumJanusVersion[ \t]*=[ \t]*["']([0-9]+(?:\.[0-9]+)*)["'][ \t]*;?''', re.M)


def janus_version() -> str:
    try:
        v = (_ROOT / 'VERSION').read_text().strip()
        if v:
            return v
    except OSError:
        pass
    return BASE + '+dev'


def parse(version: str) -> Tuple[int, ...]:
    """'0.2.1+git…' -> (0, 2, 1); trailing zeros dropped, so 0.2 == 0.2.0.
    Unparsable -> ()."""
    head = re.split(r'[+\-\s]', str(version).strip(), maxsplit=1)[0]
    try:
        parts = [int(p) for p in head.split('.') if p != '']
    except ValueError:
        return ()
    while parts and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def satisfies(minimum: Optional[str], version: Optional[str] = None) -> bool:
    """Does Janus *version* (default: this one) meet *minimum*? No minimum
    (None/'') always does."""
    if not minimum:
        return True
    return parse(version or janus_version()) >= parse(minimum)


def declared_minimum(source: str) -> Optional[str]:
    """The `Game.minimumJanusVersion` a handler's source declares, or None."""
    m = MIN_DECL.search(source)
    return m.group(1) if m else None

"""Controller mappings for Janus input mode (padd.py).

The input daemon reads pads through SDL's GameController API, so a pad works
when SDL has a mapping for it. Two sources (the second wins for a GUID):

1. SDL's built-in database (nothing to do).
2. Janus's own, made in Big Picture's "Set up controller" wizard:
   ``~/.config/janus/pads.json`` → ``mappings`` (GUID → mapping string).

Steam's stored mappings (``config.vdf`` → ``SDL_GamepadBind``) are not used
(dropped 2026-11-22): Steam keeps one for every pad it has seen, in its own
view of the device, and its "Xbox 360 Controller" entry (D-pad on buttons)
overrode SDL's and killed an xpad pad's hat D-pad. Pads SDL doesn't know
are set up once in Janus's wizard instead.

``pads.json`` also holds ``positional``: pad models ("vvvv:pppp", hex) whose
face buttons go by position instead of by label (Nintendo pads: with labels,
the button printed "A" is Xbox A; by position, the bottom one is).
"""

import ctypes
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional

PADS_FILE = Path.home() / '.config' / 'janus' / 'pads.json'
PADD = Path(__file__).resolve().parent / 'padd.py'

_MAPPING = re.compile(r'^[0-9a-fA-F]{32},[^,]+,.*:')


def _load() -> dict:
    try:
        data = json.loads(PADS_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    PADS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = PADS_FILE.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n')
    os.replace(tmp, PADS_FILE)


def model_key(vid: int, pid: int) -> str:
    return f'{vid:04x}:{pid:04x}'


# --- mappings ---------------------------------------------------------------

def janus_mappings() -> Dict[str, str]:
    """GUID → mapping string, made in Big Picture's setup wizard."""
    maps = _load().get('mappings') or {}
    return {g: m for g, m in maps.items() if isinstance(m, str) and _MAPPING.match(m)}


def save_mapping(mapping: str) -> str:
    """Store a wizard-made mapping string; returns its GUID."""
    mapping = mapping.strip()
    if not _MAPPING.match(mapping):
        raise ValueError(f'not an SDL mapping: {mapping[:60]!r}')
    guid = mapping.split(',', 1)[0].lower()
    data = _load()
    data.setdefault('mappings', {})[guid] = mapping
    _save(data)
    return guid


def remove_mapping(guid: str) -> bool:
    data = _load()
    gone = (data.get('mappings') or {}).pop(guid.lower(), None) is not None
    if gone:
        _save(data)
    return gone


def apply_to_sdl(sdl) -> int:
    """SDL_GameControllerAddMapping for every Janus mapping; returns how
    many SDL accepted. *sdl*: a ctypes SDL2 library."""
    fn = sdl.SDL_GameControllerAddMapping
    fn.restype, fn.argtypes = ctypes.c_int, [ctypes.c_char_p]
    return sum(1 for m in janus_mappings().values() if fn(m.encode()) >= 0)


# --- face buttons by position -----------------------------------------------

def positional(vid: int, pid: int) -> bool:
    return model_key(vid, pid) in (_load().get('positional') or [])


def set_positional(vid: int, pid: int, on: bool) -> None:
    data = _load()
    keys = set(data.get('positional') or [])
    (keys.add if on else keys.discard)(model_key(vid, pid))
    data['positional'] = sorted(keys)
    _save(data)


# --- which pads SDL can map -------------------------------------------------

def probe(timeout: float = 8.0) -> Optional[List[dict]]:
    """Every pad SDL sees, with Janus's mappings applied (padd.py probe, in
    its own process): [{path, name, guid, vid, pid, gamecontroller}].
    None when the probe fails (no SDL...)."""
    try:
        r = subprocess.run([sys.executable, str(PADD), 'probe'],
                           capture_output=True, text=True, timeout=timeout)
        return json.loads(r.stdout) if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def unrecognised(controllers: Iterable, probed: Optional[List[dict]] = None) -> List:
    """The *controllers* (ControllerInfo-like, with .index) that SDL has no
    mapping for, so Janus input can't make an Xbox pad of them. [] when the
    probe can't run (nothing to warn about reliably)."""
    from controllers import event_node
    controllers = list(controllers)
    if not controllers:
        return []
    probed = probe() if probed is None else probed
    if probed is None:
        return []
    known = {p['path']: p['gamecontroller'] for p in probed}
    return [c for c in controllers
            if known.get(event_node(c.index)) is False]


# --- recording a mapping (Big Picture's "Set up a controller") ---------------

# (SDL mapping key, what to press, kind). kind decides how a moved axis is
# written: stick (positive = the asked direction, '~' when it goes the other
# way), trigger (a full axis resting at one end, or a half axis), button/dpad
# (a half axis, e.g. a D-pad reported as axes).
SETUP_STEPS = [
    ('a', 'Press the bottom face button (Xbox A)', 'button'),
    ('b', 'Press the right face button (Xbox B)', 'button'),
    ('x', 'Press the left face button (Xbox X)', 'button'),
    ('y', 'Press the top face button (Xbox Y)', 'button'),
    ('dpup', 'Press D-pad up', 'dpad'),
    ('dpdown', 'Press D-pad down', 'dpad'),
    ('dpleft', 'Press D-pad left', 'dpad'),
    ('dpright', 'Press D-pad right', 'dpad'),
    ('leftshoulder', 'Press the left shoulder button (LB / L1)', 'button'),
    ('rightshoulder', 'Press the right shoulder button (RB / R1)', 'button'),
    ('lefttrigger', 'Press the left trigger (LT / L2)', 'trigger'),
    ('righttrigger', 'Press the right trigger (RT / R2)', 'trigger'),
    ('back', 'Press Back / Select', 'button'),
    ('start', 'Press Start', 'button'),
    ('guide', 'Press the Home / Guide button (skip if it has none)', 'button'),
    ('leftstick', 'Click the left stick in (L3)', 'button'),
    ('rightstick', 'Click the right stick in (R3)', 'button'),
    ('leftx', 'Push the left stick to the right', 'stick'),
    ('lefty', 'Push the left stick down', 'stick'),
    ('rightx', 'Push the right stick to the right', 'stick'),
    ('righty', 'Push the right stick down', 'stick'),
]
_MOVED = 16000      # an axis this far from rest counts as pushed
_RESTED = 8000      # ... and back within this as released


class MappingRecorder:
    """Builds an SDL mapping for one pad from raw snapshots of its state.

    feed((buttons, axes, hats)) with SDL's raw joystick values (buttons 0/1,
    axes -32768..32767, hats as SDL_HAT bits). The first snapshot is the rest
    state. Each step waits for one new input, then for everything to be let go.
    """

    def __init__(self, guid: str, name: str, steps=SETUP_STEPS):
        self.guid = guid
        self.name = re.sub(r'[,\n]', ' ', name).strip() or 'Gamepad'
        self.steps = steps
        self.step = 0
        self.bindings: Dict[str, str] = {}
        self.rest = None
        self.waiting_release = False
        self.taken: Optional[str] = None   # last press refused: the key it's set as

    @property
    def finished(self) -> bool:
        return self.step >= len(self.steps)

    @property
    def prompt(self) -> str:
        return '' if self.finished else self.steps[self.step][1]

    def skip(self):
        if not self.finished:
            self.bindings.pop(self.steps[self.step][0], None)
            self.step += 1

    def back(self):
        if self.step > 0:
            self.step -= 1
            self.bindings.pop(self.steps[self.step][0], None)
            self.waiting_release = True

    def feed(self, state) -> bool:
        """True when this snapshot completed a step."""
        buttons, axes, hats = (list(x) for x in state)
        if self.rest is None:
            self.rest = (buttons, axes, hats)
            return False
        rb, ra, rh = self.rest
        # A button or hat held in the first snapshot (the A that picked the pad
        # in the wizard) isn't rest: once it's let go it can be recorded too.
        # Before this, A stayed "pressed at rest" and was never taken for A.
        for i, b in enumerate(buttons):
            if i < len(rb) and rb[i] and not b:
                rb[i] = 0
        for i, h in enumerate(hats):
            if i < len(rh) and rh[i] and not h:
                rh[i] = 0
        if self.finished:
            return False
        if self.waiting_release:
            if self._released(buttons, axes, hats):
                self.waiting_release = False
            return False
        key, _, kind = self.steps[self.step]
        candidates = self._detect(kind, buttons, axes, hats)
        bound = {v: k for k, v in self.bindings.items()}
        free = [c for c in candidates if c not in bound]
        # Something pressed, but all of it is set already: say so (the
        # wizard shows it), instead of silently ignoring the press.
        self.taken = bound[candidates[0]] if candidates and not free else None
        if not free:
            return False
        self.bindings[key] = free[0]
        self.step += 1
        self.waiting_release = True
        return True

    def _released(self, buttons, axes, hats) -> bool:
        rb, ra, rh = self.rest
        return (all(not b or rb[i] for i, b in enumerate(buttons) if i < len(rb))
                and all(abs(v - ra[i]) < _RESTED for i, v in enumerate(axes) if i < len(ra))
                and all(h == rh[i] for i, h in enumerate(hats) if i < len(rh)))

    def _detect(self, kind, buttons, axes, hats) -> List[str]:
        """Every input that differs from rest, most likely first: buttons,
        hat directions, then axes by how far they moved."""
        rb, ra, rh = self.rest
        out = [f'b{i}' for i, b in enumerate(buttons) if b and i < len(rb) and not rb[i]]
        out += [f'h{i}.{h}' for i, h in enumerate(hats)
                if i < len(rh) and h != rh[i] and h in (1, 2, 4, 8)]
        moved = sorted(((abs(v - ra[i]), i, v) for i, v in enumerate(axes)
                        if i < len(ra) and abs(v - ra[i]) > _MOVED), reverse=True)
        for _, i, v in moved:
            out.append(self._axis_binding(kind, i, v, ra[i]))
        return out

    @staticmethod
    def _axis_binding(kind, i, v, rest) -> str:
        if kind == 'stick':
            return f'a{i}' if v > rest else f'a{i}~'
        if kind == 'trigger':
            if rest < -_MOVED:
                return f'a{i}'           # full axis: -32768 released .. 32767 pressed
            if rest > _MOVED:
                return f'a{i}~'
        return f'+a{i}' if v > rest else f'-a{i}'

    def values(self, state) -> Dict[str, float]:
        """What each recorded control reads in *state*, as the mapping would
        make it: buttons / D-pad / triggers 0..1, stick axes -1..1."""
        buttons, axes, hats = state
        kinds = {k: kind for k, _, kind in self.steps}
        out: Dict[str, float] = {}
        for key, b in self.bindings.items():
            out[key] = round(_binding_value(b, kinds.get(key), buttons, axes, hats), 2)
        return out

    def mapping(self) -> str:
        parts = [f'{k}:{v}' for k, v in self.bindings.items()]
        return ','.join([self.guid, self.name] + parts + ['platform:Linux']) + ','


# Short names of the SETUP_STEPS keys, for messages.
LABELS = {
    'a': 'A', 'b': 'B', 'x': 'X', 'y': 'Y', 'dpup': 'D-pad up', 'dpdown': 'D-pad down',
    'dpleft': 'D-pad left', 'dpright': 'D-pad right', 'leftshoulder': 'LB',
    'rightshoulder': 'RB', 'lefttrigger': 'LT', 'righttrigger': 'RT', 'back': 'Back',
    'start': 'Start', 'guide': 'Guide', 'leftstick': 'L3', 'rightstick': 'R3',
    'leftx': 'left stick right', 'lefty': 'left stick down',
    'rightx': 'right stick right', 'righty': 'right stick down',
}


def _binding_value(b: str, kind, buttons, axes, hats) -> float:
    def axis(i):
        return axes[i] if i < len(axes) else 0
    m = re.fullmatch(r'b(\d+)', b)
    if m:
        i = int(m[1])
        return float(i < len(buttons) and bool(buttons[i]))
    m = re.fullmatch(r'h(\d+)\.(\d+)', b)
    if m:
        i = int(m[1])
        return float(i < len(hats) and bool(hats[i] & int(m[2])))
    m = re.fullmatch(r'([+-]?)a(\d+)(~?)', b)
    if not m:
        return 0.0
    v = axis(int(m[2]))
    if m[1] == '+':
        return max(0, v) / 32767
    if m[1] == '-':
        return max(0, -v) / 32768
    if m[3]:
        v = -1 - v                        # inverted (-32768 <-> 32767)
    if kind == 'trigger':
        return (v + 32768) / 65535
    return max(-1.0, v / 32767)

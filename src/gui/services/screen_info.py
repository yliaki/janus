"""Screen resolution detection.

On KWin Wayland, native gamescope windows are invisible to X tooling and xrandr
only sees XWayland's (often unreliable) view, so we use kscreen-doctor (via
``host/wayland_session``) to report each output's **native pixel** geometry —
the coordinate space the Wayland "Path A" launch uses (it pins the output to
scale 1.0). Off KWin Wayland a single 1920x1080 placeholder is returned.
"""

import sys
from dataclasses import dataclass
from pathlib import Path

# host/ holds wayland_session (kscreen-doctor wrapper); add it to the path once.
_HOST = str(Path(__file__).parent.parent.parent / 'host')
if _HOST not in sys.path:
    sys.path.insert(0, _HOST)


@dataclass
class MonitorInfo:
    """Physical monitor info (native px)."""
    name: str        # "eDP-1", "HDMI-1", "DP-0"
    width: int
    height: int
    x: int           # offset in the desktop layout
    y: int
    primary: bool


def _wayland_monitors() -> list[MonitorInfo]:
    """Native-px monitor geometry on KWin Wayland (via kscreen-doctor)."""
    try:
        import wayland_session as ws
        if not ws.is_wayland():
            return []
        return [
            MonitorInfo(o.name, o.width, o.height, o.pos_x, o.pos_y, o.primary)
            for o in ws.get_outputs()
        ]
    except Exception:
        return []


def get_all_monitors() -> list[MonitorInfo]:
    """Return info for all connected monitors (via kscreen-doctor)."""
    return _wayland_monitors() or [MonitorInfo("Unknown", 1920, 1080, 0, 0, True)]


def get_screen_resolution() -> tuple[int, int]:
    """Return (width, height) of the primary monitor."""
    monitors = get_all_monitors()
    for m in monitors:
        if m.primary:
            return m.width, m.height
    return monitors[0].width, monitors[0].height

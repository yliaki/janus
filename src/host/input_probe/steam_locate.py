"""Locate the REAL Valve libsteam_api.so at runtime.

Never vendored (we can't redistribute Valve code) — loaded dynamically from the Steam
install. Honors JANUS_STEAM_API_LIB. Picks the arch matching this Python process.
"""
from __future__ import annotations

import os
import struct

_PY_BITS = struct.calcsize("P") * 8


def find_libsteam_api() -> str | None:
    env = os.environ.get("JANUS_STEAM_API_LIB")
    if env and os.path.exists(env):
        return env
    home = os.path.expanduser("~")
    rt = "steamrt64" if _PY_BITS == 64 else "steamrt32"
    candidates = [
        f"{home}/.local/share/Steam/{rt}/libsteam_api.so",
        f"{home}/.steam/steam/{rt}/libsteam_api.so",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return None

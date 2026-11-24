"""Private /dev per game instance (Janus input mode).

A game instance runs in its own mount namespace (bubblewrap, unprivileged)
whose /dev is the host's minus every input device: /dev/input holds only that
player's virtual pad (padd.py), and hidraw*, uinput and /dev/bus/usb are gone.
The kernel enforces it — static binaries, raw syscalls, Wine's winebus, SDL's
udev/inotify scan or HIDAPI: nothing can open another player's pad, because
it doesn't exist in there. Steam's pressure-vessel nests fine inside (it
re-binds our /dev). NOTES.md "Janus-owned input isolation".

Without user namespaces (bwrap fails) the hook's allowlist
(JANUS_ALLOW_DEVICES) is the fallback.
"""

import functools
import os
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional

# Entries of /dev the game doesn't get at all.
_HIDDEN = ('input', 'uinput', 'uhid', 'bus')


@functools.lru_cache(maxsize=1)
def bwrap() -> Optional[str]:
    """A bubblewrap that works here (unprivileged user namespaces), or None:
    the system's, else the one Steam's runtimes ship (srt-bwrap)."""
    candidates = [shutil.which('bwrap')]
    try:
        from steam import find_all_steam_roots, steamapps_dirs
        for root in find_all_steam_roots():
            for apps in steamapps_dirs(root):
                for rt in sorted((apps / 'common').glob('SteamLinuxRuntime*')):
                    candidates.append(str(rt / 'pressure-vessel' / 'libexec'
                                          / 'steam-runtime-tools-0' / 'srt-bwrap'))
    except Exception:
        pass
    for b in candidates:
        if not b or not os.access(b, os.X_OK):
            continue
        try:
            r = subprocess.run([b, '--dev-bind', '/', '/', '--tmpfs', '/dev/input', 'true'],
                               capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode == 0:
            return b
    return None


def available() -> bool:
    return bwrap() is not None


def wrap(cmd: List[str], nodes: List[str]) -> List[str]:
    """*cmd* run with a /dev that shows only *nodes* of the input devices."""
    b = bwrap()
    if b is None:
        raise OSError('bubblewrap is unavailable (no unprivileged user namespaces)')
    args = [b, '--die-with-parent', '--dev-bind', '/', '/', '--tmpfs', '/dev']
    for name in sorted(os.listdir('/dev')):
        if name in _HIDDEN or name.startswith('hidraw'):
            continue
        p = f'/dev/{name}'
        if os.path.islink(p):
            args += ['--symlink', os.readlink(p), p]
        elif os.path.exists(p):
            args += ['--dev-bind', p, p]
    args += ['--dir', '/dev/input']
    for node in nodes:
        args += ['--dev-bind', node, node]
    return args + ['--'] + list(cmd)

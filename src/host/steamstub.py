"""SteamStub DRM (Game.steamStubExes): unpack wrapped exes with Steamless.

A SteamStub-wrapped exe (PE section ``.bind``) talks to the Steam client
before the game starts and relaunches it through real Steam, which bypasses
Goldberg, the isolated prefix and the per-instance mirror. Janus unpacks each
listed exe once with the vendored Steamless (scripts/steamless.sh, under a
Proton's Wine) into a cache, ``~/.local/share/janus/steamless/<appid>/``, and
the mirror gets the unpacked copy in place of the original. The Steam install
is never written. The cache is redone when the original changes (game update).
See NOTES.md "SteamStub DRM".
"""

import fcntl
import os
import shutil
import struct
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

REPO = Path(__file__).resolve().parent.parent.parent
CACHE = Path.home() / '.local' / 'share' / 'janus' / 'steamless'


def pe_sections(path: Path) -> Optional[List[str]]:
    """PE section names of *path*, None if it isn't a PE file."""
    try:
        with open(path, 'rb') as f:
            d = f.read(8192)
        if d[:2] != b'MZ':
            return None
        pe = struct.unpack_from('<I', d, 0x3c)[0]
        n = struct.unpack_from('<H', d, pe + 6)[0]
        opt = struct.unpack_from('<H', d, pe + 20)[0]
        off = pe + 24 + opt
        return [d[off + 40 * i: off + 40 * i + 8].rstrip(b'\0').decode(errors='replace')
                for i in range(n)]
    except (OSError, struct.error):
        return None


def is_wrapped(path: Path) -> bool:
    return '.bind' in (pe_sections(path) or [])


def find_proton(game) -> Optional[Path]:
    """A Proton with files/bin/wine to run Steamless: the game's own, else any
    installed one (all libraries, compatibilitytools.d)."""
    from steam import steamapps_dirs
    cands = [game.proton_path] if game.proton_path else []
    root = Path(game.steam_root)
    for sa in steamapps_dirs(root):
        cands += sorted((sa / 'common').glob('Proton*'), reverse=True)
    cands += sorted((root / 'compatibilitytools.d').glob('*'))
    return next((Path(p) for p in cands if (Path(p) / 'files/bin/wine').is_file()), None)


def _stamp(exe: Path) -> str:
    st = exe.stat()
    return f'{st.st_size} {st.st_mtime_ns}\n'


def unpacked(game, rel: str) -> Optional[Path]:
    """The Steamless output for install-relative *rel*, made now if the cache
    is missing or stale. None if the exe isn't SteamStub-wrapped (use it as
    is). Raises RuntimeError when unpacking fails."""
    exe = game.install_dir / rel
    if not exe.is_file():
        raise RuntimeError(f'Game.steamStubExes: {exe} does not exist')
    if not is_wrapped(exe):
        print(f'[janus] {rel} is not SteamStub-wrapped (no .bind); using it as is',
              flush=True)
        return None
    out_dir = CACHE / str(game.appid) / Path(rel).parent
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / (exe.name + '.unpacked.exe')
    stamp = out_dir / (exe.name + '.source')
    # Instances launch in parallel: one unpacks, the others wait for it.
    with open(out_dir / (exe.name + '.lock'), 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if out.is_file() and stamp.is_file() and stamp.read_text() == _stamp(exe):
            return out
        proton = find_proton(game)
        if proton is None:
            raise RuntimeError(f'{rel} is SteamStub-wrapped and needs unpacking, but '
                               f'no Proton is installed to run Steamless with. '
                               f'Install Proton in Steam and launch again.')
        print(f'[janus] Unpacking SteamStub DRM from {rel} with Steamless '
              f'(once per game version; the first time takes about a minute)…',
              flush=True)
        work = out_dir / exe.name     # Steamless writes <input>.unpacked.exe
        for p in (work, out):
            p.unlink(missing_ok=True)
        shutil.copy2(exe, work)
        try:
            r = subprocess.run([str(REPO / 'scripts' / 'steamless.sh'), str(work)],
                               env={**os.environ, 'PROTON_DIR': str(proton)},
                               capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            r = None
        finally:
            work.unlink(missing_ok=True)
        if r is None or r.returncode != 0 or not out.is_file():
            log = (r.stdout + r.stderr).strip() if r else 'timed out after 10 minutes'
            raise RuntimeError(f'Could not remove the SteamStub DRM from {rel} '
                               f'(Steamless failed):\n{log[-1500:]}')
        stamp.write_text(_stamp(exe))
        print(f'[janus] Unpacked {rel} -> {out}', flush=True)
        return out


def replacements(game, exes: Optional[List[str]]) -> Dict[str, Path]:
    """Game.steamStubExes -> create_goldberg_mirror(replace_files=...)."""
    out: Dict[str, Path] = {}
    for rel in exes or []:
        src = unpacked(game, str(rel))
        if src is not None:
            out[str(rel)] = src
    return out

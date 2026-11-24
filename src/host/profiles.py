"""Player profiles: who plays, independent of which instance they are.

A profile is a person. It carries an IDENTITY (name + Steam ID, used as the
Goldberg account and by handlers) and owns per-game STORAGE
(``~/.local/share/janus/profiles/<id>/<appid>/``: the isolated home, Wine
prefix and whatever a handler keeps there). The instance number only decides
the session role (host, music, ports, tile).

Two kinds:

* the OWNER (id ``owner``): the Steam account logged in on this machine. Its
  name and Steam ID come from Steam's ``loginusers.vdf``, it is never stored.
  It is the only profile that plays on the real saves (real home / prefix /
  Steam Cloud) where the isolation mode allows that ("secondary").
* VIRTUAL profiles ("Alice", "Bob"): stored in ``~/.config/janus/profiles.json``
  with a Steam ID allocated once at creation, so saves a game keys by Steam ID
  follow the person.

A launch that names no profile plays instance 1 as the owner and instance N
as "Player N" (created on first use), see ``default_for_instance``.
"""

import json
import os
import random
import re
import secrets
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

OWNER_ID = 'owner'

CONFIG_FILE = Path.home() / '.config' / 'janus' / 'profiles.json'
DATA_DIR = Path.home() / '.local' / 'share' / 'janus' / 'profiles'

# The owner's identity when no Steam account is found.
_FALLBACK_OWNER_STEAM_ID = '76561198000000001'

_lock = threading.RLock()


@dataclass
class Profile:
    id: str
    name: str
    steam_id: str
    owner: bool = False

    def to_dict(self) -> dict:
        return {'id': self.id, 'name': self.name, 'steamId': self.steam_id}

    def ctx(self) -> dict:
        """The handler-script view (ctx.profile)."""
        return {'id': self.id, 'name': self.name, 'steamId': self.steam_id,
                'owner': self.owner}

    def data_dir(self, appid: int) -> Path:
        """This profile's storage for one game (created on demand)."""
        return DATA_DIR / self.id / str(appid)


# --- the owner (real Steam account) -------------------------------------

_owner_cache: Optional[tuple] = None    # (loginusers.vdf mtimes, Profile)


def _loginusers_files() -> List[Path]:
    try:
        try:
            from steam import find_all_steam_roots
        except ImportError:
            from .steam import find_all_steam_roots
        return [r / 'config' / 'loginusers.vdf' for r in find_all_steam_roots()]
    except Exception:  # noqa: BLE001
        return []


def steam_owner() -> Profile:
    """The Steam account of this machine (cached until loginusers.vdf changes)."""
    global _owner_cache
    files = _loginusers_files()
    stamp = tuple((str(f), f.stat().st_mtime if f.exists() else 0) for f in files)
    if _owner_cache is None or _owner_cache[0] != stamp:
        _owner_cache = (stamp, _read_steam_owner())
    p = _owner_cache[1]
    return Profile(p.id, p.name, p.steam_id, owner=True)


def _read_steam_owner() -> Profile:
    """The most recently used account in loginusers.vdf (a placeholder
    identity when Steam isn't found)."""
    try:
        try:
            from steam import find_all_steam_roots, parse_vdf
        except ImportError:
            from .steam import find_all_steam_roots, parse_vdf
        best = None
        for root in find_all_steam_roots():
            users = parse_vdf(root / 'config' / 'loginusers.vdf').get('users', {})
            for sid, u in users.items():
                if not (isinstance(u, dict) and sid.isdigit()):
                    continue
                key = (u.get('MostRecent') == '1', int(u.get('Timestamp', '0') or 0))
                if best is None or key > best[0]:
                    best = (key, sid, u.get('PersonaName') or u.get('AccountName') or 'Player 1')
        if best:
            return Profile(OWNER_ID, best[2], best[1], owner=True)
    except Exception as e:  # noqa: BLE001 — never block a launch on this
        print(f'[janus] profiles: reading the Steam account failed: {e}', flush=True)
    return Profile(OWNER_ID, 'Player 1', _FALLBACK_OWNER_STEAM_ID, owner=True)


# --- store ----------------------------------------------------------------

def _read() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text())
    except FileNotFoundError:
        return {'version': 1, 'profiles': []}
    except (OSError, ValueError) as e:
        print(f'[janus] profiles: {CONFIG_FILE} unreadable ({e}); starting empty', flush=True)
        return {'version': 1, 'profiles': []}


def _write(data: dict):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(data, indent=2) + '\n')
    os.replace(tmp, CONFIG_FILE)


def _from_dict(d: dict) -> Profile:
    return Profile(str(d['id']), str(d.get('name') or d['id']), str(d['steamId']))


def _virtual(data: dict) -> List[Profile]:
    return [_from_dict(d) for d in data.get('profiles', [])
            if isinstance(d, dict) and d.get('id') and d.get('steamId')]


def load() -> List[Profile]:
    """All profiles: the owner first, then the virtual ones in creation order."""
    with _lock:
        return [steam_owner()] + _virtual(_read())


def get(profile_id: str) -> Optional[Profile]:
    return next((p for p in load() if p.id == profile_id), None)


def find(name_or_id: str) -> Optional[Profile]:
    """By id, else by name (case-insensitive)."""
    ps = load()
    return (next((p for p in ps if p.id == name_or_id), None)
            or next((p for p in ps if p.name.lower() == name_or_id.lower()), None))


def _new_id(taken) -> str:
    while True:
        pid = 'p_' + secrets.token_hex(3)
        if pid not in taken:
            return pid


def _new_steam_id(taken) -> str:
    # Individual-account SteamID64s, clear of the owner fallback (and of the
    # +100 IDs handlers derive for helper processes).
    while True:
        sid = str(76561198100000000 + random.randrange(800_000_000))
        if sid not in taken:
            return sid


def _clean_name(name: str) -> str:
    name = re.sub(r'\s+', ' ', str(name or '')).strip()
    if not name:
        raise ValueError('A profile needs a name.')
    return name[:32]


def create(name: str) -> Profile:
    with _lock:
        name = _clean_name(name)
        data = _read()
        ps = _virtual(data)
        if name.lower() == steam_owner().name.lower() or any(
                p.name.lower() == name.lower() for p in ps):
            raise ValueError(f'There already is a profile called "{name}".')
        taken_ids = {p.id for p in ps} | {OWNER_ID}
        p = Profile(_new_id(taken_ids), name, _new_steam_id({p.steam_id for p in ps}))
        data.setdefault('profiles', []).append(p.to_dict())
        _write(data)
        return p


def rename(profile_id: str, name: str) -> Profile:
    with _lock:
        if profile_id == OWNER_ID:
            raise ValueError("The Steam account's name comes from Steam.")
        name = _clean_name(name)
        data = _read()
        if any(d.get('id') != profile_id and str(d.get('name', '')).lower() == name.lower()
               for d in data.get('profiles', [])) or name.lower() == steam_owner().name.lower():
            raise ValueError(f'There already is a profile called "{name}".')
        for d in data.get('profiles', []):
            if d.get('id') == profile_id:
                d['name'] = name
                _write(data)
                return _from_dict(d)
        raise KeyError(profile_id)


def delete(profile_id: str, *, delete_data: bool = False):
    """Remove a virtual profile. Its saves stay on disk unless *delete_data*."""
    with _lock:
        if profile_id == OWNER_ID:
            raise ValueError("The Steam account's profile can't be removed.")
        data = _read()
        before = len(data.get('profiles', []))
        data['profiles'] = [d for d in data.get('profiles', []) if d.get('id') != profile_id]
        if len(data['profiles']) == before:
            raise KeyError(profile_id)
        _write(data)
        # The pad memory may point at it; a stale entry is harmless (checked
        # against the known profiles) but drop it anyway.
        pads = data.get('pads') or {}
        if any(v == profile_id for v in pads.values()):
            data['pads'] = {k: v for k, v in pads.items() if v != profile_id}
            _write(data)
    if delete_data:
        _remove_tree(DATA_DIR / profile_id)


def _remove_tree(path: Path):
    """rmtree that also removes read-only entries (Wine prefixes, unpacked
    JREs). Symlinks are unlinked, never followed: an isolated prefix links
    into the shared one. Raises OSError if something stays behind."""
    import shutil
    import stat

    def retry(func, p, _exc):
        try:
            os.chmod(os.path.dirname(p), stat.S_IRWXU)
            if not os.path.islink(p):
                os.chmod(p, stat.S_IRWXU)
            func(p)
        except OSError:
            pass

    if path.is_symlink():
        path.unlink()
        return
    if not path.exists():
        return
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:
        shutil.rmtree(path, onerror=retry)
    if path.exists():
        raise OSError(f'Could not delete everything in {path}.')


def peek_default(n: int) -> Optional[Profile]:
    """default_for_instance() without creating anything: None when there is
    no "Player N" profile yet (for display)."""
    if n <= 1:
        return steam_owner()
    return find(f'Player {n}')


def default_for_instance(n: int) -> Profile:
    """The profile an instance plays as when none was chosen: the owner for
    instance 1, the profile "Player N" (created on first use) for the others."""
    with _lock:
        return peek_default(n) or create(f'Player {n}')


def resolve(profile: Optional[str], instance: int) -> Profile:
    """A launch's profile: *profile* (id or name), else the instance default."""
    if profile:
        p = find(profile)
        if p is None:
            raise ValueError(f'No profile "{profile}". Known: '
                             + ', '.join(q.name for q in load()))
        return p
    return default_for_instance(instance)


def resolve_session(entries) -> List[Profile]:
    """Profiles for a session's instances, *entries* = [(profile or None,
    instance number)]. Raises ValueError when one profile would play twice
    (two instances writing the same saves)."""
    out = [resolve(prof, n) for prof, n in entries]
    seen = {}
    for p, (_, n) in zip(out, entries):
        if p.id in seen:
            raise ValueError(f'{p.name} is assigned to both player {seen[p.id]} '
                             f'and player {n}. Pick a different profile for one of them.')
        seen[p.id] = n
    return out


# --- pad -> profile memory (Big Picture) ----------------------------------

def remembered_for_pad(pad_key: str) -> Optional[str]:
    data = _read() or {}
    return (data.get('pads') or {}).get(pad_key)


def remember_pads(mapping: Dict[str, str]):
    with _lock:
        data = _read()
        data.setdefault('pads', {}).update(mapping)
        _write(data)

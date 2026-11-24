"""What a player profile has saved for a game: the line the profile menus
show under each profile ("1.4 GB · played 3 days ago"), so a player can tell
which profile holds the saves they want.

The default line comes from the profile's storage for the game
(``profiles/<id>/<key>/``: isolated home, Wine prefix, handler data). A
handler can replace it with ``Game.profileInfo(ctx)`` (world names, hours
played, …), see SCRIPTING.md. The hook runs read-only: mutating ``Janus.*``
calls throw.
"""

import os
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, Optional, Tuple

import profiles
import scripting

# Not saves: per-instance scratch space Janus recreates.
_SKIP_TOP = {'tmp'}


def dir_stats(path: Path) -> Optional[Tuple[int, float]]:
    """(bytes, newest mtime) of everything under *path*, symlinks not
    followed (an isolated Wine prefix links to the shared one). None when
    *path* doesn't exist."""
    try:
        top = os.lstat(path)
    except OSError:
        return None
    total, newest = 0, 0.0
    stack = [(str(path), True)]
    while stack:
        d, is_top = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                if is_top and e.name in _SKIP_TOP:
                    continue
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                if e.is_dir(follow_symlinks=False):
                    stack.append((e.path, False))
                elif not e.is_symlink():
                    total += st.st_size
                    newest = max(newest, st.st_mtime)
    return total, (newest or top.st_mtime)


def human_size(n: int) -> str:
    for unit in ('bytes', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            if unit == 'bytes':
                return f'{n} bytes'
            return f'{n:.1f} {unit}' if n < 10 else f'{n:.0f} {unit}'
        n /= 1024
    return f'{n:.0f} TB'


def human_age(ts: float, now: Optional[float] = None) -> str:
    d = max(0.0, (now or time.time()) - ts)
    if d < 3600:
        return 'just now'
    if d < 86400:
        h = int(d // 3600)
        return f'{h} hour{"s" if h > 1 else ""} ago'
    days = int(d // 86400)
    if days == 1:
        return 'yesterday'
    if days < 60:
        return f'{days} days ago'
    return time.strftime('%b %Y', time.localtime(ts))


def saves_tag(prof, isolation: str, profile_saves: bool, non_steam: bool) -> str:
    """'real' (the Steam account's own saves), 'shared' (everyone plays on
    the same data: profiles are only names) or 'own'. Non-Steam games keep
    every player's saves in Janus ('own')."""
    if non_steam:
        return 'own'
    if isolation == 'none' and not profile_saves:
        return 'shared'
    if prof.owner and isolation in ('secondary', 'none'):
        return 'real'
    return 'own'


def default_text(tag: str, stats: Optional[Tuple[int, float]]) -> str:
    if tag == 'real':
        return 'Real Steam saves'
    if tag == 'shared':
        return 'Shared saves'
    if not stats or stats[0] == 0:
        return 'No saves yet'
    return f'{human_size(stats[0])} · played {human_age(stats[1])}'


def _ctx(script, key, prof, own, tag, stats, text) -> Dict:
    install = None
    if scripting.is_steam_key(key):
        try:
            from steam import find_app_install_dir
            d = find_app_install_dir(int(key))
            install = str(d) if d else None
        except Exception:  # noqa: BLE001
            install = None
    else:
        import nonsteam
        install = str(nonsteam.game_dir(key))
    pctx = prof.ctx()
    if own and prof.owner:
        pctx = {**pctx, 'name': own['name']}
    return {'appid': key, 'gameName': str(script.get('name') or key),
            'installDir': install, 'gameDir': install,
            'profile': pctx, 'saveRoot': str(prof.data_dir(key)),
            'profilesDir': str(profiles.DATA_DIR), 'home': str(Path.home()),
            'saves': tag,
            'saveSize': stats[0] if stats else None,
            'lastPlayed': stats[1] if stats else None,
            'defaultText': text, 'options': script.options}


def profile_texts(key, handler_path: Optional[Path], option_values: Dict,
                  profs: Iterable, tag_of: Callable, own: Optional[Dict] = None,
                  ) -> Dict[str, str]:
    """{profile id: line} for every profile in *profs*. *tag_of(prof)* is
    saves_tag for the session's settings. Loads its own copy of the handler
    (so this can run on a worker thread next to a session's script)."""
    script = None
    if handler_path is not None:
        try:
            script = scripting.HandlerScript(handler_path, key,
                                             option_values=option_values)
            if not script.has_hook('profileInfo'):
                script = None
        except scripting.HandlerError:
            script = None
    out = {}
    for prof in profs:
        tag = tag_of(prof)
        stats = dir_stats(prof.data_dir(key))
        text = default_text(tag, stats)
        if script is not None:
            text = _handler_text(script, key, prof, own, tag, stats, text)
        out[prof.id] = text
    return out


def _handler_text(script, key, prof, own, tag, stats, text) -> str:
    script._read_only_hook = 'profileInfo'   # mutating Janus.* calls throw
    try:
        r = script.invoke('profileInfo', soft=True,
                          ctx=_ctx(script, key, prof, own, tag, stats, text))
    finally:
        script._read_only_hook = ''
    ret = (r or {}).get('ret')
    if isinstance(ret, (str, int, float)) and str(ret).strip():
        return ' '.join(str(ret).split())[:120]
    return text


def total_stats(profile_id: str) -> Optional[Tuple[int, float]]:
    """Everything a profile keeps, all games (for the delete confirmation)."""
    return dir_stats(profiles.DATA_DIR / profile_id)

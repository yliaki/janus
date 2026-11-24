"""Client for the Janus Handler Hub (the janus_website API).

Read-only and public: list/search the hub's games, download a game's handler,
verify it and install it into the user handler dir (~/.config/janus/games/),
(during development a handler in the repo's handlers/ takes precedence over
it, with a warning in both GUIs — SCRIPTING.md §1).

  format js  -> ~/.config/janus/games/<slug>.js
  format zip -> ~/.config/janus/games/<slug>/handler.js (+ payload/)

Games are matched by their game key (scripting.parse_game_key): the Steam
appid (`Game.appid = N;`, the hub's `steam_appid`) or, for a non-Steam game,
its id (`Game.id = "minecraft";`, the hub's `janus_id`).

What the hub installed is recorded in ~/.config/janus/hub.json
({key: {id, slug, name, version, sha256, format, cover_url}}), so updates
can be offered when the hub's latest_version grows and a reinstall replaces
the previous files. A handler the user wrote themselves for the same game is
never touched: installing over it is refused.

The base URL comes from $JANUS_HUB_URL, default the real hub
https://janus-split-screen.org (the development server: JANUS_HUB_URL=
http://127.0.0.1:8000).
"""

import hashlib
import http.client
import io
import json
import os
import re
import shutil
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Dict, List, Optional

import scripting
import version as janus_version

PRODUCTION_URL = 'https://janus-split-screen.org'
DEV_URL = 'http://127.0.0.1:8000'
DEFAULT_URL = PRODUCTION_URL      # the dev server: JANUS_HUB_URL=http://127.0.0.1:8000
MAX_FILES = 500
MAX_UNPACKED = 50 * 1024 * 1024
MAX_DOWNLOAD = 5 * 1024 * 1024        # API answers
# A handler file: a zip is at most what it unpacks to. (Uploads to the hub
# are at most 5 MB, but handlers it imports from Janus's repo can be bigger:
# Gunfire Reborn's and PAYDAY 2's payloads are ~9 MB zipped.)
MAX_HANDLER_DOWNLOAD = MAX_UNPACKED
_SLUG = re.compile(r'^[a-z0-9-]+$')
# Python's default ("Python-urllib/3.x") is refused by Cloudflare's Browser
# Integrity Check in front of the hub (error 1010, HTTP 403)
USER_AGENT = f'Janus/{janus_version.janus_version()} (+{PRODUCTION_URL})'


class HubError(Exception):
    """The hub is unreachable, or a download failed verification."""


def base_url() -> str:
    return os.environ.get('JANUS_HUB_URL', DEFAULT_URL).rstrip('/')


def _user_dir() -> Path:
    return scripting.user_handler_dir()


def _manifest_path() -> Path:
    return Path.home() / '.config' / 'janus' / 'hub.json'


def _fetch(url: str, headers: dict, timeout: float, limit: int, redirects: int = 3):
    """GET with http.client: (status, body). Not urllib.request, which always
    sends "Connection: close": through Cloudflare (janus-split-screen.org)
    that cut downloads of a few MB short (2026-11-23, London edge; the origin
    had sent everything). With a proxy configured (https_proxy…), urllib,
    which honours it."""
    if urllib.request.getproxies():
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read(limit + 1)
        except urllib.error.HTTPError as e:
            return e.code, b''
    parts = urllib.parse.urlsplit(url)
    cls = http.client.HTTPSConnection if parts.scheme == 'https' else http.client.HTTPConnection
    conn = cls(parts.netloc, timeout=timeout)
    try:
        conn.request('GET', parts.path + (f'?{parts.query}' if parts.query else ''), headers=headers)
        r = conn.getresponse()
        if r.status in (301, 302, 303, 307, 308) and r.getheader('Location') and redirects:
            return _fetch(urllib.parse.urljoin(url, r.getheader('Location')), headers,
                          timeout, limit, redirects - 1)
        return r.status, r.read(limit + 1)
    finally:
        conn.close()


def _get(url: str, timeout: float = 10, headers: Optional[dict] = None,
         limit: int = MAX_DOWNLOAD) -> bytes:
    headers = {'Accept': 'application/json', 'User-Agent': USER_AGENT, **(headers or {})}
    for attempt in range(3):
        try:
            status, data = _fetch(url, headers, timeout, limit)
            break
        except http.client.IncompleteRead as e:   # cut off: try again
            if attempt == 2:
                raise HubError(f'{url}: the download was cut off; try again') from e
        except (http.client.HTTPException, urllib.error.URLError, OSError) as e:
            reason = getattr(e, 'reason', e)
            raise HubError(f"Can't reach the handler hub at {base_url()} ({reason})") from e
    if status >= 400:
        raise HubError(f'{url}: HTTP {status}')
    if len(data) > limit:
        raise HubError(f'{url}: larger than {limit // (1024 * 1024)} MB')
    return data


def _get_json(url: str):
    try:
        return json.loads(_get(url))
    except ValueError as e:
        raise HubError(f'{url}: not JSON') from e


# --- queries ---------------------------------------------------------------

# The hub's sort orders (API `ordering`), in the order a GUI cycles them.
ORDERINGS = {'popular': 'Most installed', 'uploaded': 'Recently updated',
             'added': 'Recently added', 'name': 'A–Z'}


def game_key(game: dict):
    """A hub game's key: its Steam appid, else its non-Steam id, else None
    (a game Janus can't match to a handler)."""
    return game.get('steam_appid') or game.get('janus_id') or None


def list_games(search: str = '', ordering: str = 'name') -> List[dict]:
    """Every game on the hub with an approved handler, or the ones whose
    name (or slug / non-Steam id) contains *search*, sorted by *ordering*
    (a key of ORDERINGS). The whole list (no search) is also cached
    (cached_games)."""
    q = {k: v for k, v in (('search', search), ('ordering', ordering)) if v}
    games = _get_json(f'{base_url()}/api/games/?{urllib.parse.urlencode(q)}')
    if not search and isinstance(games, list):
        _save_cache(games)
    return games


# --- cache of the hub's list ------------------------------------------------
# So Janus knows the hub's games at once at startup ("Get handler" cards,
# update badges) and while the hub can't be reached.

def _cache_path() -> Path:
    base = os.environ.get('XDG_CACHE_HOME') or str(Path.home() / '.cache')
    return Path(base) / 'janus' / 'hub-games.json'


def _save_cache(games: List[dict]):
    p = _cache_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix('.tmp')
        tmp.write_text(json.dumps({'url': base_url(), 'time': time.time(), 'games': games}))
        tmp.replace(p)
    except OSError:
        pass


def cached_games() -> List[dict]:
    """The hub's whole list as last fetched from this hub ($JANUS_HUB_URL),
    [] if never."""
    try:
        data = json.loads(_cache_path().read_text())
    except (OSError, ValueError):
        return []
    if data.get('url') != base_url() or not isinstance(data.get('games'), list):
        return []
    return data['games']


def cache_time() -> float:
    try:
        return json.loads(_cache_path().read_text()).get('time', 0.0)
    except (OSError, ValueError):
        return 0.0


def _sorted_offline(games: List[dict], search: str, ordering: str) -> List[dict]:
    """The cached list filtered and sorted like the hub would."""
    s = search.lower()
    games = [g for g in games if not s or s in g.get('name', '').lower()
             or s in g.get('slug', '') or s == str(g.get('steam_appid'))
             or s == (g.get('janus_id') or '')]
    by_name = sorted(games, key=lambda g: g.get('name', '').lower())
    if ordering == 'popular':
        return sorted(by_name, key=lambda g: -installs(g))
    if ordering == 'uploaded':
        return sorted(by_name, key=lambda g: g.get('updated_at', ''), reverse=True)
    if ordering == 'added':
        return sorted(by_name, key=lambda g: -g.get('id', 0))
    return by_name


def installs(game: dict) -> int:
    """How often a game's handler was installed (not counting updates and
    reinstalls); a hub without the count gives its downloads."""
    return game.get('installs', game.get('downloads', 0))


def download_reason(prev: Optional[dict], game_id: int, number: int) -> str:
    """Why we download version *number* (the X-Janus-Download header, so the
    hub counts first installs apart): 'install', 'update' or 'reinstall'
    (the same version again, or going back to an older one)."""
    if not prev or prev.get('id') != game_id:
        return 'install'
    return 'update' if number > prev.get('version', 0) else 'reinstall'


# --- Janus version requirements (Game.minimumJanusVersion) ------------------

def runnable(entry: dict) -> bool:
    """Can this Janus run a hub game's latest version / a version entry
    (its `min_janus_version`)?"""
    return janus_version.satisfies(entry.get('min_janus_version') or '')


def pick_version(detail: dict) -> Optional[dict]:
    """The newest of a game's versions this Janus can run, or None."""
    return next((v for v in detail.get('versions') or [] if runnable(v)), None)


def find_game(key) -> Optional[dict]:
    """The hub's game for a game key (Steam appid or non-Steam id), or None."""
    if scripting.is_steam_key(key):
        q = {'steam_appid': int(key)}
    else:
        q = {'janus_id': key}
    games = [g for g in _get_json(f'{base_url()}/api/games/?{urllib.parse.urlencode(q)}')
             if game_key(g) == (int(key) if scripting.is_steam_key(key) else key)]
    return games[0] if games else None


def game_detail(game_id: int) -> dict:
    """The game with its approved `versions`, newest first."""
    return _get_json(f'{base_url()}/api/games/{int(game_id)}/')


def page_url(game_id: int) -> str:
    """The game's page on the website, where the handler's source is readable."""
    return f'{base_url()}/games/{int(game_id)}/'


# --- catalog: the hub's games with what this machine has -------------------

def catalog(search: str = '', ordering: str = 'popular',
            local: Optional[Dict[object, dict]] = None,
            offline_ok: bool = False) -> List[dict]:
    """The hub's games (list_games) as rows for a browser, each with:
      key       game key (Steam appid or non-Steam id)
      status    '' / 'current' / 'update' (status())
      version   installed hub version (0 = none)
      where     'installed' (Steam game installed), 'library' (in the Steam
                library, not installed), 'store' (not in the library),
                'janus' (non-Steam: Janus sets it up itself)
      handler   a handler for it exists that the hub didn't install (the
                user's own, or the bundled/repo one)
      wanted    what "get all my games" fetches: every 'update', and the
                Steam games in the library (installed or not) with no
                handler at all (non-Steam games are opt-in: you may not
                own them)
      needs     the Janus version its latest handler needs when this one is
                older ('' = runnable; install picks an older version that
                runs, if there is one)
      offline   the hub couldn't be reached: rows from the cached list
                (only with *offline_ok*)
    Games the hub can't key are left out."""
    import steam
    try:
        inst = {g['appid'] for g in steam.get_installed_games()}
    except FileNotFoundError:
        inst = set()
    lib = steam.library_appids() | inst
    local = installed() if local is None else local
    rows = []
    offline = False
    try:
        games = list_games(search, ordering)
    except HubError:
        cached = cached_games() if offline_ok else []
        if not cached:
            raise
        games, offline = _sorted_offline(cached, search, ordering), True
    for g in games:
        key = game_key(g)
        if not key:
            continue
        st = status(g, local)
        if not scripting.is_steam_key(key):
            where = 'janus'
        else:
            where = 'installed' if key in inst else 'library' if key in lib else 'store'
        handler = st == '' and scripting.find_handler(key) is not None
        need = '' if runnable(g) else g.get('min_janus_version', '')
        rows.append({**g, 'key': key, 'status': st, 'needs': need, 'offline': offline,
                     'installs': installs(g),
                     'version': (local.get(key) or {}).get('version', 0) if st else 0,
                     'where': where, 'handler': handler,
                     'wanted': st == 'update' or (st == '' and not handler and where in ('installed', 'library'))})
    return rows


# --- installed state -------------------------------------------------------

def _key(text):
    return int(text) if str(text).isdigit() else str(text)


def installed() -> Dict[object, dict]:
    """{game key: record} of the handlers the hub installed (int keys for
    Steam appids, str for non-Steam ids)."""
    try:
        raw = json.loads(_manifest_path().read_text())
    except (OSError, ValueError):
        return {}
    return {_key(k): v for k, v in raw.items()}


def _save_manifest(m: Dict[object, dict]):
    p = _manifest_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps({str(k): v for k, v in sorted(m.items(), key=lambda kv: str(kv[0]))},
                              indent=2))
    tmp.replace(p)


def _record_paths(rec: dict) -> List[Path]:
    d = _user_dir()
    return [d / f"{rec['slug']}.js", d / rec['slug']]


def _declares(path: Path, key) -> bool:
    if scripting.is_steam_key(key):
        return scripting.handler_appid(path) == int(key)
    return scripting.handler_id(path) == key


def _own_handler(key, rec: Optional[dict]) -> Optional[Path]:
    """A user-dir handler for game *key* the hub did not install, or None."""
    mine = {p.resolve() for p in _record_paths(rec)} if rec else set()
    for p in scripting._handler_files(_user_dir()):
        if not _declares(p, key):
            continue
        top = p.parent if p.name == 'handler.js' else p
        if top.resolve() not in mine:
            return p
    return None


def status(game: dict, local: Optional[Dict[int, dict]] = None) -> str:
    """'' (not installed from the hub), 'current' or 'update'. A newer
    version this Janus can't run isn't an update."""
    local = installed() if local is None else local
    rec = local.get(game_key(game))
    if not rec or rec.get('id') != game['id']:
        return ''
    return 'update' if game['latest_version'] > rec['version'] and runnable(game) \
        else 'current'


# --- install ---------------------------------------------------------------

def _check_key(script: bytes, key, what: str):
    """The script must name the game it was fetched for."""
    text = script.decode('utf-8', errors='replace')
    m = scripting._APPID_DECL.search(text)
    if scripting.is_steam_key(key):
        if not m or int(m.group(1)) != int(key):
            raise HubError(f'{what} does not declare Game.appid = {key};')
        return
    i = scripting._ID_DECL.search(text)
    if m or not i or i.group(1) != key:
        raise HubError(f'{what} does not declare Game.id = "{key}";')


def _unpack_zip(data: bytes, dst: Path, key):
    """Validate the whole zip (API.md §4), then extract it into *dst*."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise HubError('the download is not a valid zip') from e
    infos = zf.infolist()
    if len(infos) > MAX_FILES:
        raise HubError(f'zip has more than {MAX_FILES} files')
    if sum(i.file_size for i in infos) > MAX_UNPACKED:
        raise HubError('zip unpacks to more than 50 MB')
    for i in infos:
        n = i.filename
        if (n.startswith('/') or '\\' in n or '..' in n.split('/')
                or re.match(r'^[A-Za-z]:', n)):
            raise HubError(f'zip entry with an unsafe path: {n!r}')
        if (i.external_attr >> 16) & 0o170000 == 0o120000:
            raise HubError(f'zip entry is a symlink: {n!r}')
    if 'handler.js' not in zf.namelist():
        raise HubError('zip has no handler.js at its root')
    _check_key(zf.read('handler.js'), key, 'handler.js')
    for i in infos:
        target = dst / i.filename
        if i.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(i) as src, open(target, 'wb') as out:
            shutil.copyfileobj(src, out)


def install(game_id: int, version: Optional[dict] = None,
            detail: Optional[dict] = None) -> dict:
    """Download, verify and install a game's handler: *version* (an entry of
    the detail's `versions`) or the latest. Returns the manifest record.

    Raises HubError when the hub is unreachable, the file fails a check, or
    the user has a handler of their own for that game."""
    detail = detail or game_detail(game_id)
    key, slug = game_key(detail), detail.get('slug', '')
    if not key:
        raise HubError(f"{detail.get('name')} has no Steam appid or game id; Janus can't use it")
    if not scripting.is_steam_key(key) and not scripting.GAME_ID_RE.match(str(key)):
        raise HubError(f'bad game id {key!r}')
    if not _SLUG.match(slug):
        raise HubError(f'bad slug {slug!r}')
    if not detail.get('versions'):
        raise HubError(f"{detail['name']} has no approved handler version")
    version = version or pick_version(detail)
    if version is None:
        need = detail['versions'][-1].get('min_janus_version') or '?'
        raise HubError(f"{detail['name']}: every version of its handler needs a newer Janus "
                       f"(at least {need}; this is {janus_version.janus_version()}). Update Janus.")
    if not runnable(version):
        raise HubError(f"{detail['name']} v{version['number']} needs Janus "
                       f"{version['min_janus_version']} or newer "
                       f"(this is {janus_version.janus_version()}).")
    if version.get('format') not in ('js', 'zip'):
        raise HubError(f"unknown handler format {version.get('format')!r}")

    local = installed()
    prev = local.get(key)
    own = _own_handler(key, prev)
    if own:
        raise HubError(f'You have your own handler for {detail["name"]} ({own}). '
                       'Remove it to install the one from the hub.')

    reason = download_reason(prev, detail['id'], version['number'])
    data = _get(version['download_url'], timeout=60, headers={'X-Janus-Download': reason},
                limit=MAX_HANDLER_DOWNLOAD)
    if hashlib.sha256(data).hexdigest() != version['sha256']:
        raise HubError(f"{detail['name']}: checksum mismatch, not installed")

    d = _user_dir()
    d.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.hub-', dir=d))
    try:
        if version['format'] == 'js':
            _check_key(data, key, f'{slug}.js')
            staged = stage / f'{slug}.js'
            staged.write_bytes(data)
        else:
            staged = stage / slug
            staged.mkdir()
            _unpack_zip(data, staged, key)
        # Replace what the hub installed before (either form, maybe another slug).
        for p in _record_paths(prev) if prev else []:
            if p.is_dir():
                shutil.rmtree(p)
            elif p.exists():
                p.unlink()
        for p in (d / f'{slug}.js', d / slug):
            if p.is_dir():
                shutil.rmtree(p)
            elif p.exists():
                p.unlink()
        staged.rename(d / staged.name)
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    rec = {'id': detail['id'], 'slug': slug, 'name': detail['name'],
           'version': version['number'], 'sha256': version['sha256'],
           'format': version['format'], 'cover_url': detail.get('cover_url', '')}
    local[key] = rec
    _save_manifest(local)
    return rec


def uninstall(key) -> bool:
    """Remove a handler the hub installed (game key: appid or non-Steam id).
    False if there was none."""
    local = installed()
    rec = local.pop(_key(key), None)
    if rec is None:
        return False
    for p in _record_paths(rec):
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()
    _save_manifest(local)
    return True

#!/usr/bin/env python3
"""Unit tests for src/host/hub.py (handler hub client) against a fake hub
served from this process, with $HOME pointed at a temp dir.

    python3 tests/test_hub.py
"""

import hashlib
import http.server
import io
import json
import os
import sys
import tempfile
import threading
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'src' / 'host'))

import hub  # noqa: E402
import nonsteam  # noqa: E402
import scripting  # noqa: E402

fails = []


def check(cond, msg):
    print(('  ok: ' if cond else '  FAIL: ') + msg)
    if not cond:
        fails.append(msg)


def make_zip(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buf.getvalue()


FILES = {}      # path -> bytes
GAMES = {}      # id -> detail
QUERIES = []    # query dicts the fake hub's list endpoint got
REASONS = []    # X-Janus-Download of every file download


def add_game(gid, name, slug, appid, versions, janus_id=None):
    """versions: [(fmt, bytes)], oldest first."""
    vs = []
    for n, (fmt, data) in enumerate(versions, 1):
        path = f'/api/games/{gid}/versions/{n}/download/'
        FILES[path] = data
        vs.insert(0, {'number': n, 'uploader': 'u', 'format': fmt,
                      'sha256': hashlib.sha256(data).hexdigest(), 'changelog': '',
                      'download_url': hub.base_url() + path})
    GAMES[gid] = {'id': gid, 'name': name, 'slug': slug, 'steam_appid': appid,
                  'janus_id': janus_id, 'downloads': gid,
                  'cover_url': '', 'latest_version': len(versions),
                  'url': f'{hub.base_url()}/api/games/{gid}/', 'versions': vs}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        path, _, query = self.path.partition('?')
        if path in FILES:
            REASONS.append(self.headers.get('X-Janus-Download'))
            return self._send(FILES[path])
        if path == '/api/games/':
            q = dict(p.split('=', 1) for p in query.split('&') if '=' in p)
            QUERIES.append(q)
            out = [{k: v for k, v in g.items() if k != 'versions'} for g in GAMES.values()
                   if ('steam_appid' not in q or str(g['steam_appid']) == q['steam_appid'])
                   and ('janus_id' not in q or g['janus_id'] == q['janus_id'])
                   and q.get('search', '').lower() in g['name'].lower()]
            if q.get('ordering') == 'popular':
                out.sort(key=lambda g: -g['downloads'])
            return self._send(json.dumps(out).encode())
        gid = path.strip('/').split('/')[-1]
        if path.startswith('/api/games/') and gid.isdigit() and int(gid) in GAMES:
            return self._send(json.dumps(GAMES[int(gid)]).encode())
        self.send_response(404)
        self.end_headers()

    def _send(self, data):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(data)


def main():
    srv = http.server.HTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    os.environ['JANUS_HUB_URL'] = f'http://127.0.0.1:{srv.server_port}'
    home = tempfile.mkdtemp(prefix='janus-hub-test-')
    os.environ['HOME'] = home
    games = Path(home) / '.config/janus/games'

    add_game(1, 'Flat Game', 'flat-game', 100,
             [('js', b'Game.appid = 100;\n// v1\n'), ('js', b'Game.appid = 100;\n// v2\n')])
    add_game(2, 'Zip Game', 'zip-game', 200,
             [('zip', make_zip({'handler.js': 'Game.appid = 200;\n',
                                'payload/mod.txt': 'hi'}))])
    add_game(3, 'Wrong Appid', 'wrong-appid', 300, [('js', b'Game.appid = 999;\n')])
    add_game(4, 'Evil Zip', 'evil-zip', 400,
             [('zip', make_zip({'handler.js': 'Game.appid = 400;\n', '../x': 'pwn'}))])
    add_game(5, 'Bad Sum', 'bad-sum', 500, [('js', b'Game.appid = 500;\n')])
    GAMES[5]['versions'][0]['sha256'] = '0' * 64
    add_game(6, 'Mine', 'mine', 600, [('js', b'Game.appid = 600;\n')])
    add_game(7, 'Blocks', 'blocks', None,
             [('zip', make_zip({'handler.js': 'Game.id = "blocks";\nGame.name = "Blocks";\n',
                                'payload/cover.png': 'png'}))], janus_id='blocks')
    add_game(8, 'Fake Blocks', 'fake-blocks', None,
             [('js', b'Game.appid = 800;\nGame.id = "fake";\n')], janus_id='fake')
    add_game(9, 'Nothing', 'nothing', None, [('js', b'// no key\n')])

    check(len(hub.list_games()) == 9, 'list_games returns everything')
    check([g['id'] for g in hub.list_games(ordering='popular')][:2] == [9, 8]
          and QUERIES[-1].get('ordering') == 'popular', 'list_games passes the ordering')
    check([g['id'] for g in hub.list_games('zip')] == [2, 4], 'search filters by name')
    check(hub.find_game(200)['id'] == 2 and hub.find_game(1) is None, 'find_game by appid')
    check(hub.find_game('blocks')['id'] == 7 and hub.find_game('nope') is None,
          'find_game by non-Steam id')

    # js: install v1, then "update" to v2 replaces it
    REASONS.clear()
    rec = hub.install(1, version=GAMES[1]['versions'][1])
    check(rec['version'] == 1 and (games / 'flat-game.js').exists(), 'js handler installed')
    check(scripting.find_handler(100) == games / 'flat-game.js', 'find_handler sees it')
    check(hub.status(hub.list_games('flat')[0]) == 'update', 'older version -> update')
    hub.install(1)
    check(b'v2' in (games / 'flat-game.js').read_bytes(), 'update replaces the file')
    check(hub.status(hub.list_games('flat')[0]) == 'current', 'latest -> current')
    hub.install(1)
    hub.install(1, version=GAMES[1]['versions'][1])
    check(REASONS == ['install', 'update', 'reinstall', 'reinstall'],
          f'download reasons: install, update, reinstall, older = reinstall {REASONS}')
    hub.install(1)                   # back to the latest for the checks below

    hub.install(2)
    check((games / 'zip-game/handler.js').exists()
          and (games / 'zip-game/payload/mod.txt').read_text() == 'hi', 'zip extracted')
    check(scripting.find_handler(200) == games / 'zip-game/handler.js', 'zip handler found')

    # Non-Steam art: the handler's Game.art first, the hub's (SteamGridDB) for
    # the kinds it doesn't give.
    GAMES[7].update(portrait_url='https://img/p.png', hero_url='https://img/h.png',
                    logo_url='https://img/l.png')

    class Script:
        def __init__(self, art):
            self.art = art

        def get(self, k):
            return 'blocks'

        def has_hook(self, name):
            return self.art is not None

        def invoke(self, name, ctx=None, soft=False):
            return {'ret': self.art}
    got = {}
    real_dl, real_ctx = nonsteam._download_art, nonsteam.base_ctx
    nonsteam._download_art = lambda key, kind, url: got.__setitem__(kind, url) or True
    nonsteam.base_ctx = lambda script: {}
    try:
        nonsteam.fetch_art(Script({'cover': 'https://store/c.jpg', 'hero': 'http://no'}))
        check(got == {'cover': 'https://store/c.jpg', 'hero': 'https://img/h.png',
                      'logo': 'https://img/l.png'}, f'art: handler first, hub fills the rest {got}')
        check(not nonsteam.art_wanted('blocks'), 'art fetched: not wanted again today')
        got.clear()
        nonsteam.fetch_art(Script(None))
        check(got == {'cover': 'https://img/p.png', 'hero': 'https://img/h.png',
                      'logo': 'https://img/l.png'}, 'art: no Game.art, all from the hub')
    finally:
        nonsteam._download_art, nonsteam.base_ctx = real_dl, real_ctx

    # A non-Steam game (Game.id): installed and keyed by its id.
    rec = hub.install(7)
    check((games / 'blocks/handler.js').exists() and 'blocks' in hub.installed(),
          'non-Steam handler installed, manifest keyed by id')
    check(scripting.find_handler('blocks') == games / 'blocks/handler.js',
          'find_handler sees the non-Steam handler')
    check(hub.status(hub.find_game('blocks')) == 'current', 'non-Steam status')
    rows = {r['key']: r for r in hub.catalog()}
    check(rows['blocks']['where'] == 'janus' and rows['blocks']['status'] == 'current'
          and not rows['blocks']['wanted'], 'catalog: non-Steam row')
    check(9 not in {r['id'] for r in rows.values()}, 'catalog skips games without a key')
    check(rows[100]['status'] == 'current' and rows[100]['where'] == 'store'
          and not rows[100]['handler'], 'catalog: Steam row')

    for gid, what in ((3, 'wrong appid'), (4, 'zip path traversal'), (5, 'checksum'),
                      (8, 'Game.id with a Game.appid'), (9, 'no key')):
        try:
            hub.install(gid)
            check(False, f'{what} rejected')
        except hub.HubError as e:
            check(not (games / GAMES[gid]['slug']).exists()
                  and not (games / f"{GAMES[gid]['slug']}.js").exists(), f'{what} rejected: {e}')
    check(not any(p.name.startswith('.hub-') for p in games.iterdir()), 'no staging left over')
    check((Path(home) / 'x').exists() is False and not (games / 'x').exists(), 'nothing escaped')

    (games / 'my-own.js').write_text('Game.appid = 600;\n')
    try:
        hub.install(6)
        check(False, "user's own handler is not overwritten")
    except hub.HubError:
        check((games / 'my-own.js').exists() and not (games / 'mine.js').exists(),
              "user's own handler is not overwritten")

    check(hub.uninstall(200) and not (games / 'zip-game').exists(), 'uninstall removes the dir')
    check(hub.uninstall('blocks') and not (games / 'blocks').exists(), 'uninstall by id')
    check(set(hub.installed()) == {100}, 'manifest tracks what is installed')

    # Game.minimumJanusVersion: the newest version this Janus can run.
    add_game(10, 'Future Game', 'future-game', 1000,
             [('js', b'Game.appid = 1000;\n// v1\n'), ('js', b'Game.appid = 1000;\n// v2\n')])
    GAMES[10]['versions'][0]['min_janus_version'] = '99'      # v2
    GAMES[10]['min_janus_version'] = '99'
    rec = hub.install(10)
    check(rec['version'] == 1, 'install picks the newest version this Janus runs (v1, v2 needs 99)')
    g10 = hub.find_game(1000)
    check(hub.status(g10) == 'current', "a version this Janus can't run isn't an update")
    row = {r['key']: r for r in hub.catalog()}[1000]
    check(row['needs'] == '99', 'catalog: needs the Janus version of the latest')
    GAMES[10]['versions'][1]['min_janus_version'] = '98'
    try:
        hub.install(10, detail=GAMES[10])
        check(False, 'no runnable version: refused')
    except hub.HubError as e:
        check('newer Janus' in str(e), f'no runnable version: refused ({e})')
    hub.uninstall(1000)

    # The hub's list is cached; offline, the catalog falls back to it.
    hub.list_games()
    check(len(hub.cached_games()) == len(GAMES), 'whole list cached')
    srv.shutdown()
    srv.server_close()
    rows = hub.catalog('', 'popular', offline_ok=True)
    check(rows and all(r['offline'] for r in rows)
          and [r['downloads'] for r in rows] == sorted((r['downloads'] for r in rows), reverse=True),
          'offline: catalog from the cache, sorted by downloads')
    check([r['name'] for r in hub.catalog('zip', offline_ok=True)] == ['Zip Game', 'Evil Zip']
          or {r['name'] for r in hub.catalog('zip', offline_ok=True)} == {'Zip Game', 'Evil Zip'},
          'offline search filters the cache')
    os.environ['JANUS_HUB_URL'] = 'http://127.0.0.1:1'
    check(hub.cached_games() == [], 'cache is per hub URL')
    try:
        hub.list_games()
        check(False, 'unreachable hub raises HubError')
    except hub.HubError:
        check(True, 'unreachable hub raises HubError')

    print(f"\n{'FAILED: ' + str(len(fails)) if fails else 'all passed'}")
    sys.exit(1 if fails else 0)


if __name__ == '__main__':
    main()

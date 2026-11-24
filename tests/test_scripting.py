#!/usr/bin/env python3
"""Unit tests for src/host/scripting.py (handler script engine).

Standalone: python3 tests/test_scripting.py  (no pytest needed; also
pytest-compatible). Uses a temp dir for all file operations.
"""

import json
import os
import sys
import tempfile
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src' / 'host'))
import scripting  # noqa: E402

PASS = 0


def check(cond, msg):
    global PASS
    assert cond, msg
    PASS += 1


def make_script(tmp, source, appid=999, options=None):
    p = tmp / f'{appid}.js'
    p.write_text(source)
    s = scripting.HandlerScript(p, appid, option_values=options)
    s.set_base_dir(tmp)
    return s


def test_sheet_and_hooks(tmp):
    s = make_script(tmp, """
        Game.name = "T";
        Game.goldberg = true;
        Game.staggerSeconds = 1.5;
        Game.play = function (ctx) {};
    """)
    check(s.get('name') == 'T', 'sheet string')
    check(s.get('goldberg') is True, 'sheet bool')
    check(s.get('staggerSeconds') == 1.5, 'sheet float')
    check(s.get('missing', 'dflt') == 'dflt', 'sheet default')
    check(s.has_hook('play') and not s.has_hook('onStop'), 'hooks detected')


def test_minimum_janus_version(tmp):
    import version
    s = make_script(tmp, 'Game.minimumJanusVersion = "0.1";\nGame.name = "T";')
    check(s.get('name') == 'T', 'handler for an older/equal Janus loads')
    try:
        make_script(tmp, 'Game.minimumJanusVersion = "999.0";\nGame.name = "T";')
        check(False, 'handler for a newer Janus refused')
    except scripting.HandlerError as e:
        check('needs Janus 999.0' in str(e), 'handler for a newer Janus refused')
    check(version.parse('0.10.0') > version.parse('0.9') and version.parse('0.2') == version.parse('0.2.0'),
          'versions compare numerically, trailing zeros ignored')
    check(version.satisfies('0.2', '0.2.0+git20261122.abc'), 'build suffix ignored')


def test_launch_message(tmp):
    s = make_script(tmp, 'Game.launchMessage = "  Press A, not the keyboard.\\n";')
    check(scripting.launch_message(s) == 'Press A, not the keyboard.', 'launch message trimmed')
    check(scripting.launch_message(make_script(tmp, 'Game.name = "T";')) == '',
          'no launch message')
    check(scripting.launch_message(None) == '', 'no script, no message')


def test_options(tmp):
    src = """
        Janus.addOption("mode", {type: "choice", choices: ["a", "b"], default: "a"});
        Janus.addOption("count", {type: "int", default: 2});
        Janus.addOption("flag", {type: "bool", default: false});
        Game.play = function (ctx) { return ctx.options; };
    """
    s = make_script(tmp, src, options={'count': '7', 'flag': 'yes'})
    check(s.options == {'mode': 'a', 'count': 7, 'flag': True}, f'coercion: {s.options}')
    r = s.invoke('play', ctx={'options': s.options})
    check(r['ret'] == {'mode': 'a', 'count': 7, 'flag': True}, 'options reach ctx')
    try:
        make_script(tmp, src, options={'mode': 'zzz'})
        check(False, 'bad choice should raise')
    except scripting.HandlerError:
        check(True, 'bad choice raises')


def test_env_precedence(tmp):
    s = make_script(tmp, """
        Game.extraEnv = {SCRIPT_VAR: "s"};
    """)
    env = {}
    s.apply_env(env, user_env={})
    check(env == {'SCRIPT_VAR': 's'}, f'apply_env: {env}')
    env = {'SCRIPT_VAR': 'user'}
    s.apply_env(env, user_env={'SCRIPT_VAR': 'user'})
    check(env['SCRIPT_VAR'] == 'user', 'user env beats script extraEnv')


def test_invoke_mutations(tmp):
    s = make_script(tmp, """
        Game.buildEnvironment = function (ctx, env) { env.ADDED = "1"; delete env.KILL; };
        Game.play = function (ctx) { ctx.gameArgs.push("-x"); ctx.env.E = "v"; };
    """)
    r = s.invoke('buildEnvironment', ctx={}, env={'KEEP': 'k', 'KILL': 'x'})
    check(r['env'] == {'KEEP': 'k', 'ADDED': '1'}, f'env mutation: {r["env"]}')
    r = s.invoke('play', ctx={'gameArgs': [], 'env': {}})
    check(r['gameArgs'] == ['-x'] and r['ctxEnv'] == {'E': 'v'}, 'play mutations')
    check(s.invoke('nonexistent') is None, 'missing hook -> None')


def test_errors(tmp):
    try:
        make_script(tmp, "this is not js !!!")
        check(False, 'syntax error should raise')
    except scripting.HandlerError:
        check(True, 'syntax error raises HandlerError')
    s = make_script(tmp, "Game.play = function () { throw new Error('boom'); };")
    try:
        s.invoke('play', ctx={})
        check(False, 'hook error should raise')
    except scripting.HandlerError as e:
        check('boom' in str(e), 'hook error message surfaces')
    check(s.invoke('play', ctx={}, soft=True) is None, 'soft error returns None')


def test_file_api(tmp):
    s = make_script(tmp, """
        Game.play = function (ctx) {
            Janus.writeFile("sub/a.txt", "one\\ntwo joystick 0\\nthree");
            var n = Janus.findLine("sub/a.txt", "joystick");
            Janus.replaceLine("sub/a.txt", "joystick", 'joystick "1"');
            Janus.iniSet("cfg.ini", "video", "width", "960");
            Janus.iniSet("cfg.ini", "video", "height", "540");
            Janus.iniSet("cfg.ini", "", "global", "yes");
            Janus.jsonSet("s.json", "graphics.mode", 3);
            return {line: n, ini: Janus.iniGet("cfg.ini", "video", "width"),
                    exists: Janus.exists("sub/a.txt"),
                    missing: Janus.exists("nope")};
        };
    """)
    r = s.invoke('play', ctx={})['ret']
    check(r == {'line': 2, 'ini': '960', 'exists': True, 'missing': False}, f'file api: {r}')
    check('joystick "1"' in (tmp / 'sub/a.txt').read_text(), 'replaceLine wrote')
    ini = (tmp / 'cfg.ini').read_text()
    check('global=yes' in ini.splitlines()[0], 'sectionless ini key first')
    check('[video]' in ini and 'width=960' in ini and 'height=540' in ini, f'ini sections: {ini!r}')
    check(json.loads((tmp / 's.json').read_text()) == {'graphics': {'mode': 3}}, 'jsonSet')


def test_ini_preserves_lines(tmp):
    cfg = tmp / 'game.ini'
    cfg.write_text('; comment\n[a]\nx=1\n\n[b]\ny=2\n')
    s = make_script(tmp, """
        Game.play = function () { Janus.iniSet("game.ini", "a", "x", "9"); };
    """)
    s.invoke('play', ctx={})
    check(cfg.read_text() == '; comment\n[a]\nx=9\n\n[b]\ny=2\n', f'line-preserving: {cfg.read_text()!r}')


def test_remove_guard(tmp):
    # /tmp is a managed root by design, so the guard test target must live
    # elsewhere — use a throwaway dir under ~/.cache.
    outside = Path.home() / '.cache' / 'janus_test_remove_guard'
    outside.mkdir(parents=True, exist_ok=True)
    victim = outside / 'v.txt'
    victim.write_text('x')
    try:
        s = make_script(tmp, f"""
            Game.play = function () {{ Janus.remove("{victim}"); }};
            Game.buildCommand = function () {{ Janus.remove("{victim}", {{force: true}}); }};
        """)
        try:
            s.invoke('play', ctx={})
            check(False, 'unmanaged remove should raise')
        except scripting.HandlerError as e:
            check('refusing to remove' in str(e), 'remove guard message')
        check(victim.exists(), 'victim untouched')
        s.invoke('buildCommand', ctx={})
        check(not victim.exists(), 'force remove works')
    finally:
        shutil.rmtree(outside, ignore_errors=True)


def test_backup_restore(tmp):
    f = tmp / 'save.cfg'
    f.write_text('original')
    s = make_script(tmp, """
        Game.play = function () {
            Janus.backup("save.cfg");
            Janus.writeFile("save.cfg", "modified");
            Janus.backup("new.cfg");
            Janus.writeFile("new.cfg", "created");
        };
    """)
    s.invoke('play', ctx={})
    check(f.read_text() == 'modified', 'file modified during session')
    s.finish()
    check(f.read_text() == 'original', 'backup restored on finish')
    check(not (tmp / 'new.cfg').exists(), 'created-then-backed-up file removed')
    s.finish()  # idempotent


def test_payload(tmp):
    payload = tmp / '999.payload'
    (payload / 'mods').mkdir(parents=True)
    (payload / 'mods' / 'm.txt').write_text('mod')
    s = make_script(tmp, """
        Game.play = function (ctx) { Janus.deployPayload(ctx.gameDir); };
    """)
    dst = tmp / 'gamedir'
    dst.mkdir()
    s.invoke('play', ctx={'gameDir': str(dst)})
    check((dst / 'mods' / 'm.txt').read_text() == 'mod', 'payload deployed')


def test_run_spawn(tmp):
    s = make_script(tmp, """
        Game.play = function () {
            var r = Janus.run(["/bin/echo", "out"]);
            var pid = Janus.spawn(["/bin/sleep", "30"]);
            return {code: r.code, out: r.stdout.trim(), pid: pid};
        };
        Game.buildCommand = function () { Janus.run("echo nope"); };
    """)
    r = s.invoke('play', ctx={})['ret']
    check(r['code'] == 0 and r['out'] == 'out' and r['pid'] > 0, f'run/spawn: {r}')
    check(os.path.exists(f"/proc/{r['pid']}"), 'spawned alive')
    s.finish()
    import time
    time.sleep(0.2)
    alive = os.path.exists(f"/proc/{r['pid']}") and \
        open(f"/proc/{r['pid']}/stat").read().split(') ')[1][0] != 'Z' \
        if os.path.exists(f"/proc/{r['pid']}") else False
    check(not alive, 'spawned killed on finish')
    try:
        s2 = make_script(tmp, 'Game.play = function () { Janus.run("echo nope"); };')
        s2.invoke('play', ctx={})
        check(False, 'string argv should raise')
    except scripting.HandlerError:
        check(True, 'string argv rejected')


def test_binary_patch(tmp):
    f = tmp / 'bin.dat'
    f.write_bytes(b'\x00AAAA\x01AAAA\x02')
    s = make_script(tmp, """
        Game.play = function () { return Janus.binaryPatch("bin.dat", "41414141", "42424242"); };
    """)
    check(s.invoke('play', ctx={})['ret'] == 2, 'patch count')
    check(f.read_bytes() == b'\x00BBBB\x01BBBB\x02', 'bytes patched')


def test_effective(tmp):
    s = make_script(tmp, "Game.isolation = 'full';")
    check(scripting.effective(s, 'isolation', 'none', 'none', 'none') == 'full',
          'default falls through to script')
    check(scripting.effective(s, 'isolation', 'secondary', 'none', 'none') == 'secondary',
          'explicit CLI wins')
    check(scripting.effective(s, 'goldberg', False, False, False) is False,
          'unset field -> builtin')
    check(scripting.effective(None, 'isolation', 'none', 'none', 'x') == 'x',
          'no script -> builtin')


def test_load_time_gating(tmp):
    try:
        make_script(tmp, 'Janus.writeFile("x.txt", "boom");')
        check(False, 'load-time write should raise')
    except scripting.HandlerError as e:
        check('load time' in str(e), 'load-time gating message')
    check(not (tmp / 'x.txt').exists(), 'no file written at load')
    # read-only probes stay allowed at load
    s = make_script(tmp, 'Game.name = Janus.exists("nope") ? "y" : "n";')
    check(s.get('name') == 'n', 'read-only allowed at load')


def test_atexit_cleanup_restores(tmp):
    f = tmp / 'cfg.txt'
    f.write_text('orig')
    s = make_script(tmp, """
        Game.play = function () {
            Janus.backup("cfg.txt");
            Janus.writeFile("cfg.txt", "changed");
        };
    """)
    s.invoke('play', ctx={})
    check(f.read_text() == 'changed', 'edited')
    # simulate the interpreter-exit path without finish()
    s._cleanup_resources()
    check(f.read_text() == 'orig', 'atexit-style cleanup restored')
    s.finish()  # still idempotent afterwards


def test_user_messages(tmp):
    s = make_script(tmp, """
        Janus.warn("at load");
        Game.play = function (ctx) { Janus.warn("missing", 1); Janus.warn("missing", 1); };
        Game.onStop = function (ctx) { Janus.error("fatal"); return "unreached"; };
    """)
    got = []
    s.set_notify(lambda level, text, done: got.append((level, text)))
    check(got == [('warning', 'at load')], f'load-time warning replayed: {got}')
    s.invoke('play', ctx={})
    check(got[1:] == [('warning', 'missing 1')], f'warn deduplicated: {got}')
    try:
        s.invoke('onStop', ctx={})
        check(False, 'Janus.error should abort the hook')
    except scripting.HandlerError as e:
        check('fatal' in str(e), f'error message carried: {e}')
    check(got[-1] == ('error', 'fatal'), 'error notified')
    check(s.invoke('onStop', ctx={}, soft=True) is None, 'soft hook swallows error')


def test_user_messages_block(tmp):
    import threading
    import time
    s = make_script(tmp, """
        Game.play = function (ctx) { Janus.warn("w"); return "after"; };
        Game.onAllStarted = function (ctx) { return "ui"; };
    """)
    pending = []
    s.set_notify(lambda level, text, done: pending.append(done),
                 ui_thread=threading.get_ident())
    out = {}
    t = threading.Thread(target=lambda: out.update(r=s.invoke('play', ctx={})))
    t.start()
    while not pending:
        time.sleep(0.01)
    time.sleep(0.1)
    check(t.is_alive() and 'r' not in out, 'hook blocks until dialog closed')
    # A UI-thread hook must skip, not deadlock, while the dialog is up.
    check(s.invoke('onAllStarted', ctx={}, soft=True) is None, 'ui hook skipped')
    pending[0]()
    t.join(2)
    check(out['r']['ret'] == 'after', 'hook resumes after done()')
    check(s.invoke('onAllStarted', ctx={})['ret'] == 'ui', 'ui hook runs again')

    # Stop while blocked: the hook aborts instead of continuing the launch.
    s2 = make_script(tmp, 'Game.play = function (c) { Janus.warn("x"); return 1; };', appid=998)
    pending2 = []
    s2.set_notify(lambda level, text, done: pending2.append(done),
                  ui_thread=threading.get_ident())
    t = threading.Thread(target=lambda: out.update(
        e=_raises(lambda: s2.invoke('play', ctx={}))))
    t.start()
    while not pending2:
        time.sleep(0.01)
    s2.release_waiters()
    t.join(2)
    check(isinstance(out['e'], scripting.HandlerError), f'stop aborts hook: {out}')
    s2.finish()


def test_unity_ui_scale(tmp):
    """Game.unityUiScale adds the uiScale choice option (1.0..2.5); BepInEx is
    needed only above 1.0 or with bepinexPlugins, and deploys next to the exe
    inside the mirror (a symlinked exe dir is made real, the install is never
    written)."""
    import bepinex
    src = 'Game.unityUiScale = true;'
    s = make_script(tmp, src)
    spec = dict(s.option_specs).get('uiScale')
    check(spec and spec['choices'][0] == '1.0' and spec['choices'][-1] == '2.5'
          and len(spec['choices']) == 16, f'uiScale choices: {spec}')
    check(s.options['uiScale'] == '1.0' and not bepinex.needed(s), 'off at 1.0')
    s = make_script(tmp, src, options={'uiScale': '1.3'})
    check(bepinex.needed(s) and bepinex.plan(s)[0] == 1.3, 'on at 1.3')
    check(not bepinex.needed(make_script(tmp, 'Game.name = "x";')), 'no flag, no BepInEx')

    # Fake Unity install + a mirror whose exe dir is a symlink into it.
    inst = tmp / 'install' / 'Game'
    (inst / 'Game_Data' / 'Managed').mkdir(parents=True)
    (inst / 'Game.exe').write_text('')
    mirror = tmp / 'mirror'
    mirror.mkdir()
    (mirror / 'Game').symlink_to(inst)
    env = {'WINEDLLOVERRIDES': 'dxgi=n'}
    ok = bepinex.deploy(s, mirror, mirror / 'Game' / 'Game.exe', env, True)
    check(ok and not (mirror / 'Game').is_symlink(), 'exe dir materialized')
    check((mirror / 'Game' / 'winhttp.dll').is_file()
          and (mirror / 'Game' / 'BepInEx' / 'plugins' / 'JanusUiScale.dll').is_file(),
          'BepInEx + plugin deployed')
    check(not (inst / 'winhttp.dll').exists() and not (inst / 'BepInEx').exists(),
          'real install untouched')
    check(env['WINEDLLOVERRIDES'] == 'dxgi=n;winhttp=n,b' and env['JANUS_UI_SCALE'] == '1.3',
          f'env: {env}')

    # IL2CPP: warned and skipped.
    (inst / 'GameAssembly.dll').write_text('')
    shutil.rmtree(mirror)
    mirror.mkdir()
    (mirror / 'Game').symlink_to(inst)
    check(not bepinex.deploy(s, mirror, mirror / 'Game' / 'Game.exe', {}, True)
          and any('IL2CPP' in t for _, t in s.messages), 'IL2CPP skipped with a warning')

    # bepinexPlugins: payload-relative, deployed without the UI scale plugin.
    p = tmp / 'mods.js'
    p.write_text('Game.bepinexPlugins = ["Fix.dll"];')
    (tmp / 'mods.payload').mkdir()
    (tmp / 'mods.payload' / 'Fix.dll').write_text('')
    s2 = scripting.HandlerScript(p, 999)
    (inst / 'GameAssembly.dll').unlink()
    shutil.rmtree(mirror)
    mirror.mkdir()
    (mirror / 'Game').symlink_to(inst)
    env = {}
    check(bepinex.needed(s2) and bepinex.deploy(s2, mirror, mirror / 'Game' / 'Game.exe', env, True),
          'plugin-only deploy')
    plugins = sorted(x.name for x in (mirror / 'Game' / 'BepInEx' / 'plugins').iterdir())
    check(plugins == ['Fix.dll'] and 'JANUS_UI_SCALE' not in env, f'plugins: {plugins}')


def _raises(fn):
    try:
        fn()
    except Exception as e:
        return e
    return None


def test_find_handler(tmp):
    """Discovery matches on the declared Game.appid, not the file name; repo
    handlers/ beats the user dir (with a conflict warning); legacy <appid>.js
    still matches; repo handlers all resolve."""
    user = tmp / 'user'
    repo = tmp / 'repo'
    user.mkdir()
    repo.mkdir()
    (repo / 'some-game.js').write_text('Game.appid = 4242;\nGame.name = "X";\n')
    (repo / 'dir-game').mkdir()
    (repo / 'dir-game' / 'handler.js').write_text('Game.appid = 5353;\n')
    (repo / '6464.js').write_text('Game.name = "legacy";\n')
    (repo / '7575.js').write_text('Game.appid = 1;\n')   # declaration wins
    orig = scripting._handler_dirs
    scripting._handler_dirs = lambda: [repo, user]
    try:
        check(scripting.find_handler(4242) == repo / 'some-game.js', 'by Game.appid')
        check(scripting.find_handler(5353) == repo / 'dir-game' / 'handler.js',
              'directory form')
        check(scripting.find_handler(6464) == repo / '6464.js', 'legacy name')
        check(scripting.find_handler(7575) is None, 'declared appid beats name')
        check(scripting.find_handler(9999) is None, 'no handler')
        check(scripting.handler_conflict(4242) is None, 'no conflict with one handler')
        (user / 'mine.js').write_text('Game.appid = 4242;\n')
        (user / 'only-mine.js').write_text('Game.appid = 8686;\n')
        check(scripting.find_handler(4242) == repo / 'some-game.js', 'repo dir first')
        check(scripting.find_handler(8686) == user / 'only-mine.js', 'user dir as fallback')
        msg = scripting.handler_conflict(4242) or ''
        check('some-game.js' in msg.split('ignoring')[0] and 'mine.js' in msg.split('ignoring')[1],
              f'conflict names the used and the ignored handler: {msg}')
    finally:
        scripting._handler_dirs = orig
    # Installed copy (VERSION file): no bundled handlers, only the user's (hub).
    orig_installed = scripting.INSTALLED
    scripting.INSTALLED = True
    try:
        check(scripting._handler_dirs() == [scripting.user_handler_dir()],
              'installed: only the user dir')
    finally:
        scripting.INSTALLED = orig_installed
        scripting._handler_dirs = orig
    # Shipped handlers, on their own (independent of what's installed in the
    # real ~/.config/janus/games).
    shipped = scripting._REPO_ROOT / 'handlers'
    scripting._handler_dirs = lambda: [shipped]
    try:
        for p in sorted(shipped.glob('*.js')):
            aid = scripting.handler_appid(p)
            check(aid is not None, f'{p.name} declares Game.appid')
            check(scripting.find_handler(aid) == p, f'{p.name} found for {aid}')
    finally:
        scripting._handler_dirs = orig


def test_non_steam_discovery(tmp):
    d = tmp / 'handlers'
    (d / 'minecraft').mkdir(parents=True)
    (d / 'minecraft' / 'handler.js').write_text('Game.id = "minecraft";\nGame.name = "Minecraft";\n')
    (d / 'steamy.js').write_text('Game.appid = 550;\nGame.id = "not-used";\n')
    orig = scripting._handler_dirs
    scripting._handler_dirs = lambda: [d]
    try:
        check(scripting.parse_game_key('550') == 550, 'digits -> appid')
        check(scripting.parse_game_key('minecraft') == 'minecraft', 'name -> id')
        try:
            scripting.parse_game_key('Bad Id')
            check(False, 'invalid id rejected')
        except ValueError:
            check(True, 'invalid id rejected')
        check(scripting.is_steam_key(550) and not scripting.is_steam_key('minecraft'),
              'is_steam_key')
        check(scripting.find_handler('minecraft') == d / 'minecraft' / 'handler.js',
              'found by Game.id')
        check(scripting.find_handler(550) == d / 'steamy.js', 'Steam handler by appid')
        check(scripting.handler_id(d / 'steamy.js') is None, 'an appid makes it a Steam game')
        check(scripting.non_steam_handlers() == {'minecraft': d / 'minecraft' / 'handler.js'},
              'non-Steam handlers listed')
        check(scripting.handler_name(d / 'minecraft' / 'handler.js') == 'Minecraft', 'name read')
        check('"minecraft"' in scripting.no_handler_message('minecraft'), 'message names the id')
    finally:
        scripting._handler_dirs = orig


def test_actions_and_setup(tmp):
    import nonsteam
    orig_root = nonsteam.DATA_ROOT
    nonsteam.DATA_ROOT = tmp / 'games'
    try:
        s = make_script(tmp, """
            Game.id = "thing";
            Game.name = "Thing";
            Janus.addAction("fix", {label: "Fix it", menu: false,
                                    run: function (ctx) {
                                        Janus.progress("fixing");
                                        Janus.writeFile(ctx.gameDir + "/fixed", "1");
                                        return "Fixed.";
                                    }});
            Janus.addAction("tidy", {label: "Tidy", run: function (ctx) {}});
            Game.setupCheck = function (ctx) {
                if (!Janus.exists(ctx.gameDir + "/fixed"))
                    return {ready: false, message: "Broken.", action: "fix"};
                return {ready: true, status: "v1"};
            };
            Game.owner = function (ctx) { return {name: "Alex", label: "Some account"}; };
        """, appid='thing')
        check(s.non_steam, 'Game.id without appid = non-Steam')
        st = nonsteam.setup_status(s)
        check(not st['ready'] and st['action'] == 'fix' and st['actionLabel'] == 'Fix it'
              and st['message'] == 'Broken.', 'not ready, with its fix')
        check([k for k, _ in nonsteam.menu_actions(s)] == ['tidy'], 'menu: false hides an action')
        seen = []
        s.progress = seen.append
        check(nonsteam.run_action(s, 'fix') == 'Fixed.', 'action result')
        check(seen == ['fixing'], 'progress reported')
        check(nonsteam.run_action(s, 'tidy') == '', 'no result -> empty')
        st = nonsteam.setup_status(s)
        check(st['ready'] and st['status'] == 'v1', 'ready after the fix')
        check(nonsteam.owner(s) == {'name': 'Alex', 'label': 'Some account'}, 'owner')
        check(nonsteam.with_owner({'name': 'x', 'owner': True}, nonsteam.owner(s))['name'] == 'Alex',
              'owner name replaces the Steam account')
        check(nonsteam.with_owner({'name': 'x', 'owner': False}, nonsteam.owner(s))['name'] == 'x',
              'other profiles keep theirs')
        try:
            make_script(tmp, 'Janus.addAction("x", {label: "X"});')
            check(False, 'addAction needs run')
        except scripting.HandlerError:
            check(True, 'addAction needs run')
        st = nonsteam.setup_status(make_script(tmp, 'Game.id = "y";'))
        check(st['ready'], 'no setupCheck = ready')
    finally:
        nonsteam.DATA_ROOT = orig_root


def test_wrapper_script(tmp):
    import nonsteam
    base = {'HOME': '/h', 'GONE': '1', 'BASH_FUNC_x%%': '() { :; }'}
    env = {'HOME': '/h', 'LD_PRELOAD': "/a b/lib.so", 'NEW': "it's"}
    text = nonsteam._wrapper_script(base, env, ['/usr/games/gamescope', '-w', '1', '--'])
    check('unset GONE' in text, 'removed vars unset')
    check("export LD_PRELOAD='/a b/lib.so'" in text, 'values quoted')
    check('HOME=' not in text, 'unchanged vars left alone')
    check('BASH_FUNC' not in text, 'non-shell names skipped')
    check(text.rstrip().endswith('exec /usr/games/gamescope -w 1 -- "$@"'), 'runs the args in gamescope')
    check(nonsteam._wrapper_script(base, base, []).rstrip().endswith('exec "$@"'), 'no gamescope')


def test_extract_refuses_escapes(tmp):
    import io
    import tarfile
    s = make_script(tmp, 'Game.id = "z";')
    good = tmp / 'good.tar.gz'
    with tarfile.open(good, 'w:gz') as t:
        data = b'hi'
        info = tarfile.TarInfo('dir/file.txt')
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo('dir/link')
        link.type = tarfile.SYMTYPE
        link.linkname = 'file.txt'
        t.addfile(link)
    s._api_extract(str(good), str(tmp / 'out'))
    check((tmp / 'out' / 'dir' / 'file.txt').read_text() == 'hi', 'extracted')
    check((tmp / 'out' / 'dir' / 'link').is_symlink(), 'inner symlink kept')
    for name, linkname in (('../evil', None), ('dir/link', '../../etc')):
        bad = tmp / 'bad.tar.gz'
        with tarfile.open(bad, 'w:gz') as t:
            info = tarfile.TarInfo(name)
            if linkname:
                info.type = tarfile.SYMTYPE
                info.linkname = linkname
            t.addfile(info, io.BytesIO(b''))
        try:
            s._api_extract(str(bad), str(tmp / 'out2'))
            check(False, f'{name} refused')
        except scripting.HandlerError:
            check(True, f'{name} refused')


def test_download_flaky(tmp):
    """Janus.download survives dropped connections (with and without Range
    support) and corrupted bodies, and gives up cleanly on a 404."""
    import hashlib
    import http.server
    import threading
    import time
    body = os.urandom(300_000)
    sha = hashlib.sha512(body).hexdigest()
    state = {'mode': '', 'n': 0, 'ranges': []}

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *a):
            pass

        def do_GET(self):
            state['n'] += 1
            mode, n = state['mode'], state['n']
            if mode == '404':
                self.send_error(404)
                return
            start = 0
            rng = self.headers.get('Range')
            state['ranges'].append(rng)
            if rng and mode != 'norange':
                start = int(rng.split('=')[1].rstrip('-'))
                self.send_response(206)
                self.send_header('Content-Range', f'bytes {start}-{len(body) - 1}/{len(body)}')
            else:
                self.send_response(200)
            data = body[start:]
            if mode == 'corrupt' and n == 1:
                data = bytes(len(data))
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            if mode in ('drop', 'norange') and n <= 2:
                self.wfile.write(data[:len(data) // 3])   # then hang up
                self.wfile.flush()
                self.close_connection = True
                return
            self.wfile.write(data)

    srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{srv.server_address[1]}/f'
    real_sleep, time.sleep = time.sleep, lambda s: None
    try:
        s = make_script(tmp, 'Game.id = "dl";')
        for mode in ('drop', 'norange', 'corrupt'):
            state.update(mode=mode, n=0, ranges=[])
            out = tmp / f'{mode}.bin'
            s._api_download(url, str(out), {'sha512': sha})
            check(out.read_bytes() == body, f'{mode}: complete file')
            check(not (tmp / f'{mode}.bin.part').exists(), f'{mode}: no .part left')
        state.update(mode='drop', n=0, ranges=[])
        s._api_download(url, str(tmp / 'r.bin'), {'sha512': sha})
        check(state['ranges'][0] is None and state['ranges'][1] == 'bytes=100000-',
              'resumed where the connection dropped')
        state.update(mode='404', n=0)
        try:
            s._api_download(url, str(tmp / 'x.bin'), {})
            check(False, '404 raises')
        except scripting.HandlerError as e:
            check('HTTP 404' in str(e) and state['n'] == 1, '404: no retries, says why')
        state.update(mode='', n=0)
        try:
            s._api_download(url, str(tmp / 'y.bin'), {'sha512': '0' * 128, 'retries': 2})
            check(False, 'wrong hash raises')
        except scripting.HandlerError as e:
            check('checksum' in str(e) and state['n'] == 3 and not (tmp / 'y.bin').exists(),
                  'wrong hash: retried, then a clear error, nothing kept')
    finally:
        time.sleep = real_sleep
        srv.shutdown()


def main():
    tests =[(n, f) for n, f in sorted(globals().items()) if n.startswith('test_')]
    for name, fn in tests:
        tmp = Path(tempfile.mkdtemp(prefix='janus_scripting_test_'))
        try:
            if fn.__code__.co_argcount:
                fn(tmp)
            else:
                fn()
            print(f'  ok: {name}')
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    print(f'ALL PASS ({PASS} assertions, {len(tests)} tests)')


if __name__ == '__main__':
    main()

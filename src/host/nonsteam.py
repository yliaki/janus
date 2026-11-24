"""Non-Steam games: handlers that declare `Game.id = "<id>";` instead of a
Steam appid (Minecraft through Prism Launcher, ...).

Nothing comes from Steam, so the handler supplies what Steam would:
- setup: `Game.setupCheck(ctx)` says whether the game is ready to launch and,
  if not, which `Janus.addAction` button fixes it (download a launcher, log
  in, install). The GUIs run the check when the game is selected and before
  a launch, and offer the fix; actions with `menu` (the default) are also
  buttons in the game's options (maintenance: "Open Prism Launcher").
- the command: `Game.buildCommand(ctx)` (required). With
  `Game.launcherStartsGame` the command is a launcher that starts the game
  itself through `ctx.wrapper`, a script that runs its arguments in this
  instance's gamescope tile with the hook; the launcher stays outside.
- the owner: `Game.owner(ctx)` -> {name, label} replaces the Steam account
  as the owner profile's name and label ("Microsoft account").
- artwork: `Game.art(ctx)` -> {cover, hero, logo} image URLs, downloaded in
  the background into <game dir>/art/ (Big Picture's cover grid and banner).

Janus keeps the rest: profiles (ctx.profile, ctx.saveRoot), the hook,
gamescope tiles and KWin placement. There is no isolation (no Steam
runtime / Proton): handlers keep per-player data under ctx.saveRoot.
"""

import os
import re
import shlex
import signal
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional

import profiles
import scripting
from steam import (apply_hook_env, apply_janus_input_env, apply_perf_env, wrap_in_gamescope,
                   wrap_input_namespace)

DATA_ROOT = Path.home() / '.local' / 'share' / 'janus' / 'games'
RUN_ROOT = Path.home() / '.local' / 'share' / 'janus' / 'run'


def game_dir(key) -> Path:
    """Janus's directory for a non-Steam game: the handler installs the
    game (or its launcher) here. ctx.gameDir / ctx.installDir."""
    return DATA_ROOT / str(key)


def mem_total_mb() -> int:
    """Total RAM in MB (ctx.memTotalMb), 0 if unknown."""
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                if line.startswith('MemTotal:'):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


class NonSteamGame:
    """The bits of steam.SteamGame the CLI and GUIs read, for a Game.id
    handler."""

    is_native = True
    is_proton = False
    is_32bit = False
    launch_command = None
    proton_path = None

    def __init__(self, key, script):
        self.appid = key
        self.name = str(script.get('name') or key)
        self.install_dir = game_dir(key)


def base_ctx(script) -> Dict:
    """ctx for setupCheck, owner and actions (no instance)."""
    key = script.get('id')
    d = game_dir(key)
    d.mkdir(parents=True, exist_ok=True)
    return {'appid': key, 'gameName': str(script.get('name') or key),
            'gameDir': str(d), 'installDir': str(d),
            'profilesDir': str(profiles.DATA_DIR),
            'options': script.options, 'memTotalMb': mem_total_mb(),
            'verbose': script.verbose}


def setup_status(script) -> Dict:
    """Game.setupCheck, normalized: {ready, message, action, actionLabel,
    status}. *action* is the Janus.addAction key that fixes it ('' = none:
    the message says what to do); *status* is a line for when it's ready
    (e.g. the installed version). A throwing check counts as not ready."""
    out = {'ready': True, 'message': '', 'action': '', 'actionLabel': '',
           'status': ''}
    if not script.has_hook('setupCheck'):
        return out
    try:
        ret = (script.invoke('setupCheck', ctx=base_ctx(script)) or {}).get('ret')
    except scripting.HandlerError as e:
        return {**out, 'ready': False, 'message': str(e)}
    if ret is None or ret is True:
        return out
    if isinstance(ret, str):
        return {**out, 'ready': False, 'message': ret}
    if not isinstance(ret, dict):
        return {**out, 'ready': False,
                'message': f'Game.setupCheck returned {ret!r}'}
    action = str(ret.get('action') or '')
    specs = dict(script.action_specs)
    return {'ready': bool(ret.get('ready', False)),
            'message': str(ret.get('message') or ''),
            'action': action if action in specs else '',
            'actionLabel': str(specs.get(action, {}).get('label', '')),
            'status': str(ret.get('status') or '')}


ART_KINDS = ('cover', 'hero', 'logo')    # portrait cover, wide banner, title logo
ART_MAX_AGE = 30 * 24 * 3600             # fetch again after a month
_ART_MAX_BYTES = 20 << 20


def art_dir(key) -> Path:
    return game_dir(key) / 'art'


def art_file(key, kind: str) -> Optional[Path]:
    """The downloaded *kind* image (Game.art) of a non-Steam game, or None."""
    return next(iter(sorted(art_dir(key).glob(f'{kind}.*'))), None) \
        if art_dir(key).is_dir() else None


ART_RETRY_AGE = 24 * 3600                 # a kind still missing: try again daily
# Hub API fields (SteamGridDB art the hub looked up) for each kind.
HUB_ART = {'cover': 'portrait_url', 'hero': 'hero_url', 'logo': 'logo_url'}


def art_wanted(key) -> bool:
    """No artwork fetched yet, it's older than ART_MAX_AGE, or a kind is
    still missing and the last try is a day old."""
    stamp = art_dir(key) / '.fetched'
    try:
        age = time.time() - stamp.stat().st_mtime
    except OSError:
        return True
    if age > ART_MAX_AGE:
        return True
    return age > ART_RETRY_AGE and any(art_file(key, k) is None for k in ART_KINDS)


def _download_art(key, kind: str, url: str) -> bool:
    """Save one image as art_dir/<kind>.<ext>. Logs and returns False on
    anything that isn't an image."""
    import urllib.request
    d = art_dir(key)
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'janus-split-screen'})
        with urllib.request.urlopen(req, timeout=30) as resp:
            ctype = resp.headers.get('Content-Type', '')
            data = resp.read(_ART_MAX_BYTES + 1)
        ext = {'image/jpeg': 'jpg', 'image/png': 'png',
               'image/webp': 'webp'}.get(ctype.split(';')[0].strip())
        if not ext or len(data) > _ART_MAX_BYTES:
            print(f"[janus] {key} {kind} art: not an image ({ctype})", flush=True)
            return False
        for old in d.glob(f'{kind}.*'):
            old.unlink()
        tmp = d / f'.{kind}.part'
        tmp.write_bytes(data)
        tmp.replace(d / f'{kind}.{ext}')
        return True
    except Exception as e:
        print(f"[janus] {key} {kind} art: {e}", flush=True)
        return False


def _hub_art(key) -> Dict[str, str]:
    """{kind: URL} of the art the handler hub has for the game (SteamGridDB,
    looked up by the hub), {} when it has none or can't be reached."""
    import hub
    try:
        g = hub.find_game(key)
    except hub.HubError as e:
        print(f"[janus] {key} art from the hub: {e}", flush=True)
        return {}
    return {kind: g.get(field) for kind, field in HUB_ART.items() if g and g.get(field)}


def fetch_art(script) -> bool:
    """Download a non-Steam game's artwork into art_dir: what Game.art(ctx)
    points at ({cover, hero, logo}: image URLs), and for the kinds it doesn't
    give, the handler hub's (from SteamGridDB). Like Steam's library art:
    fetched to the user's machine, never shipped with Janus. Returns True if
    anything was saved. Errors are logged, not raised (art is decoration)."""
    key = script.get('id')
    urls = {}
    if script.has_hook('art'):
        r = script.invoke('art', ctx=base_ctx(script), soft=True)
        ret = (r or {}).get('ret')
        if isinstance(ret, dict):
            urls = {k: v for k, v in ret.items()
                    if k in ART_KINDS and isinstance(v, str) and v.startswith('https://')}
    if any(k not in urls for k in ART_KINDS):
        for kind, url in _hub_art(key).items():
            if url.startswith('https://'):
                urls.setdefault(kind, url)
    d = art_dir(key)
    d.mkdir(parents=True, exist_ok=True)
    saved = False
    for kind, url in urls.items():
        saved = _download_art(key, kind, url) or saved
    (d / '.fetched').touch()
    return saved


def menu_actions(script) -> List:
    """The actions shown as buttons in the game's options: [(key, spec)]."""
    return [(k, s) for k, s in script.action_specs if s.get('menu', True)]


def run_action(script, key: str) -> str:
    """Run a Janus.addAction; returns the message it returned ('' = none).
    Raises scripting.HandlerError when it fails."""
    ret = script.run_action(key, base_ctx(script))
    return ret if isinstance(ret, str) else ''


def owner(script) -> Optional[Dict]:
    """Game.owner: {name, label} of the account that replaces the Steam
    account as the owner profile, or None (keep the Steam account)."""
    if script is None or not script.has_hook('owner'):
        return None
    r = script.invoke('owner', ctx=base_ctx(script), soft=True)
    ret = (r or {}).get('ret')
    if not isinstance(ret, dict) or not ret.get('name'):
        return None
    return {'name': str(ret['name']), 'label': str(ret.get('label') or '')}


def with_owner(profile_ctx: Dict, own: Optional[Dict]) -> Dict:
    """A Profile.ctx() with the owner's name from Game.owner."""
    if own and profile_ctx.get('owner'):
        return {**profile_ctx, 'name': own['name']}
    return profile_ctx


def _instance_tag(key, instance: int) -> str:
    return f'{key}/{instance}'


def kill_instance(key, instance: int) -> int:
    """SIGKILL every process of this instance's game (they carry
    JANUS_INSTANCE_TAG in their environment): a launcher that is
    terminated doesn't take its game along. Returns how many."""
    marker = f'JANUS_INSTANCE_TAG={_instance_tag(key, instance)}\0'.encode()
    killed = 0
    for entry in os.listdir('/proc'):
        if not entry.isdigit() or int(entry) == os.getpid():
            continue
        try:
            with open(f'/proc/{entry}/environ', 'rb') as f:
                if marker not in f.read():
                    continue
            os.kill(int(entry), signal.SIGKILL)
            killed += 1
        except OSError:
            continue
    return killed


_SH_NAME = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def _wrapper_script(base_env: Dict[str, str], env: Dict[str, str],
                    prefix: List[str]) -> str:
    """A shell script that turns *base_env* (the launcher's environment)
    into *env* (the game's) and runs its arguments after *prefix* (the
    gamescope command, or nothing)."""
    lines = ['#!/bin/sh',
             "# Written by Janus for each launch: runs the game (\"$@\") in this",
             '# instance\'s tile with the hook. See src/host/nonsteam.py.']
    for k in sorted(base_env):
        if k not in env and _SH_NAME.match(k):
            lines.append(f'unset {k}')
    for k in sorted(env):
        if base_env.get(k) != env[k] and _SH_NAME.match(k):
            lines.append(f'export {k}={shlex.quote(env[k])}')
    lines.append(f'exec {shlex.join(prefix)} "$@"' if prefix else 'exec "$@"')
    return '\n'.join(lines) + '\n'


def launch(key, controller: Optional[int] = None, instance: int = 1,
           verbose: bool = False, extra_args: Optional[List[str]] = None,
           gamescope_args: Optional[List[str]] = None,
           nvidia_prime: bool = False, wmclass: Optional[str] = None,
           controller_vid: int = 0, controller_pid: int = 0,
           controller_name: Optional[str] = None,
           steam_input_filter: bool = False, script=None,
           script_ctx: Optional[Dict] = None,
           profile: Optional[str] = None,
           input_nodes: Optional[List[str]] = None,
           fps_limit: Optional[int] = None) -> subprocess.Popen:
    """launch_steam_game() for a non-Steam game (steam.py hands Game.id
    handlers here). Hooks: buildEnvironment -> buildCommand (required) ->
    play -> onInstanceStarted."""
    name = str(script.get('name') or key)
    status = setup_status(script)
    if not status['ready']:
        raise scripting.HandlerError(
            f"{name} isn't set up yet: {status['message'] or 'see its options'}")
    gdir = game_dir(key)
    player = profiles.resolve(profile, instance)
    own = owner(script)
    pctx = with_owner(player.ctx(), own)
    label = (own or {}).get('label') or 'Steam account'
    print(f"[janus] Instance {instance} plays as {pctx['name']}"
          f"{f' ({label})' if player.owner else ''}", flush=True)

    base_env = os.environ.copy()
    env = dict(base_env)
    janus_input = input_nodes is not None     # Janus input mode (steam.py)
    apply_hook_env(env, key, instance, None if janus_input else controller, verbose,
                   controller_vid=0 if janus_input else controller_vid,
                   controller_pid=0 if janus_input else controller_pid,
                   controller_name=None if janus_input else controller_name,
                   steam_input_filter=steam_input_filter and not janus_input)
    if janus_input:
        apply_janus_input_env(env, input_nodes)
    apply_perf_env(env, fps_limit, proton=False)     # no Steam shader cache
    env['JANUS_INSTANCE_TAG'] = _instance_tag(key, instance)
    script.apply_env(env)

    run_dir = RUN_ROOT / str(key)
    run_dir.mkdir(parents=True, exist_ok=True)
    wrapper = run_dir / f'i{instance}.sh'
    meta = script_ctx or {}
    sctx = {
        'appid': key, 'gameName': name,
        'instance': instance, 'rank': instance - 1,
        'numPlayers': meta.get('numPlayers'),
        'profile': pctx,
        'players': [{**p, 'profile': with_owner(p['profile'], own)}
                    for p in (meta.get('players') or
                              [{'instance': instance, 'profile': player.ctx()}])],
        'saveRoot': str(player.data_dir(key)),
        'profilesDir': str(profiles.DATA_DIR),
        'controller': {'index': controller, 'vid': controller_vid,
                       'pid': controller_pid, 'name': controller_name},
        'inputMode': 'janus' if janus_input else 'steam',
        'width': meta.get('width'), 'height': meta.get('height'),
        'posX': meta.get('posX'), 'posY': meta.get('posY'),
        'installDir': str(gdir), 'gameDir': str(gdir),
        'home': str(Path.home()), 'winePrefix': None,
        'isNative': True, 'isProton': False, 'is32bit': False,
        'wrapper': str(wrapper), 'memTotalMb': mem_total_mb(),
        'options': script.options,
        'env': {}, 'gameArgs': [],
        'verbose': verbose, 'pid': None, 'lobbyId': None,
    }

    def absorb(r):
        # As in launch_steam_game: gameArgs accumulate, ctx.env merges into
        # the game's environment (null deletes).
        if not r:
            return
        if isinstance(r.get('gameArgs'), list):
            sctx['gameArgs'] = [str(a) for a in r['gameArgs']]
        if isinstance(r.get('ctxEnv'), dict):
            for k, v in r['ctxEnv'].items():
                if v is None:
                    env.pop(str(k), None)
                else:
                    env[str(k)] = str(v)
            sctx['env'] = {k: v for k, v in r['ctxEnv'].items() if v is not None}

    r = script.invoke('buildEnvironment', ctx=sctx, env=env)
    if r and isinstance(r.get('env'), dict):
        env = {str(k): str(v) for k, v in r['env'].items() if v is not None}
    absorb(r)

    r = script.invoke('buildCommand', ctx=sctx)
    absorb(r)
    cmd = (r or {}).get('ret')
    if not isinstance(cmd, list) or not cmd:
        raise scripting.HandlerError(
            f"{name}: Game.buildCommand must return the command (an argv array)")
    cmd = [str(a) for a in cmd] + list(extra_args or [])
    absorb(script.invoke('play', ctx=sctx))
    cmd += sctx['gameArgs']

    if script.get('launcherStartsGame'):
        # The launcher runs outside the tile; the game it starts goes through
        # the wrapper, which carries everything the game needs.
        prefix = (wrap_in_gamescope([], env, gamescope_args, nvidia_prime, wmclass)
                  if gamescope_args is not None else [])
        if janus_input:
            prefix += wrap_input_namespace([], input_nodes, instance)
        wrapper.write_text(_wrapper_script(base_env, env, prefix))
        wrapper.chmod(0o755)
        spawn_env = base_env
        log = open(run_dir / f'i{instance}.log', 'w')
        print(f"[janus] Instance {instance}: {shlex.join(cmd)} "
              f"(log: {run_dir / f'i{instance}.log'})", flush=True)
        proc = subprocess.Popen(cmd, env=spawn_env, cwd=gdir,
                                stdout=log, stderr=subprocess.STDOUT)
        log.close()
    else:
        if janus_input:
            cmd = wrap_input_namespace(cmd, input_nodes, instance)
        if gamescope_args is not None:
            cmd = wrap_in_gamescope(cmd, env, gamescope_args, nvidia_prime, wmclass)
        proc = subprocess.Popen(cmd, env=env, cwd=gdir)

    proc.janus_cleanup = lambda: kill_instance(key, instance)
    sctx['pid'] = proc.pid
    proc.janus_script_ctx = sctx
    script.invoke('onInstanceStarted', ctx=sctx, soft=True)
    return proc

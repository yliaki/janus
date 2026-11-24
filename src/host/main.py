#!/usr/bin/env python3
"""
Janus Split Screen - Linux Gamepad Input Splitting Tool

Main entry point for the host application.
"""

import argparse
import os
import signal
import subprocess
import shlex
import sys
import threading
import time
from pathlib import Path

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from controllers import list_controllers, format_controller_list
from steam import launch_steam_game, SteamGame, get_hook_paths
from focus_release import run_daemon as run_focus_release
import diagnose as diagnose_mod
import prereqs
import scripting
import steam_input
import controller_input as input_mode
from padd import PadDaemonError
from janus_procs import kill_janus_processes


def _load_script_for_args(args):
    """Load the appid's handler script per CLI flags (-o / --handler).
    Exits with a clear error when there is none, it's broken, or -o is bad:
    Janus doesn't launch a game without a handler."""
    try:
        opts = scripting.parse_option_args(getattr(args, 'option', None))
        return scripting.load_handler(
            args.appid, option_values=opts,
            verbose=getattr(args, 'verbose', False),
            explicit_path=getattr(args, 'handler', None))
    except (scripting.HandlerError, ValueError) as e:
        print(f"Error in handler script: {e}", file=sys.stderr)
        sys.exit(1)


def _game_and_script(args):
    """The game and its handler for args.appid (a Steam appid or a non-Steam
    game id). Exits with a clear error when either is missing."""
    import nonsteam
    if scripting.is_steam_key(args.appid):
        try:
            game = SteamGame(args.appid)
        except FileNotFoundError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        return game, _load_script_for_args(args)
    script = _load_script_for_args(args)
    return nonsteam.NonSteamGame(args.appid, script), script


def _resolve_input_mode(script, args):
    """args.input (None = default) -> the mode this launch uses (controller_input.py)."""
    mode = input_mode.resolve(script, getattr(args, 'input', None))
    if getattr(args, 'input', None) and args.input != mode:
        print(f"[janus] {game_label(script)} doesn't support "
              f"{input_mode.LABELS[args.input]}; using {input_mode.LABELS[mode]}",
              file=sys.stderr, flush=True)
    print(f"[janus] Controller input: {input_mode.LABELS[mode]}", flush=True)
    return mode


def game_label(script):
    return (script.get('name') if script is not None else None) or 'This game'


def _require_steam_input(script, game, controller_indices=(), mode='steam'):
    """Refuse the launch unless the game's Steam Input setting suits *mode*
    (steam_input.check). Pads that work badly in *mode* (input_mode.
    pad_warnings) and, for Game.requireGamepadLayout in Steam Input mode,
    pads without a Gamepad layout only warn."""
    by_index = {c.index: c for c in list_controllers()}
    pads = [by_index[i] for i in controller_indices if i in by_index]
    warnings = input_mode.pad_warnings(mode, pads)
    if mode == input_mode.STEAM:
        layout = steam_input.gamepad_layout_warning(game.appid, script, pads, game.name)
        if layout:
            warnings.append(layout)
    for warning in warnings:   # the GUIs offer "Launch anyway"; the CLI warns and goes on
        print(f"[janus] Warning: {warning}", file=sys.stderr, flush=True)
    try:
        steam_input.check(game.appid, script, game.name, mode)
    except steam_input.SteamInputNotEnabled as e:
        print(f"Error: {e}", file=sys.stderr)
        if e.alternative:
            print(f"(add --input {e.alternative})", file=sys.stderr)
        sys.exit(1)


def _check_build(script, game, args):
    """Exit when Steam installed the game's other build (Linux vs Windows)
    than the launch is set up for (prereqs.build_problem). Before the Steam
    Input check: no setting fixes a launch that can't find its executable."""
    problem = prereqs.build_problem(game, script, args.exe, args.launch_cmd)
    if problem:
        print(f"Error: {problem}", file=sys.stderr)
        sys.exit(1)
    # Never started from Steam (no Proton prefix): start it once there first.
    import first_run
    if first_run.needed(game, script):
        print(f"[janus] {first_run.message(game)}", flush=True)
        err = first_run.run(game, progress=lambda m: print(f"[janus] {m}", flush=True))
        if err:
            print(f"Error: {err}", file=sys.stderr)
            sys.exit(1)


def _check_prereqs(script, game, args, gamescope=True):
    """Exit if gamescope is needed but unusable; print a warning when the
    game's Steam Linux Runtime isn't installed (prereqs.py)."""
    problem = prereqs.gamescope_problem() if gamescope else None
    if problem:
        print(f"Error: {problem}", file=sys.stderr)
        sys.exit(1)
    if not getattr(args, 'no_runtime', False):
        warning = prereqs.runtime_warning(game, script)
        if warning:
            print(f"[janus] Warning: {warning}", file=sys.stderr, flush=True)


def _print_launch_message(script, game):
    """The handler's Game.launchMessage. The GUIs wait for an OK; the CLI
    only prints it, so scripted launches don't block."""
    message = scripting.launch_message(script)
    if message:
        print(f"\n[janus] Before you play {game.name}:\n{message}\n", flush=True)


def _merge_script_args(script, args, isolation_default='none'):
    """Apply handler-sheet defaults under CLI flags (CLI > script > builtin).

    A flag left at its argparse default falls through to the script's value.
    Limitation (documented in SCRIPTING.md): explicitly passing a flag equal
    to its default cannot override the script."""
    if script is None:
        return
    for attr, field, default in [
        ('isolation', 'isolation', isolation_default),
        ('goldberg', 'goldberg', False),
        ('goldberg_no_overlay', 'goldbergNoOverlay', True),
        ('goldberg_disable_lan', 'goldbergDisableLan', True),
        ('goldberg_debug', 'goldbergDebug', False),
        ('exe', 'exeName', None),
        ('launch_cmd', 'launchCommand', None),
        ('steam_input_filter', 'steamInputFilter', False),
        ('nvidia', 'nvidia', False),
        ('verbose', 'verbose', False),
        ('copy_prefix', 'copyPrefix', False),
        ('copy_userdata', 'copyUserdata', False),
    ]:
        if hasattr(args, attr):
            setattr(args, attr, scripting.effective(
                script, field, getattr(args, attr), default, default))
    if hasattr(args, 'no_runtime'):
        args.no_runtime = not scripting.effective(
            script, 'useRuntime', not args.no_runtime, True, True)
    # The script was constructed before the merge, so Game.verbose couldn't
    # influence its own logging — propagate the merged value back.
    script.verbose = script.verbose or bool(getattr(args, 'verbose', False))


def _merge_script_game_args(script, cli_game_args):
    """Game.gameArgs (a string, shlex-split) prepended to CLI game args."""
    sheet = shlex.split(script.get('gameArgs', '') or '') if script else []
    return sheet + (cli_game_args or [])


def cmd_list(args):
    """List connected controllers."""
    controllers = list_controllers()
    print("Connected controllers (kernel ordering):")
    print(format_controller_list(controllers))
    print(f"\nTotal: {len(controllers)} controller(s)")


def _interrupt_on_signals():
    """SIGTERM — and SIGINT even when it started out ignored (a launcher
    started from a script) — stop a session like Ctrl-C: the KeyboardInterrupt
    paths stop the games, the input daemon and restore the desktop. With the
    default action the process just died and the games kept running."""
    def interrupt(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGTERM, interrupt)


def cmd_steam(args):
    """Launch a Steam game."""
    _interrupt_on_signals()
    # Check hook libraries exist
    hook_paths = get_hook_paths()
    if not hook_paths:
        print("Error: Hook libraries not found", file=sys.stderr)
        print("Run 'make' in src/hook/ first.", file=sys.stderr)
        sys.exit(1)

    game, script = _game_and_script(args)
    _merge_script_args(script, args, isolation_default='none')
    _check_build(script, game, args)
    mode = _resolve_input_mode(script, args)
    _require_steam_input(script, game,
                         [args.controller] if args.controller is not None else [], mode)
    _check_prereqs(script, game, args, gamescope=args.gamescope is not None)
    _print_launch_message(script, game)

    print(f"[janus] Launching {game.name} ({args.appid})")
    if game.is_proton:
        print(f"[janus] Type: Proton ({game.proton_path.name if game.proton_path else 'unknown'})")

    if args.controller is not None:
        print(f"[janus] Controller: {args.controller}")
    if args.instance > 1:
        print(f"[janus] Instance: {args.instance}")
    if args.isolation != 'none':
        print(f"[janus] Isolation: {args.isolation}")
    # Parse gamescope args from string to list
    gamescope_args = None
    if args.gamescope is not None:
        gamescope_args = shlex.split(args.gamescope) if args.gamescope else []
        if script is not None and script.get('extraGamescopeArgs'):
            gamescope_args += shlex.split(script.get('extraGamescopeArgs'))
        print(f"[janus] Gamescope: {args.gamescope if args.gamescope else '(default)'}")
    if args.nvidia:
        print(f"[janus] NVIDIA PRIME: enabled")
    if args.goldberg:
        variant = 'debug' if args.goldberg_debug else 'regular'
        print(f"[janus] Goldberg: enabled ({variant}, LAN multiplayer via steam_api replacement)")
        if args.goldberg_no_overlay:
            print(f"[janus] Goldberg: overlay disabled")
        if args.goldberg_disable_lan:
            print(f"[janus] Goldberg: LAN auto-discovery disabled")

    sys.stdout.flush()

    # Per-instance metadata for the script ctx (game resolution from
    # gamescope's -w/-h when given)
    script_ctx = {'numPlayers': None}
    for flag, key in (('-w', 'width'), ('-h', 'height')):
        if gamescope_args and flag in gamescope_args:
            i = gamescope_args.index(flag)
            try:
                script_ctx[key] = int(gamescope_args[i + 1])
            except (IndexError, ValueError):
                pass

    # Bind the hook to the pad's identity (VID:PID + name), as steam-split
    # and the GUIs do: with the bare index, the hook renumbers when other
    # pads disconnect and then blocks this one.
    pad = None
    if args.controller is not None:
        pads = {c.index: c for c in list_controllers()}
        pad = pads.get(args.controller)
        if pad is None:
            print(f"Error: No controller at index {args.controller}. Connected: "
                  f"{', '.join(str(i) for i in pads) or '(none)'} "
                  f"(see 'main.py list')", file=sys.stderr)
            sys.exit(1)

    merged_game_args = _merge_script_game_args(script, args.game_args)
    # Janus input with a controller: its own virtual Xbox pad (padd.py).
    # Without -c the game reads every pad directly (nothing to isolate).
    daemon = None
    if mode == input_mode.JANUS and pad is not None:
        try:
            daemon = input_mode.start_daemon(game.appid, [(args.instance, pad.index)],
                                             args.verbose)
        except PadDaemonError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    try:
        proc = launch_steam_game(
            appid=args.appid,
            controller=args.controller,
            controller_vid=pad.vendor_id if pad else 0,
            controller_pid=pad.product_id if pad else 0,
            controller_name=pad.name if pad else None,
            instance=args.instance,
            isolation=args.isolation,
            verbose=args.verbose,
            extra_args=merged_game_args if merged_game_args else None,
            use_runtime=not args.no_runtime,
            gamescope_args=gamescope_args,
            nvidia_prime=args.nvidia,
            copy_prefix=args.copy_prefix,
            copy_userdata=args.copy_userdata,
            goldberg_enabled=args.goldberg,
            goldberg_no_overlay=args.goldberg_no_overlay,
            goldberg_disable_lan=args.goldberg_disable_lan,
            goldberg_debug=args.goldberg_debug,
            exe_name=args.exe,
            launch_command=args.launch_cmd,
            steam_input_filter=args.steam_input_filter,
            script=script,
            script_ctx=script_ctx,
            profile=args.profile,
            input_nodes=daemon.nodes[args.instance] if daemon else None,
            fps_limit=args.fps_limit,
        )
    except (FileNotFoundError, PermissionError, ValueError,
            scripting.HandlerError) as e:
        if daemon is not None:
            daemon.stop()
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Start focus-release hotkey daemon when using gamescope
    if gamescope_args is not None:
        t = threading.Thread(target=run_focus_release, args=('alt+1',), daemon=True)
        t.start()

    exit_code = None
    try:
        exit_code = proc.wait()
    except KeyboardInterrupt:
        print("\n[janus] Terminating game...", flush=True)
        proc.terminate()
        try:
            exit_code = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        if getattr(proc, 'janus_cleanup', None):
            proc.janus_cleanup()     # a launcher's game (nonsteam.py)
        # gamescopereaper / wine re-parent away from the Popen: sweep them.
        killed = kill_janus_processes()
        if killed:
            print(f"[janus] force-killed {killed} leftover process(es)", flush=True)
    finally:
        if daemon is not None:
            daemon.stop()
        if script is not None:
            sctx = getattr(proc, 'janus_script_ctx', None) or {}
            script.invoke('onInstanceExit',
                          ctx={**sctx, 'exitCode': exit_code}, soft=True)
            script.invoke('onStop',
                          ctx={'appid': args.appid, 'numPlayers': 1},
                          soft=True)
            script.finish()


def _parse_tile(spec):
    """Parse a --tile 'controller:x,y,w,h[@profile]' spec into a TileSpec
    (native px)."""
    from split_session import TileSpec
    spec, _, profile = spec.partition('@')
    try:
        ctrl, rect = spec.split(':', 1)
        x, y, w, h = (int(v) for v in rect.split(','))
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid tile '{spec}', expected controller:x,y,w,h[@profile] "
            f"(e.g. 0:0,0,1920,2160@Alice)")
    return TileSpec(controller=int(ctrl), x=x, y=y, w=w, h=h,
                    profile=profile.strip() or None)


def cmd_profiles(args):
    """List / add / rename / remove player profiles."""
    import profiles
    try:
        if args.action == 'add':
            p = profiles.create(args.name)
            print(f"Created {p.name} ({p.id}, Steam ID {p.steam_id})")
        elif args.action == 'rename':
            p = profiles.find(args.name)
            if p is None:
                raise ValueError(f'No profile "{args.name}"')
            p = profiles.rename(p.id, args.new_name)
            print(f"Renamed to {p.name}")
        elif args.action == 'remove':
            p = profiles.find(args.name)
            if p is None:
                raise ValueError(f'No profile "{args.name}"')
            profiles.delete(p.id, delete_data=args.delete_saves)
            print(f"Removed {p.name}" + (" and its saves" if args.delete_saves else
                                        f" (saves kept in {profiles.DATA_DIR / p.id})"))
        else:
            for p in profiles.load():
                tag = '  (Steam account: real saves)' if p.owner else ''
                print(f"{p.id:<10} {p.name:<24} {p.steam_id}{tag}")
    except (ValueError, KeyError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_hub(args):
    """Search the handler hub and install / update / remove its handlers."""
    import hub
    where_text = {'installed': 'installed', 'library': 'in your library, not installed',
                  'store': 'not in your library', 'janus': 'not on Steam'}

    def show(r):
        mark = (f"handler v{r['version']}" if r['status'] == 'current' else
                f"update v{r['version']} -> v{r['latest_version']}" if r['status'] == 'update' else
                "has a handler" if r["handler"] else
                f"v{r['latest_version']} available")
        print(f"{str(r['key']):>10}  {r['name'][:32]:<32} {r['installs']:>6} installs  "
              f"{mark:<22} ({where_text[r['where']]})")

    def get(rows):
        """Confirm, then install each game's latest handler."""
        if not rows:
            print("Nothing to install.")
            return
        details = [hub.game_detail(r['id']) for r in rows]
        for d in details:
            v = d['versions'][0]
            print(f"{d['name']} v{v['number']} by {v['uploader'] or '(deleted)'}"
                  f"  {hub.page_url(d['id'])}")
            if v['changelog']:
                print(f"    {v['changelog']}")
        print("Handlers run with file and process access; read them on the hub first.")
        if not args.yes:
            try:
                if input(f"Install {len(details)} handler(s)? [y/N] ").lower() != 'y':
                    return
            except EOFError:
                return
        failed = False
        where = {r['id']: r['where'] for r in rows}
        for d in details:
            try:
                rec = hub.install(d['id'], detail=d)
                note = (f"  — install the game in Steam to play it (steam://install/{d['steam_appid']})"
                        if where.get(d['id']) in ('library', 'store') else '')
                print(f"Installed {d['name']} v{rec['version']}{note}")
            except hub.HubError as e:
                failed = True
                print(f"{d['name']}: {e}", file=sys.stderr)
        if failed:
            sys.exit(1)

    try:
        if args.action == 'get':
            if args.all or args.all_installed:
                rows = [r for r in hub.catalog() if r['wanted']
                        and (args.all or r['where'] == 'installed' or r['status'] == 'update')]
            else:
                rows, every = [], None
                for want in args.games:
                    key = want if not want.isdigit() else int(want)
                    if hub.scripting.GAME_ID_RE.match(want) or want.isdigit():
                        g = hub.find_game(key)
                        if g:
                            every = every or {r['id']: r for r in hub.catalog()}
                            rows.append(every[g['id']])
                            continue
                    hits = hub.catalog(want)
                    if len(hits) != 1:
                        names = ', '.join(h['name'] for h in hits) or 'nothing'
                        raise hub.HubError(f'"{want}" matches {names} on the hub')
                    rows.append(hits[0])
            get(rows)
        elif args.action == 'update':
            get([r for r in hub.catalog() if r['status'] == 'update'])
        elif args.action == 'remove':
            for key in args.games:
                print(f"Removed {key}" if hub.uninstall(key)
                      else f"{key}: no handler from the hub")
        else:
            text = ' '.join(args.text) if args.action == 'search' else ''
            rows = hub.catalog(text, getattr(args, 'sort', None) or 'popular')
            if getattr(args, 'mine', False):
                rows = [r for r in rows if r['where'] in ('installed', 'library') or r['status']]
            for r in rows:
                show(r)
            if not rows:
                print("No handlers found.")
    except hub.HubError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_steam_split(args):
    """Launch a multi-instance split-screen session (KWin Wayland, Path A)."""
    _interrupt_on_signals()
    hook_paths = get_hook_paths()
    if not hook_paths:
        print("Error: Hook libraries not found", file=sys.stderr)
        print("Run 'make' in src/hook/ first.", file=sys.stderr)
        sys.exit(1)
    game, script = _game_and_script(args)

    from split_session import launch_split_session
    _merge_script_args(script, args, isolation_default='secondary')
    _check_build(script, game, args)
    mode = _resolve_input_mode(script, args)
    _require_steam_input(script, game, [t.controller for t in args.tile], mode)
    _check_prereqs(script, game, args)
    _print_launch_message(script, game)
    print(f"[janus] {game.name} ({args.appid}) — split-screen, "
          f"{len(args.tile)} instance(s)")
    merged_game_args = _merge_script_game_args(script, args.game_args)
    try:
        launch_split_session(
            appid=args.appid,
            tiles=args.tile,
            output=args.output,
            fps_limit=args.fps_limit,
            isolation=args.isolation,
            verbose=args.verbose,
            nvidia=args.nvidia,
            copy_prefix=args.copy_prefix,
            copy_userdata=args.copy_userdata,
            goldberg=args.goldberg,
            goldberg_no_overlay=args.goldberg_no_overlay,
            goldberg_disable_lan=args.goldberg_disable_lan,
            goldberg_debug=args.goldberg_debug,
            exe=args.exe,
            launch_command=args.launch_cmd,
            extra_args=merged_game_args or None,
            steam_input_filter=args.steam_input_filter,
            script=script,
            keep_above=args.keep_above,
            render_scale=args.render_scale,
            upscale_filter=args.upscale_filter,
            sharpness=args.sharpness,
            input_mode=mode,
        )
    except (RuntimeError, FileNotFoundError, PermissionError, ValueError,
            scripting.HandlerError, PadDaemonError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_setup(args):
    """Set up a non-Steam game (Game.setupCheck + its fix actions), or run
    one of its actions (--action)."""
    import nonsteam
    script = _load_script_for_args(args)
    if not script.non_steam:
        print(f"{args.appid} is a Steam game: Steam installs it.", file=sys.stderr)
        sys.exit(1)
    script.progress = lambda text: None      # Janus.progress already logs
    actions = dict(script.action_specs)

    def run(key):
        try:
            msg = nonsteam.run_action(script, key)
        except scripting.HandlerError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        if msg:
            print(msg)

    if args.action:
        if args.action not in actions:
            print(f"No action '{args.action}'. Actions: "
                  f"{', '.join(actions) or 'none'}", file=sys.stderr)
            sys.exit(1)
        run(args.action)
        return
    tried = set()
    while True:
        st = nonsteam.setup_status(script)
        if st['ready']:
            print(f"{script.get('name') or args.appid} is ready"
                  + (f": {st['status']}" if st['status'] else '.'))
            menu = nonsteam.menu_actions(script)
            if menu:
                print("Actions (--action KEY): " + ', '.join(
                    f"{k} ({s['label']})" for k, s in menu))
            return
        print(st['message'])
        if not st['action'] or st['action'] in tried:
            sys.exit(1)
        tried.add(st['action'])
        if not args.yes:
            try:
                if input(f"{st['actionLabel']}? [Y/n] ").strip().lower() == 'n':
                    sys.exit(1)
            except EOFError:
                sys.exit(1)
        run(st['action'])


def cmd_diagnose(args):
    """Print Steam install + runtime detection diagnostics."""
    diagnose_mod.run(args.appid)


def main():
    parser = argparse.ArgumentParser(
        description='Janus Split Screen - Linux Gamepad Input Splitting Tool',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s list                              # List connected controllers
  %(prog)s steam 550 -c 0                    # Launch L4D2 with controller 0
  %(prog)s steam 550 -c 0 -i 1 --isolation secondary   # Primary instance
  %(prog)s steam 550 -c 1 -i 2 --isolation secondary   # Isolated instance 2
  %(prog)s steam 550 -c 0 --gamescope="-w 1280 -h 720"   # Launch in gamescope
  %(prog)s steam-split 550 --tile 0:0,0,1920,2160 --tile 1:1920,0,1920,2160  # Split-screen
  %(prog)s steam-split 550 --tile 0:0,0,1920,2160@Alice --tile 1:1920,0,1920,2160@Charlie
  %(prog)s profiles add Charlie               # A new player with their own saves
"""
    )

    subparsers = parser.add_subparsers(dest='command', help='Command to run')

    # list command
    list_parser = subparsers.add_parser('list', help='List connected controllers')
    list_parser.set_defaults(func=cmd_list)

    # steam command
    steam_parser = subparsers.add_parser('steam', help='Launch Steam game')
    steam_parser.add_argument('appid', type=scripting.parse_game_key,
                              help='Steam App ID, or a non-Steam game id')
    steam_parser.add_argument('-c', '--controller', type=int, help='Controller index')
    steam_parser.add_argument('-i', '--instance', type=int, default=1,
                              help='Instance number (1=primary, 2+=secondary)')
    steam_parser.add_argument('-p', '--profile', type=str, default=None,
                              help='Player profile (name or id, see "profiles"). '
                                   'Default: the Steam account for instance 1, '
                                   '"Player N" for instance N')
    steam_parser.add_argument('--isolation', choices=['none', 'prefix', 'secondary', 'full'],
                              default='none',
                              help="Isolation: none | prefix (per-instance Wine "
                                   "prefix, shared home — keeps Steam Input/controller "
                                   "UI working) | secondary | full")
    steam_parser.add_argument('-v', '--verbose', action='store_true',
                              help='Enable verbose logging')
    steam_parser.add_argument('--no-runtime', action='store_true',
                              help='Skip Steam Runtime wrapper')
    steam_parser.add_argument('--gamescope', nargs='?', const='', metavar='ARGS',
                              help='Wrap in gamescope with given options (e.g., --gamescope="-w 1280 -h 720 -b")')
    steam_parser.add_argument('--fps-limit', type=int, default=60,
                              help='Frame limit, 0 = unlimited (default 60): DXVK/vkd3d '
                                   '(Proton) or MangoHud (native) limiter')
    steam_parser.add_argument('--nvidia', action='store_true',
                              help='Add NVIDIA PRIME offload env vars for hybrid GPU')
    steam_parser.add_argument('--copy-prefix', action='store_true',
                              help='Full-copy Wine prefix for isolated Proton instances (default: smart symlink+copy)')
    steam_parser.add_argument('--copy-userdata', action='store_true',
                              help='Copy user data (saves/config) from original prefix (default: empty skeleton)')
    steam_parser.add_argument('--goldberg', action='store_true',
                              help='Use Goldberg Steam Emulator for LAN multiplayer (replaces steam_api via symlinked mirror)')
    steam_parser.add_argument('--goldberg-no-overlay', action='store_true', default=True,
                              help='Disable Goldberg overlay (default: true, prevents freezes in Proton)')
    steam_parser.add_argument('--goldberg-overlay', action='store_false', dest='goldberg_no_overlay',
                              help='Enable Goldberg overlay (may cause freezes in Proton)')
    steam_parser.add_argument('--goldberg-no-lan', action='store_true', default=True,
                              dest='goldberg_disable_lan',
                              help='Disable Goldberg LAN auto-discovery (default: true; prevents '
                                   'two same-host instances from auto-joining each other and '
                                   'deadlocking the netcode, e.g. Lethal Company)')
    steam_parser.add_argument('--goldberg-lan', action='store_false', dest='goldberg_disable_lan',
                              help='Enable Goldberg LAN auto-discovery (broadcast on UDP; may '
                                   'cause freezes with games that auto-join, but needed if you '
                                   'want a single-instance LAN game discoverable by other devices)')
    steam_parser.add_argument('--goldberg-debug', action='store_true', default=False,
                              dest='goldberg_debug',
                              help='Use the debug gbe_fork build (writes STEAM_LOG_*.log so '
                                   'the GUI lobby watcher can detect lobby IDs). Bigger, slower; place files in '
                                   'vendor/goldberg/debug/.')
    steam_parser.add_argument('--exe', type=str, default=None,
                              help='Executable filename to search for (e.g. Borderlands2.exe)')
    steam_parser.add_argument('--launch-cmd', type=str, default=None, metavar='CMD',
                              help='Full launch command, relative to the game install dir '
                                   '(e.g. "./jre/bin/java -Xmx4G -jar Necesse.jar"). '
                                   'Overrides --exe and disables executable auto-detection.')
    steam_parser.add_argument('--input', choices=('janus', 'steam'), default=None,
                              help="How controllers reach the game: 'janus' (default: "
                                   "Janus's own virtual Xbox pads, Steam Input off for the "
                                   "game) or 'steam' (Steam Input). Handlers may allow only one.")
    steam_parser.add_argument('--steam-input-filter', action='store_true',
                              help='Keep Steam Input active per-instance and filter it via '
                                   'vtable (instead of nulling it). Needed for controllers '
                                   'that only work through Steam Input.')
    steam_parser.add_argument('-o', '--option', action='append', metavar='KEY=VALUE',
                              help='Set a handler-script option declared via '
                                   'Janus.addOption (repeatable)')
    steam_parser.add_argument('--handler', type=str, default=None, metavar='PATH',
                              help='Explicit handler script path (overrides '
                                   'the ~/.config/janus/games + handlers/ search)')
    steam_parser.add_argument('game_args', nargs='*', help='Extra game arguments')
    steam_parser.set_defaults(func=cmd_steam)

    # steam-split command (KWin Wayland multi-instance split-screen, "Path A")
    split_parser = subparsers.add_parser(
        'steam-split',
        help='Launch a multi-instance split-screen session (KWin Wayland)')
    split_parser.add_argument('appid', type=scripting.parse_game_key,
                              help='Steam App ID, or a non-Steam game id')
    split_parser.add_argument('--tile', action='append', type=_parse_tile,
                              required=True, metavar='C:X,Y,W,H[@PROFILE]',
                              help='Instance tile controller:x,y,w,h in native px '
                                   '(repeatable), optionally @profile. e.g. '
                                   '--tile 0:0,0,1920,2160@Alice '
                                   '--tile 1:1920,0,1920,2160@Charlie')
    split_parser.add_argument('--output', type=str, default=None,
                              help='Target output name (default: primary)')
    split_parser.add_argument('--fps-limit', '--rate', dest='fps_limit', type=int, default=60,
                              help='Frame limit per instance, 0 = unlimited (default 60): '
                                   'gamescope -r/-o + DXVK/vkd3d (Proton) or MangoHud '
                                   '(native) limiter. --rate is the old name')
    split_parser.add_argument('--isolation', choices=['none', 'prefix', 'secondary', 'full'],
                              default='secondary',
                              help="Isolation (default: secondary). 'prefix' = "
                                   "per-instance Wine prefix but shared home, so "
                                   "Steam Input / controller menus keep working")
    split_parser.add_argument('-v', '--verbose', action='store_true')
    split_parser.add_argument('--nvidia', action='store_true',
                              help='NVIDIA PRIME offload env vars')
    split_parser.add_argument('--copy-prefix', action='store_true',
                              help='Full-copy Wine prefix for isolated Proton instances')
    split_parser.add_argument('--copy-userdata', action='store_true',
                              help='Copy user data (saves/config) from original prefix')
    split_parser.add_argument('--goldberg', action='store_true',
                              help='Use Goldberg Steam Emulator for LAN multiplayer')
    split_parser.add_argument('--goldberg-no-overlay', action='store_true',
                              default=True, dest='goldberg_no_overlay')
    split_parser.add_argument('--goldberg-overlay', action='store_false',
                              dest='goldberg_no_overlay')
    split_parser.add_argument('--goldberg-no-lan', action='store_true',
                              default=True, dest='goldberg_disable_lan')
    split_parser.add_argument('--goldberg-lan', action='store_false',
                              dest='goldberg_disable_lan')
    split_parser.add_argument('--goldberg-debug', action='store_true',
                              default=False, dest='goldberg_debug')
    split_parser.add_argument('--exe', type=str, default=None,
                              help='Executable filename to search for')
    split_parser.add_argument('--launch-cmd', type=str, default=None, metavar='CMD',
                              help='Full launch command, relative to the game install dir '
                                   '(e.g. "./jre/bin/java -Xmx4G -jar Necesse.jar"). '
                                   'Overrides --exe and disables executable auto-detection.')
    split_parser.add_argument('--input', choices=('janus', 'steam'), default=None,
                              help="How controllers reach the game: 'janus' (default: "
                                   "Janus's own virtual Xbox pads, Steam Input off for the "
                                   "game) or 'steam' (Steam Input). Handlers may allow only one.")
    split_parser.add_argument('--steam-input-filter', action='store_true',
                              help='Keep Steam Input active per-instance and filter it via '
                                   'vtable (instead of nulling it). Needed for controllers '
                                   'that only work through Steam Input.')
    split_parser.add_argument('-o', '--option', action='append', metavar='KEY=VALUE',
                              help='Set a handler-script option declared via '
                                   'Janus.addOption (repeatable)')
    split_parser.add_argument('--keep-above', action='store_true',
                              help="Force the tiles always-on-top (KWin 'above' "
                                   "rule). Off by default.")
    split_parser.add_argument('--no-keep-above', action='store_false',
                              dest='keep_above',
                              help="Don't force the tiles always-on-top "
                                   "(the default; kept for old command lines)")
    split_parser.add_argument('--render-scale', type=int, default=100, metavar='PCT',
                              help='Game resolution as %% of each tile (10-100, default '
                                   '100); gamescope upscales it. Lower = faster, and a '
                                   'bigger UI in games whose UI is sized in pixels')
    split_parser.add_argument('--upscale-filter', default='linear',
                              choices=['linear', 'nearest', 'fsr', 'nis', 'pixel'],
                              help='gamescope upscale filter below 100%% (default linear)')
    split_parser.add_argument('--sharpness', type=int, default=2, metavar='0-20',
                              help='fsr/nis sharpness, 0 = sharpest (default 2)')
    split_parser.add_argument('--handler', type=str, default=None, metavar='PATH',
                              help='Explicit handler script path (overrides '
                                   'the ~/.config/janus/games + handlers/ search)')
    split_parser.add_argument('game_args', nargs='*', help='Extra game arguments')
    split_parser.set_defaults(func=cmd_steam_split)

    # setup command (non-Steam games)
    setup_parser = subparsers.add_parser(
        'setup', help="Set up a non-Steam game (or run one of its actions)")
    setup_parser.add_argument('appid', type=scripting.parse_game_key, metavar='GAME',
                              help='Non-Steam game id (its handler\'s Game.id)')
    setup_parser.add_argument('--action', metavar='KEY',
                              help="Run this action of the handler's instead")
    setup_parser.add_argument('-y', '--yes', action='store_true',
                              help="Run the fixes without asking")
    setup_parser.add_argument('-o', '--option', action='append', metavar='KEY=VALUE',
                              help='Set a handler-script option (repeatable)')
    setup_parser.add_argument('-v', '--verbose', action='store_true')
    setup_parser.add_argument('--handler', type=str, default=None, metavar='PATH',
                              help='Explicit handler script path')
    setup_parser.set_defaults(func=cmd_setup)

    # profiles command
    prof_parser = subparsers.add_parser('profiles', help='List / manage player profiles')
    prof_sub = prof_parser.add_subparsers(dest='action')
    prof_sub.add_parser('list', help='List profiles')
    p_add = prof_sub.add_parser('add', help='Create a profile')
    p_add.add_argument('name')
    p_ren = prof_sub.add_parser('rename', help='Rename a profile')
    p_ren.add_argument('name', help='Current name or id')
    p_ren.add_argument('new_name')
    p_rm = prof_sub.add_parser('remove', help='Remove a profile (saves are kept)')
    p_rm.add_argument('name', help='Name or id')
    p_rm.add_argument('--delete-saves', action='store_true',
                      help="Also delete the profile's saves for every game")
    prof_parser.set_defaults(func=cmd_profiles)

    # hub command
    hub_parser = subparsers.add_parser(
        'hub', help='Get handlers from the handler hub ($JANUS_HUB_URL)')
    hub_sub = hub_parser.add_subparsers(dest='action')
    h_search = hub_sub.add_parser('search', help='List the hub\'s handlers (matching TEXT), '
                                  'most installed first')
    h_search.add_argument('text', nargs='*')
    h_search.add_argument('--sort', choices=['popular', 'uploaded', 'added', 'name'], default='popular')
    h_search.add_argument('--mine', action='store_true',
                          help='Only games in your Steam library or with a hub handler')
    h_get = hub_sub.add_parser('get', help='Install handlers by appid, game id or name')
    h_get.add_argument('games', nargs='*', metavar='APPID|ID|NAME')
    h_get.add_argument('--all', action='store_true',
                       help='Every missing or outdated handler for your Steam library '
                            '(installed or not)')
    h_get.add_argument('--all-installed', action='store_true',
                       help='The same, for installed games only')
    h_get.add_argument('-y', '--yes', action='store_true', help="Don't ask")
    h_upd = hub_sub.add_parser('update', help='Update the handlers installed from the hub')
    h_upd.add_argument('-y', '--yes', action='store_true', help="Don't ask")
    h_rm = hub_sub.add_parser('remove', help='Remove handlers installed from the hub')
    h_rm.add_argument('games', nargs='+', metavar='APPID|ID')
    hub_parser.set_defaults(func=cmd_hub)

    # diagnose command
    diag_parser = subparsers.add_parser('diagnose',
                                        help='Show Steam install + runtime detection')
    diag_parser.add_argument('appid', type=int, nargs='?',
                             help='Specific Steam appid to inspect (omit for overview)')
    diag_parser.set_defaults(func=cmd_diagnose)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    args.func(args)


if __name__ == '__main__':
    main()

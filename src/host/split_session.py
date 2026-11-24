#!/usr/bin/env python3
"""
Split-screen session orchestrator for KWin Wayland ("Path A").

Wraps a :class:`wayland_session.WaylandSession` (scale -> 1.0, panel hide, KWin
window rules) around N gamescope + game instances, each pinned to its tile via a
unique ``SDL_VIDEO_X11_WMCLASS`` (``janus-p<N>``) that the rules match. The
session restores the desktop (scale, panel, rules) on normal exit, Ctrl-C, or
crash.

Tile sizes/positions are given in **native pixels** because the session forces
the output to scale 1.0 (logical == physical), so there is no scale arithmetic.

CLI entry point: ``main.py steam-split`` (see ``cmd_steam_split``).
"""

import os
import shlex
import time
import subprocess
from dataclasses import dataclass
from typing import List, Optional

import wayland_session as ws
from steam import launch_steam_game
import profiles
import steam_input
from controllers import list_controllers
from pad_matcher import PadMatcher
import controller_input as im
from janus_procs import kill_janus_processes


@dataclass
class TileSpec:
    """One instance: a controller assigned to a screen rectangle (native px)."""
    controller: int
    x: int
    y: int
    w: int
    h: int
    profile: Optional[str] = None   # player profile id/name (profiles.py)


# gamescope -F values. linear = soft; fsr/nis = sharpened (take --sharpness);
# nearest/pixel = crisp pixels (pixel art: use an integer factor, e.g. 50%).
UPSCALE_FILTERS = ('linear', 'nearest', 'fsr', 'nis', 'pixel')


def render_size(w: int, h: int, render_scale: int = 100):
    """The resolution the game renders at: *render_scale* % of its tile."""
    s = max(10, min(100, int(render_scale))) / 100
    return max(1, round(w * s)), max(1, round(h * s))


def gamescope_size_args(w: int, h: int, render_scale: int = 100,
                        upscale_filter: str = 'linear',
                        sharpness: int = 2, nested=None) -> List[str]:
    """gamescope size args for a w x h tile: output (-W/-H) = the tile, game
    (-w/-h) = render_scale % of it, upscaled with *upscale_filter*. Below 100%
    games with pixel-sized UI get a bigger UI; games whose UI scales with the
    resolution only get faster (and softer). See NOTES.md "Render scale".
    *nested* (w, h): gamescope's screen instead (a game resized as its tile
    re-flows, resize.py: monitor-sized, its window is the render size)."""
    rw, rh = render_size(w, h, render_scale)
    nw, nh = nested or (rw, rh)
    args = ['-W', str(w), '-H', str(h), '-w', str(nw), '-h', str(nh)]
    if (rw, rh) != (w, h):
        args += ['-F', upscale_filter]
        if upscale_filter in ('fsr', 'nis'):
            args += ['--sharpness', str(int(sharpness))]
    return args


def launch_split_session(
    appid: int,
    tiles: List[TileSpec],
    output: Optional[str] = None,
    fps_limit: int = 60,
    isolation: str = 'secondary',
    verbose: bool = False,
    nvidia: bool = False,
    copy_prefix: bool = False,
    copy_userdata: bool = False,
    goldberg: bool = False,
    goldberg_no_overlay: bool = True,
    goldberg_disable_lan: bool = True,
    goldberg_debug: bool = False,
    exe: Optional[str] = None,
    launch_command: Optional[str] = None,
    extra_args: Optional[List[str]] = None,
    steam_input_filter: bool = False,
    stagger: float = 3.0,
    settle: float = 1.5,
    script=None,
    keep_above: bool = False,
    render_scale: int = 100,
    upscale_filter: str = 'linear',
    sharpness: int = 2,
    input_mode: str = 'steam',
) -> None:
    if not ws.is_kwin_wayland():
        raise RuntimeError("steam-split (Path A) requires a KWin Wayland session")
    if script is not None and script.has('staggerSeconds'):
        stagger = float(script.get('staggerSeconds'))
    import perf
    rate = perf.gamescope_rate(fps_limit)
    if not tiles:
        raise RuntimeError("No tiles given (use --tile controller:x,y,w,h)")

    # Resolve each tile's controller index to its HARDWARE IDENTITY (VID:PID/name) so the
    # hook binds by identity, not the bare kernel js index. js indices are fragile — Steam
    # interleaves its virtual pads (js2/js3/...), so "controller 2" may be a virtual pad,
    # not the 3rd controller. Binding by VID:PID is stable across that shifting, and lets us
    # fail loudly on a bad index instead of silently binding the wrong (virtual) device.
    # Who plays each tile; fails before anything starts if a profile is
    # unknown or used twice.
    session_profiles = profiles.resolve_session(
        [(t.profile, i + 1) for i, t in enumerate(tiles)])
    players = [{'instance': i + 1, 'profile': p.ctx()}
               for i, p in enumerate(session_profiles)]

    controllers = list_controllers()
    ctrl_by_index = {c.index: c for c in controllers}
    for t in tiles:
        if t.controller not in ctrl_by_index:
            avail = ', '.join(str(c.index) for c in controllers) or '(none)'
            raise RuntimeError(
                f"No controller at index {t.controller}. Connected controller indices: "
                f"{avail}. (Steam's virtual pads occupy other js indices — run "
                f"'python3 src/host/main.py list' to see the real ones.)")

    outputs = ws.get_outputs()
    if not outputs:
        raise RuntimeError("No outputs detected via kscreen-doctor")
    if output:
        target = next((o for o in outputs if o.name == output), None)
        if target is None:
            raise RuntimeError(f"Output '{output}' not found "
                               f"(have: {', '.join(o.name for o in outputs)})")
    else:
        target = next((o for o in outputs if o.primary), outputs[0])

    # One KWin rule per instance. Positions are absolute (output origin + tile
    # offset); native px since the session pins the output to scale 1.0.
    rule_tiles = [
        ws.Tile(f'janus-p{i + 1}', target.pos_x + t.x, target.pos_y + t.y)
        for i, t in enumerate(tiles)
    ]

    print(f"[janus] Wayland split: {len(tiles)} tile(s) on {target.name} "
          f"({target.width}x{target.height} @ scale 1.0)")

    procs: List[subprocess.Popen] = []
    # Steam applies the game's Steam Input layout whatever has focus
    # (steam_input.force_layout).
    steam_input.force_layout(appid, script)
    matcher = None
    daemon = None
    try:
        if input_mode == im.JANUS:
            # One virtual Xbox pad per player (padd.py); each instance sees
            # only its own (private /dev).
            daemon = im.start_daemon(
                appid, [(i + 1, t.controller) for i, t in enumerate(tiles)], verbose)
        with ws.WaylandSession(rule_tiles, scale_outputs=[target.name],
                               keep_above=keep_above):
            # Let kscreen apply the scale change before gamescope queries the output.
            if settle:
                time.sleep(settle)

            def _terminate_started():
                for p in procs:
                    try:
                        p.terminate()
                    except Exception:
                        pass
                for p in procs:
                    try:
                        p.wait(timeout=10)
                    except Exception:
                        pass
                    if getattr(p, 'janus_cleanup', None):
                        p.janus_cleanup()    # a launcher's game (nonsteam.py)

            try:
                for i, t in enumerate(tiles):
                    wmclass = f'janus-p{i + 1}'
                    # Backend override (JANUS_GAMESCOPE_BACKEND, default sdl) + extra args
                    # (JANUS_GAMESCOPE_EXTRA, space-separated) for experimenting with the
                    # NVIDIA DRM-format-modifier crash on native-GL games. See NOTES.md.
                    backend = os.environ.get('JANUS_GAMESCOPE_BACKEND', 'sdl')
                    rw, rh = render_size(t.w, t.h, render_scale)
                    gs_args = [
                        '--backend', backend,
                        *gamescope_size_args(t.w, t.h, render_scale,
                                             upscale_filter, sharpness),
                        '-r', str(rate), '-o', str(rate),   # -o == -r: no unfocused throttle
                        '-b',
                    ]
                    extra_gs = os.environ.get('JANUS_GAMESCOPE_EXTRA', '').split()
                    if extra_gs:
                        gs_args.extend(extra_gs)
                    if script is not None and script.get('extraGamescopeArgs'):
                        gs_args.extend(shlex.split(script.get('extraGamescopeArgs')))
                    ci = ctrl_by_index[t.controller]
                    print(f"[janus]   instance {i + 1}: controller={t.controller} "
                          f"[{ci.vendor_id:04x}:{ci.product_id:04x} {ci.name}] "
                          f"wmclass={wmclass} tile={t.w}x{t.h}@{t.x},{t.y}"
                          + (f" render={rw}x{rh}" if (rw, rh) != (t.w, t.h) else ''))
                    try:
                        proc = launch_steam_game(
                            appid=appid,
                        controller=t.controller,
                        controller_vid=ci.vendor_id,
                        controller_pid=ci.product_id,
                        controller_name=ci.name,
                        instance=i + 1,
                        isolation=isolation,
                        verbose=verbose,
                        extra_args=extra_args,
                        gamescope_args=gs_args,
                        nvidia_prime=nvidia,
                        use_runtime=(
                            False if os.environ.get('JANUS_NO_RUNTIME')
                            else bool(script.get('useRuntime', True))
                            if script is not None else True),
                        copy_prefix=copy_prefix,
                        copy_userdata=copy_userdata,
                        goldberg_enabled=goldberg,
                        goldberg_no_overlay=goldberg_no_overlay,
                        goldberg_disable_lan=goldberg_disable_lan,
                        goldberg_debug=goldberg_debug,
                        exe_name=exe,
                        launch_command=launch_command,
                        wmclass=wmclass,
                        steam_input_filter=steam_input_filter,
                        script=script,
                        script_ctx={'width': rw, 'height': rh,
                                    'posX': t.x, 'posY': t.y,
                                    'numPlayers': len(tiles),
                                    'players': players},
                        profile=session_profiles[i].id,
                        input_nodes=daemon.nodes[i + 1] if daemon else None,
                        fps_limit=fps_limit,
                        )
                    except Exception:
                        # Don't strand already-launched instances (they'd keep
                        # running on a restored desktop with script-edited files
                        # about to be rolled back by finish()).
                        print(f"[janus] instance {i + 1} failed to launch — "
                              f"stopping the {len(procs)} already running",
                              flush=True)
                        _terminate_started()
                        raise
                    procs.append(proc)
                    if i < len(tiles) - 1 and stagger:
                        time.sleep(stagger)

                print(f"[janus] All {len(procs)} instance(s) launched. "
                      f"Ctrl-C to stop and restore the desktop.")
                # Identical pads -> their Steam virtual pads, one-to-one (pad_matcher.py)
                if daemon is None:
                    matcher = PadMatcher(
                        appid, [(i + 1, t.controller) for i, t in enumerate(tiles)],
                        verbose=verbose).start()
                from focus_release import release_soon
                release_soon()   # no tile holds the mouse until clicked
                if script is not None:
                    script.invoke('onAllStarted',
                                  ctx={'appid': appid, 'numPlayers': len(procs),
                                       'pids': [p.pid for p in procs]},
                                  soft=True)
                for p in procs:
                    rc = p.wait()
                    if script is not None:
                        sctx = getattr(p, 'janus_script_ctx', None) or {}
                        script.invoke('onInstanceExit',
                                      ctx={**sctx, 'exitCode': rc}, soft=True)
            except KeyboardInterrupt:
                # Ctrl-C, or SIGTERM (WaylandSession turns it into this) — also
                # in the middle of the staggered launch.
                print("\n[janus] Stopping all instances...", flush=True)
                _terminate_started()
                killed = kill_janus_processes()
                if killed:
                    print(f"[janus] force-killed {killed} leftover process(es)", flush=True)
    finally:
        if daemon is not None:
            daemon.stop()
        steam_input.release_layout(wait=True)
        if matcher is not None:
            matcher.stop()
        if script is not None:
            script.invoke('onStop',
                          ctx={'appid': appid, 'numPlayers': len(tiles)},
                          soft=True)
            script.finish()
    # WaylandSession.__exit__ has restored scale / panel / rules by here.
    print("[janus] Janus session ended; desktop restored.")

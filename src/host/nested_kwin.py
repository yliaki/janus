#!/usr/bin/env python3
"""
Nested KWin for SteamOS Game Mode (and other hosts without KWin Wayland).

In Game Mode, Steam's gamescope is the system compositor: it shows one app at a
time, full screen, and can't tile. Janus's tile placement is KWin window rules
(wayland_session.py), so in Game Mode Janus runs inside its OWN KWin:

    Game Mode gamescope (Steam's)
     └─ Janus shortcut (run_bigpicture.sh → main.py)
         └─ dbus-run-session -- kwin_wayland --x11-display $DISPLAY …   (this module)
             └─ python3 -m src.host.nested_kwin child -- <Janus again>
                 ├─ Big Picture UI (Wayland client of the nested KWin)
                 └─ gamescope tiles (placed by the usual KWin rules)

Gamescope shows the nested KWin's X11 window because it's in the shortcut's
process tree (gamescope maps windows to app ids by the `reaper SteamLaunch
AppId=` ancestor).

The nested KWin gets a private D-Bus session (so it never fights a host KWin for
org.kde.KWin) and a private config dir (its kwinrc/kwinrulesrc never touch the
user's desktop ones). Janus itself and the games keep the REAL session bus and
config dir; the KWin helpers in wayland_session.py reach the nested KWin
through JANUS_KWIN_DBUS / JANUS_KWIN_CONFIG.

Environment set for the child:
    JANUS_NESTED_KWIN=1        we're inside the nested KWin
    JANUS_NESTED_SIZE=WxH      its single output (wayland_session.get_outputs)
    JANUS_KWIN_DBUS=<addr>     the private bus KWin is on
    JANUS_KWIN_CONFIG=<dir>    KWin's config dir (kwinrulesrc goes here)

`JANUS_NO_NESTED_KWIN=1` disables the wrap (diagnostics). `JANUS_KWIN_BIN=<path>`
runs another kwin_wayland binary (diagnostics: a copy without the cap_sys_nice
file capability can be traced and dumps core).
"""

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

# Steam's Game Mode env aimed at games that run directly under its gamescope.
# A nested compositor (KWin) and the per-tile gamescopes must not see it: an
# inner gamescope that inherits ENABLE_GAMESCOPE_WSI/GAMESCOPE_WAYLAND_DISPLAY
# fails with "Creating swapchain for non-Gamescope swapchain" (NOTES.md "Nested
# gamescope"), and games in the tiles would talk to the outer gamescope.
GAMESCOPE_SESSION_VARS = (
    'ENABLE_GAMESCOPE_WSI', 'GAMESCOPE_WAYLAND_DISPLAY', 'GAMESCOPE_LIMITER_FILE',
    'XWAYLAND_FORCE_ENABLE_EXTRA_MODES', 'STEAM_GAME_DISPLAY_0',
    'STEAM_GAMESCOPE_VRR_SUPPORTED', 'STEAM_GAMESCOPE_NIS_SUPPORTED',
    'STEAM_GAMESCOPE_DYNAMIC_FPSLIMITER', 'STEAM_GAMESCOPE_HDR_SUPPORTED',
    'STEAM_GAMESCOPE_TEARING_SUPPORTED', 'STEAM_GAMESCOPE_HAS_TEARING_SUPPORT',
    'STEAM_GAMESCOPE_FANCY_SCALING_SUPPORT', 'STEAM_GAMESCOPE_COLOR_MANAGED',
    'STEAM_GAMESCOPE_VIRTUAL_WHITE', 'STEAM_MANGOAPP_PRESETS_SUPPORTED',
    'STEAM_MANGOAPP_HORIZONTAL_SUPPORTED', 'STEAM_USE_MANGOAPP',
    'STEAM_USE_DYNAMIC_VRS', 'STEAM_DISABLE_MANGOAPP_ATOM_WORKAROUND',
    'STEAM_MULTIPLE_XWAYLANDS', 'STEAM_GAMESCOPE_DYNAMIC_REFRESH_IN_STEAM_SUPPORTED',
    'GAMESCOPECTRL_DEBUG_HDR', 'SRT_URLOPEN_PREFER_STEAM',
)

# KWin config for the nested session: no animations/effects (they cost GPU on a
# Deck and delay tiles), no focus stealing prevention (a tile must take the
# keyboard when it maps), no screen edges / hot corners, one desktop.
_KWINRC = """\
[Compositing]
AnimationSpeed=0
LatencyPolicy=Low

[Effect-overview]
BorderActivate=9

[ElectricBorders]
Bottom=None
BottomLeft=None
BottomRight=None
Left=None
Right=None
Top=None
TopLeft=None
TopRight=None

[Plugins]
blurEnabled=false
contrastEnabled=false
fadingpopupsEnabled=false
kwin4_effect_fadeEnabled=false
kwin4_effect_maximizeEnabled=false
kwin4_effect_scaleEnabled=false
kwin4_effect_squashEnabled=false
slidingpopupsEnabled=false
windowapertureEnabled=false
overviewEnabled=false
screenedgeEnabled=false
zoomEnabled=false

[TabBox]
LayoutName=compact

[Windows]
FocusStealingPreventionLevel=0
Placement=Centered

[Desktops]
Number=1
Rows=1
"""


def active() -> bool:
    """True inside the nested KWin (the child process and everything it starts)."""
    return os.environ.get('JANUS_NESTED_KWIN') == '1'


def in_gamescope_session() -> bool:
    """True when the host compositor is a gamescope session (SteamOS Game Mode,
    or `gamescope -e … steam -gamepadui` on a desktop): no KWin to place tiles."""
    if active():
        return False
    if os.environ.get('XDG_CURRENT_DESKTOP', '').lower() == 'gamescope':
        return True
    return bool(os.environ.get('GAMESCOPE_WAYLAND_DISPLAY')) and \
        not os.environ.get('WAYLAND_DISPLAY')


def should_wrap() -> bool:
    return (in_gamescope_session() and os.environ.get('JANUS_NO_NESTED_KWIN') != '1'
            and bool(os.environ.get('DISPLAY')) and kwin_problem() is None)


def kwin_problem() -> Optional[str]:
    """Why the nested KWin can't run here, or None."""
    for tool in ('kwin_wayland', 'dbus-run-session'):
        if shutil.which(tool) is None:
            return f'{tool} is not installed'
    return None


def nested_size() -> Optional[Tuple[int, int]]:
    """The nested KWin's output size (inside it), from JANUS_NESTED_SIZE."""
    v = os.environ.get('JANUS_NESTED_SIZE', '')
    try:
        w, h = (int(x) for x in v.lower().split('x'))
        return (w, h) if w > 0 and h > 0 else None
    except ValueError:
        return None


def screen_size(display: Optional[str] = None) -> Tuple[int, int]:
    """Size of the host X screen (gamescope's Xwayland = the Game Mode output).
    `JANUS_NESTED_SIZE=WxH` overrides; falls back to 1280x800 (Steam Deck)."""
    forced = nested_size()
    if forced:
        return forced
    try:
        from Xlib import display as xdisplay
        d = xdisplay.Display(display or os.environ.get('DISPLAY'))
        try:
            s = d.screen()
            return int(s.width_in_pixels), int(s.height_in_pixels)
        finally:
            d.close()
    except Exception:
        pass
    try:
        r = subprocess.run(['xdpyinfo'], capture_output=True, text=True, timeout=5)
        for line in r.stdout.splitlines():
            if 'dimensions:' in line:
                w, h = line.split()[1].split('x')
                return int(w), int(h)
    except Exception:
        pass
    return 1280, 800


def config_dir() -> Path:
    """Private XDG_CONFIG_HOME for the nested KWin (kwinrc, kwinrulesrc)."""
    base = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local' / 'share')
    d = base / 'janus' / 'run' / 'kwin-nested'
    d.mkdir(parents=True, exist_ok=True)
    (d / 'kwinrc').write_text(_KWINRC)
    rules = d / 'kwinrulesrc'
    if rules.exists():
        rules.unlink()          # stale rules from a crashed session
    return d


def kwin_command(child_argv: List[str], size: Tuple[int, int],
                 socket: str) -> List[str]:
    w, h = size
    child = [sys.executable, '-m', 'src.host.nested_kwin', 'child', '--', *child_argv]
    return ['dbus-run-session', '--',
            os.environ.get('JANUS_KWIN_BIN') or 'kwin_wayland',
            '--x11-display', os.environ['DISPLAY'],
            '--width', str(w), '--height', str(h),
            '--xwayland', '--socket', socket,
            '--exit-with-session', shlex.join(child)]


def _no_new_privs() -> None:
    """Keep the nested KWin off real-time scheduling.

    kwin_wayland ships with the cap_sys_nice file capability and puts its main
    thread on SCHED_RR with RLIMIT_RTTIME = 200 ms. Nested in Game Mode it can
    burn 200 ms without blocking (a Proton game loading: Lethal Company, every
    time, ~15 s in), and the kernel then SIGKILLs it: no message, no core (a
    process with file caps isn't dumpable), every tile and Janus gone. Its
    scheduling can't be lowered from outside afterwards (EPERM: it holds a cap
    we don't) and it has no switch for it, so don't let exec grant the cap:
    PR_SET_NO_NEW_PRIVS. Inherited by everything in the session, which needs no
    setuid/file-cap binary (bwrap and pressure-vessel use user namespaces; the
    tile gamescopes just log "No CAP_SYS_NICE")."""
    import ctypes
    PR_SET_NO_NEW_PRIVS = 38
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            print(f'[janus] nested KWin: PR_SET_NO_NEW_PRIVS failed '
                  f'(errno {ctypes.get_errno()}); it may run real-time', flush=True)
    except OSError as e:
        print(f'[janus] nested KWin: PR_SET_NO_NEW_PRIVS failed: {e}', flush=True)


def exec_wrapped(child_argv: List[str], cwd: Optional[str] = None) -> None:
    """Replace this process with a nested KWin that runs *child_argv*.
    Never returns (exec), unless exec itself fails (then raises)."""
    size = screen_size()
    cfg = config_dir()
    env = dict(os.environ)
    for k in GAMESCOPE_SESSION_VARS:
        env.pop(k, None)
    env.pop('WAYLAND_DISPLAY', None)
    # What the child restores for itself and the games.
    env['JANUS_ORIG_DBUS'] = env.get('DBUS_SESSION_BUS_ADDRESS', '')
    env['JANUS_ORIG_XDG_CONFIG_HOME'] = env.get('XDG_CONFIG_HOME', '__janus_unset__')
    env['XDG_CONFIG_HOME'] = str(cfg)
    env['JANUS_KWIN_CONFIG'] = str(cfg)
    env['JANUS_NESTED_SIZE'] = f'{size[0]}x{size[1]}'
    env['JANUS_NESTED_KWIN'] = '1'
    socket = f'janus-kwin-{os.getpid()}'
    cmd = kwin_command(child_argv, size, socket)
    _no_new_privs()
    print(f'[janus] gamescope session: running inside a nested KWin '
          f'{size[0]}x{size[1]} ({socket})', flush=True)
    if cwd:
        os.chdir(cwd)
    os.execvpe(cmd[0], cmd, env)


def _child(argv: List[str]) -> None:
    """Runs inside the nested KWin: point the KWin helpers at the private bus,
    give Janus back the real bus + config dir, then exec Janus."""
    env = os.environ
    env['JANUS_KWIN_DBUS'] = env.get('DBUS_SESSION_BUS_ADDRESS', '')
    orig = env.pop('JANUS_ORIG_DBUS', '')
    if orig:
        env['DBUS_SESSION_BUS_ADDRESS'] = orig
    else:
        env.pop('DBUS_SESSION_BUS_ADDRESS', None)
    xch = env.pop('JANUS_ORIG_XDG_CONFIG_HOME', '__janus_unset__')
    if xch == '__janus_unset__':
        env.pop('XDG_CONFIG_HOME', None)
    else:
        env['XDG_CONFIG_HOME'] = xch
    for k in GAMESCOPE_SESSION_VARS:
        env.pop(k, None)
    # We're a Wayland client of the nested KWin now (Qt picks its platform
    # plugin from this; Game Mode said x11).
    env['XDG_SESSION_TYPE'] = 'wayland'
    os.execvp(argv[0], argv)


def main(argv: List[str]) -> int:
    if len(argv) >= 2 and argv[0] == 'child' and argv[1] == '--' and len(argv) > 2:
        _child(argv[2:])
        return 1
    print('usage: nested_kwin.py child -- CMD...  (internal)', file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))

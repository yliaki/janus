#!/usr/bin/env python3
"""Nested KWin for SteamOS Game Mode (src/host/nested_kwin.py) and the
nested-aware bits of wayland_session.py. No KWin is started.

    python3 tests/test_nested_kwin.py
"""
import importlib
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'src' / 'host'))

import nested_kwin  # noqa: E402

GAME_MODE_ENV = {
    'XDG_CURRENT_DESKTOP': 'gamescope', 'XDG_SESSION_TYPE': 'x11', 'DISPLAY': ':1',
    'GAMESCOPE_WAYLAND_DISPLAY': 'gamescope-0', 'ENABLE_GAMESCOPE_WSI': '1',
    'GAMESCOPE_LIMITER_FILE': '/run/user/1000/gamescope-limiter-X',
    'STEAM_GAME_DISPLAY_0': ':1', 'STEAM_GAMESCOPE_HDR_SUPPORTED': '1',
}

failures = 0


def check(name, cond, detail=''):
    global failures
    print(('ok   ' if cond else 'FAIL ') + name + (f'  ({detail})' if detail and not cond else ''))
    if not cond:
        failures += 1


def with_env(env, fn):
    saved = dict(os.environ)
    try:
        for k in ('JANUS_NESTED_KWIN', 'WAYLAND_DISPLAY', 'XDG_CURRENT_DESKTOP',
                  'GAMESCOPE_WAYLAND_DISPLAY', 'JANUS_NO_NESTED_KWIN'):
            os.environ.pop(k, None)
        os.environ.update(env)
        return fn()
    finally:
        os.environ.clear()
        os.environ.update(saved)


# --- session detection -------------------------------------------------------
check('Game Mode is a gamescope session',
      with_env(GAME_MODE_ENV, nested_kwin.in_gamescope_session))
check('gamescope socket without a Wayland display counts too',
      with_env({'GAMESCOPE_WAYLAND_DISPLAY': 'gamescope-0'}, nested_kwin.in_gamescope_session))
check('Plasma Wayland is not',
      not with_env({'XDG_CURRENT_DESKTOP': 'KDE', 'WAYLAND_DISPLAY': 'wayland-0'},
                   nested_kwin.in_gamescope_session))
check('inside the nested KWin we never wrap again',
      not with_env({**GAME_MODE_ENV, 'JANUS_NESTED_KWIN': '1'}, nested_kwin.should_wrap))
check('JANUS_NO_NESTED_KWIN=1 disables the wrap',
      not with_env({**GAME_MODE_ENV, 'JANUS_NO_NESTED_KWIN': '1'}, nested_kwin.should_wrap))

# --- the KWin command -------------------------------------------------------
cmd = with_env(GAME_MODE_ENV, lambda: nested_kwin.kwin_command(
    ['python3', '-m', 'src.bigpicture.main', '--x y'], (1280, 800), 'janus-kwin-1'))
check('private D-Bus session', cmd[:2] == ['dbus-run-session', '--'], cmd)
check('nested on the Game Mode X display', cmd[cmd.index('--x11-display') + 1] == ':1', cmd)
check('output = screen size', cmd[cmd.index('--width') + 1] == '1280'
      and cmd[cmd.index('--height') + 1] == '800', cmd)
session = cmd[cmd.index('--exit-with-session') + 1]
check('session runs the child shim, args quoted',
      'src.host.nested_kwin child --' in session and "'--x y'" in session, session)
check('JANUS_NESTED_SIZE overrides the screen size',
      with_env({'JANUS_NESTED_SIZE': '1920x1080'}, nested_kwin.screen_size) == (1920, 1080))

# --- the child: real bus/config back, KWin's in JANUS_KWIN_* -----------------
env = {k: v for k, v in os.environ.items() if k in ('PATH', 'HOME', 'LANG')}
env.update(GAME_MODE_ENV)
env.update({
    'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/tmp/private-bus',   # set by dbus-run-session
    'JANUS_ORIG_DBUS': 'unix:path=/run/user/1000/bus',
    'XDG_CONFIG_HOME': '/tmp/kwin-cfg',
    'JANUS_ORIG_XDG_CONFIG_HOME': '__janus_unset__',
    'JANUS_NESTED_KWIN': '1', 'WAYLAND_DISPLAY': 'janus-kwin-1',
})
out = subprocess.run([sys.executable, '-m', 'src.host.nested_kwin', 'child', '--', 'env'],
                     cwd=ROOT, env=env, capture_output=True, text=True).stdout
got = dict(line.split('=', 1) for line in out.splitlines() if '=' in line)
check('child: KWin bus in JANUS_KWIN_DBUS', got.get('JANUS_KWIN_DBUS') == 'unix:path=/tmp/private-bus', got.get('JANUS_KWIN_DBUS'))
check('child: real session bus restored', got.get('DBUS_SESSION_BUS_ADDRESS') == 'unix:path=/run/user/1000/bus')
check('child: XDG_CONFIG_HOME unset again (was unset)', 'XDG_CONFIG_HOME' not in got)
check('child: gamescope session vars gone',
      not any(k in got for k in ('ENABLE_GAMESCOPE_WSI', 'GAMESCOPE_WAYLAND_DISPLAY',
                                 'GAMESCOPE_LIMITER_FILE', 'STEAM_GAME_DISPLAY_0',
                                 'STEAM_GAMESCOPE_HDR_SUPPORTED')))
check('child: Qt picks Wayland', got.get('XDG_SESSION_TYPE') == 'wayland')
check('child: still marked nested', got.get('JANUS_NESTED_KWIN') == '1')

env['JANUS_ORIG_XDG_CONFIG_HOME'] = '/home/u/.config'
out = subprocess.run([sys.executable, '-m', 'src.host.nested_kwin', 'child', '--', 'env'],
                     cwd=ROOT, env=env, capture_output=True, text=True).stdout
check('child: XDG_CONFIG_HOME restored when it was set',
      'XDG_CONFIG_HOME=/home/u/.config' in out.splitlines())

# --- wayland_session inside the nested KWin ----------------------------------
def nested_ws():
    import wayland_session
    return importlib.reload(wayland_session)

ws = with_env({'JANUS_NESTED_KWIN': '1', 'JANUS_NESTED_SIZE': '1280x800',
               'JANUS_KWIN_CONFIG': '/tmp/kwin-cfg', 'JANUS_KWIN_DBUS': 'unix:path=/tmp/b'},
              lambda: (nested_ws(),
                       nested_ws().session_kind(),
                       nested_ws().get_outputs(),
                       nested_ws().missing_tools(),
                       nested_ws()._clean_env()['DBUS_SESSION_BUS_ADDRESS'],
                       nested_ws().KWINRULES))
_, kind, outs, missing, bus, rules = ws
check('nested counts as KWin Wayland', kind == 'kwin-wayland', kind)
check('one output, the nested size, scale 1',
      len(outs) == 1 and (outs[0].width, outs[0].height, outs[0].scale) == (1280, 800, 1.0), outs)
check('kscreen-doctor not required nested', 'kscreen-doctor' not in missing, missing)
check('KWin calls go to the private bus', bus == 'unix:path=/tmp/b', bus)
check('rules land in the nested KWin config dir', str(rules) == '/tmp/kwin-cfg/kwinrulesrc', rules)
with_env({}, nested_ws)   # back to the host defaults
js = nested_ws()._NESTED_SESSION_JS % dict(pid=42, running='true')
check('session script fills in', 'launcherPid = 42' in js and 'var running = true' in js)

print('\nall passed' if not failures else f'\n{failures} FAILED')
sys.exit(1 if failures else 0)

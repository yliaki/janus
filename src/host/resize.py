"""Resizing a running game to its tile (dynamic players' re-flow).

gamescope's own screen (-w/-h) can't change while it runs, but gamescope
shows the game's *window* at its own size, scaled to fit the tile. So a game
whose handler says it can be resized (``Game.resizeGame``) is launched with
a monitor-sized gamescope screen and a window the size of its tile; when the
tiles re-flow, the window is resized to the new tile:

  "window"  Janus resizes the game's X window inside gamescope (xdotool on
            the nested display). Native games that follow a resized window
            (SDL/FNA: Terraria).
  "unity"   the JanusUiScale BepInEx plugin calls Screen.SetResolution with
            the size in $JANUS_SIZE_FILE (Proton Unity Mono games; Wine
            ignores resizes from outside).
  "unreal"  the JanusUiFit UE4SS Lua mod runs "r.SetRes WxHw" (Proton
            Unreal games; UE4SS installed even without the UI fit).
  "file"    only $JANUS_SIZE_FILE: the handler's own mod reads it.

$JANUS_SIZE_FILE (every instance has one, run/<game>/i<N>.size) holds
"WxH", the size the game should render at. NOTES.md "Dynamic players".
"""

import os
import re
import subprocess
from pathlib import Path
from typing import Optional

RUN_ROOT = Path.home() / '.local' / 'share' / 'janus' / 'run'
MODES = ('window', 'unity', 'unreal', 'file')


def mode(script) -> Optional[str]:
    """The handler's Game.resizeGame, or None (the game keeps its size and
    is scaled into its new tile)."""
    if script is None:
        return None
    m = script.get('resizeGame')
    return m if m in MODES else None


def size_file(game_key, instance: int) -> Path:
    return RUN_ROOT / str(game_key) / f'i{int(instance)}.size'


def write_size(game_key, instance: int, w: int, h: int) -> None:
    p = size_file(game_key, instance)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.tmp')
    tmp.write_text(f'{int(w)}x{int(h)}\n')
    os.replace(tmp, p)


def nested_display(wmclass: str) -> Optional[str]:
    """The X display inside the instance's gamescope (':N'): the DISPLAY of a
    process of that instance (environment marks: the hook preload and
    SDL_VIDEO_X11_WMCLASS=<wmclass>) other than the host's."""
    tile = f'SDL_VIDEO_X11_WMCLASS={wmclass}\0'.encode()
    host = os.environ.get('DISPLAY', '')
    for entry in os.listdir('/proc'):
        if not entry.isdigit():
            continue
        try:
            with open(f'/proc/{entry}/environ', 'rb') as f:
                env = f.read()
        except OSError:
            continue
        if b'libjanus' not in env or tile not in env:
            continue
        m = re.search(rb'(?:^|\0)DISPLAY=([^\0]*)', env)
        if m and m.group(1).decode() not in ('', host):
            return m.group(1).decode()
    return None


def _instance_env(wmclass: str, key: str, accept) -> Optional[str]:
    """*key* from the environment of a process of the instance (marks: the
    hook preload and SDL_VIDEO_X11_WMCLASS=<wmclass>) whose value *accept*s."""
    tile = f'SDL_VIDEO_X11_WMCLASS={wmclass}\0'.encode()
    pat = re.compile(rb'(?:^|\0)' + re.escape(key.encode()) + rb'=([^\0]*)')
    for entry in os.listdir('/proc'):
        if not entry.isdigit():
            continue
        try:
            with open(f'/proc/{entry}/environ', 'rb') as f:
                env = f.read()
        except OSError:
            continue
        if b'libjanus' not in env or tile not in env:
            continue
        m = pat.search(env)
        if m and accept(m.group(1).decode()):
            return m.group(1).decode()
    return None


def gamescope_socket(wmclass: str) -> Optional[str]:
    """The instance's gamescope Wayland socket name ('gamescope-N', in
    $XDG_RUNTIME_DIR): its libei input socket is '<name>-ei', and
    gamescopectl reaches it with GAMESCOPE_WAYLAND_DISPLAY=<name>. Read on
    the host side (inside the runtime container it's a path)."""
    return _instance_env(wmclass, 'GAMESCOPE_WAYLAND_DISPLAY',
                         lambda v: bool(v) and '/' not in v)


def _top_windows(display: str):
    """[(id, w, h)] of the mapped top-level windows on *display*."""
    env = dict(os.environ, DISPLAY=display)
    try:
        out = subprocess.run(['xwininfo', '-root', '-children'], env=env,
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    wins = []
    for line in out.splitlines():
        m = re.match(r'\s+(0x[0-9a-f]+) .*?\s(\d+)x(\d+)\+-?\d+\+-?\d+', line)
        if not m:
            continue
        wid, w, h = m.group(1), int(m.group(2)), int(m.group(3))
        try:
            st = subprocess.run(['xwininfo', '-id', wid], env=env, capture_output=True,
                                text=True, timeout=5).stdout
        except (OSError, subprocess.TimeoutExpired):
            continue
        if 'IsViewable' in st and w > 1 and h > 1:
            wins.append((wid, w, h))
    return wins


def resize_window(display: str, w: int, h: int) -> bool:
    """Resize the game's window (the biggest mapped top-level window) on the
    nested *display* to w x h. False when there is none."""
    wins = _top_windows(display)
    if not wins:
        return False
    wid = max(wins, key=lambda t: t[1] * t[2])[0]
    env = dict(os.environ, DISPLAY=display)
    try:
        r = subprocess.run(['xdotool', 'windowsize', wid, str(int(w)), str(int(h))],
                           env=env, capture_output=True, timeout=5)
        subprocess.run(['xdotool', 'windowmove', wid, '0', '0'],
                       env=env, capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def read_size(game_key, instance: int):
    """(w, h) from the instance's size file, or None."""
    try:
        w, h = size_file(game_key, instance).read_text().strip().split('x')
        return int(w), int(h)
    except (OSError, ValueError):
        return None


class WindowKeeper:
    """Game.resizeGame = "window": keeps one instance's game window at the
    size in its size file, for as long as the instance runs. Checked every
    2 s, so a game that starts slowly (Necesse, Java) or opens its window at
    the screen's size is caught too; 5 tries per size, then it is left alone
    (a game with a minimum size: Terraria won't go below 800 wide)."""

    def __init__(self, game_key, instance: int, wmclass: str, log=print):
        import threading
        self.key, self.instance, self.wmclass, self.log = game_key, instance, wmclass, log
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f'resize-{wmclass}')
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        display, target, tries = None, None, 0
        while not self._stop.wait(2.0):
            want = read_size(self.key, self.instance)
            if want is None:
                continue
            if want != target:
                target, tries = want, 0
            if tries >= 5:
                continue
            display = display or nested_display(self.wmclass)
            if not display:
                continue
            wins = _top_windows(display)
            if not wins:
                continue
            _, w, h = max(wins, key=lambda t: t[1] * t[2])
            if (w, h) == target:
                tries = 0
                continue
            tries += 1
            if resize_window(display, *target):
                self.log(f'[launcher] P{self.instance}: window {w}x{h} -> '
                         f'{target[0]}x{target[1]} (display {display})')

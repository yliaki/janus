#!/usr/bin/env python3
"""
Wayland (KDE Plasma 6 / KWin) session window management for Janus — "Path A".

On a native-Wayland KWin host the X11 tools (xdotool/wmctrl/xprop) can't see or
move gamescope windows, so window management goes through KWin + plasmashell.
This module implements the validated "Path A" split-screen setup:

  1. flip the affected output(s) to **scale 1.0** so logical == physical and
     gamescope's SDL-backend windows render crisp with no KWin upscaling,
  2. hide the Plasma panel (also releases its strut so tiles get full height),
  3. write KWin **window rules** — one per instance, matched by a unique
     ``SDL_VIDEO_WAYLAND_WMCLASS`` — that force position / noborder / above,
  4. **restore everything** (scale, panel, rules) on normal exit, exception,
     Ctrl-C, or SIGTERM.

Per-instance window identity comes from launching each gamescope with
``--backend sdl`` and ``SDL_VIDEO_WAYLAND_WMCLASS=janus-p<N>`` (the wayland
backend hardcodes app_id "gamescope", so rules can't tell instances apart).

See WAYLAND.md for the empirical background behind every choice here.
"""

import atexit
import json
import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

# Inside Janus's nested KWin (SteamOS Game Mode, nested_kwin.py) KWin has its
# own config dir and D-Bus session; everything here talks to that KWin.
KWINRULES = (Path(os.environ['JANUS_KWIN_CONFIG']) if os.environ.get('JANUS_KWIN_CONFIG')
             else Path.home() / '.config') / 'kwinrulesrc'

# KWin rule "apply" enum: 2=Apply Initially, 3=Remember, 4=Force, 5=Apply Now.
_FORCE = 4
# KWin string-match enum: 0=unimportant, 1=exact, 2=substring, 3=regex.
_EXACT = 1


# ---------------------------------------------------------------------------
# Environment detection
# ---------------------------------------------------------------------------

def is_nested() -> bool:
    """True inside Janus's own nested KWin (see nested_kwin.py)."""
    return os.environ.get('JANUS_NESTED_KWIN') == '1'


def is_wayland() -> bool:
    """True if the host session is Wayland."""
    if is_nested():
        return True
    if os.environ.get('XDG_SESSION_TYPE', '').lower() == 'wayland':
        return True
    return bool(os.environ.get('WAYLAND_DISPLAY'))


def is_kwin() -> bool:
    """True if KWin is the running compositor (KDE Plasma)."""
    if is_nested():
        return True
    if 'KDE' in os.environ.get('XDG_CURRENT_DESKTOP', ''):
        return True
    try:
        r = subprocess.run(['qdbus6', 'org.kde.KWin'],
                           capture_output=True, timeout=3, env=_clean_env())
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def is_kwin_wayland() -> bool:
    return is_wayland() and is_kwin()


def session_kind() -> str:
    """Classify the host session: 'kwin-wayland' | 'wayland-other' | 'x11'."""
    if not is_wayland():
        return 'x11'
    return 'kwin-wayland' if is_kwin() else 'wayland-other'


def missing_tools() -> List[str]:
    """Required CLIs for the Wayland ('Path A') flow that aren't on PATH."""
    import shutil
    tools = ('qdbus6',) if is_nested() else ('kscreen-doctor', 'qdbus6')
    return [t for t in tools if shutil.which(t) is None]


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _clean_env() -> dict:
    """Environment for KDE CLI helpers, without an inherited QT_QPA_PLATFORM.

    `kscreen-doctor` is a Qt app; if it inherits e.g. QT_QPA_PLATFORM=offscreen
    (which a GUI/test process may set) it can't see the real display and reports
    a wrong scale (1.0). Dropping it makes it auto-detect the real session.
    """
    env = dict(os.environ)
    env.pop('QT_QPA_PLATFORM', None)
    # Nested KWin: it's on its own private session bus.
    if os.environ.get('JANUS_KWIN_DBUS'):
        env['DBUS_SESSION_BUS_ADDRESS'] = os.environ['JANUS_KWIN_DBUS']
    return env


def _kscreen(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(['kscreen-doctor', *args],
                          capture_output=True, text=True, timeout=10,
                          env=_clean_env())


def _kwin_reconfigure() -> None:
    subprocess.run(['qdbus6', 'org.kde.KWin', '/KWin',
                    'org.kde.KWin.reconfigure'],
                   capture_output=True, timeout=5, env=_clean_env())


def _plasma_eval(script: str) -> str:
    """Run a plasmashell scripting snippet; returns its print() buffer."""
    r = subprocess.run(
        ['qdbus6', 'org.kde.plasmashell', '/PlasmaShell',
         'org.kde.PlasmaShell.evaluateScript', script],
        capture_output=True, text=True, timeout=8, env=_clean_env())
    return (r.stdout or '').strip()


def kwin_run_script(js: str, name: str = 'janus-oneshot') -> bool:
    """Run a one-shot KWin script (load, run, unload). True if KWin ran it.

    Unloading matters: a loaded script stays registered in KWin until
    unloaded, and loading another under the same plugin name fails."""
    import tempfile
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False) as f:
        f.write(js)
        path = f.name
    qdbus = ['qdbus6', 'org.kde.KWin', '/Scripting']
    try:
        subprocess.run([*qdbus, 'org.kde.kwin.Scripting.unloadScript', name],
                       capture_output=True, timeout=3, env=_clean_env())
        r = subprocess.run([*qdbus, 'org.kde.kwin.Scripting.loadScript', path, name],
                           capture_output=True, text=True, timeout=3,
                           env=_clean_env())
        num = (r.stdout or '').strip()
        if r.returncode != 0 or not num.lstrip('-').isdigit() or int(num) < 0:
            return False
        subprocess.run(['qdbus6', 'org.kde.KWin', f'/Scripting/Script{num}',
                        'org.kde.kwin.Script.run'],
                       capture_output=True, timeout=3, env=_clean_env())
        subprocess.run([*qdbus, 'org.kde.kwin.Scripting.unloadScript', name],
                       capture_output=True, timeout=3, env=_clean_env())
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    finally:
        os.unlink(path)


def kwin_load_script(js: str, name: str) -> bool:
    """Load and run a KWin script that STAYS loaded (signal handlers keep
    working) until kwin_unload_script(name)."""
    import tempfile
    d = Path(tempfile.gettempdir()) / f'janus-kwin-{os.getuid()}'
    d.mkdir(exist_ok=True)
    path = d / f'{name}.js'
    path.write_text(js)
    qdbus = ['qdbus6', 'org.kde.KWin', '/Scripting']
    try:
        subprocess.run([*qdbus, 'org.kde.kwin.Scripting.unloadScript', name],
                       capture_output=True, timeout=3, env=_clean_env())
        r = subprocess.run([*qdbus, 'org.kde.kwin.Scripting.loadScript', str(path), name],
                           capture_output=True, text=True, timeout=3, env=_clean_env())
        num = (r.stdout or '').strip()
        if r.returncode != 0 or not num.lstrip('-').isdigit() or int(num) < 0:
            return False
        subprocess.run(['qdbus6', 'org.kde.KWin', f'/Scripting/Script{num}',
                        'org.kde.kwin.Script.run'],
                       capture_output=True, timeout=3, env=_clean_env())
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def kwin_unload_script(name: str) -> None:
    try:
        subprocess.run(['qdbus6', 'org.kde.KWin', '/Scripting',
                        'org.kde.kwin.Scripting.unloadScript', name],
                       capture_output=True, timeout=3, env=_clean_env())
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


# Nested KWin (Game Mode): the launcher is a full-screen window, and KWin keeps
# an ACTIVE full-screen window above normal ones, so the tiles opened under it.
# While the session runs, the launcher leaves full screen and is kept below the
# tiles, and it never keeps the focus: whenever it becomes active (at the start,
# when its full-screen change lands, when clicked in a gap between tiles) the
# last active tile is activated again, so keyboard input reaches a game.
# KWin 6.3 refuses to activate another window while the active one is kept
# below or full screen, hence keepBelow is lifted around each activation, and it
# ignores activation of a tile that is still mapping, hence the retries on later
# events. When the games end the restore run lifts keep-below; Main.qml's
# restore() puts the launcher back in full screen and activates it.
_NESTED_SESSION_JS = """\
var launcherPid = %(pid)d;
var running = %(running)s;
var launcher = null;
var lastTile = null;
function isTile(w) { return ("" + w.resourceClass).indexOf("janus-p") === 0; }
function firstTile() {
    var ws = workspace.stackingOrder;
    for (var i = ws.length - 1; i >= 0; i--)
        if (isTile(ws[i])) return ws[i];
    return null;
}
var busy = false;     // activating fires windowActivated: don't re-enter
function focusTile() {
    if (!running || busy) return;
    var act = workspace.activeWindow;
    if (act && isTile(act)) { lastTile = act; return; }
    // Only take the focus away from the launcher. Not from a dialog, and not
    // when nothing is active: that's the instant between two tiles (KWin
    // activating a new tile), and "fixing" it ping-pongs the focus forever.
    if (!act || act !== launcher) return;
    var t = lastTile || firstTile();
    if (!t) return;
    busy = true;
    if (launcher) {
        if (launcher.fullScreen) launcher.fullScreen = false;
        launcher.keepBelow = false;
    }
    workspace.activeWindow = t;
    if (launcher) launcher.keepBelow = true;
    busy = false;
}
function setup(w) {
    if (w.pid === launcherPid && w.normalWindow) {
        launcher = w;
        if (running) {
            w.fullScreen = false;
            w.keepBelow = true;
            w.fullScreenChanged.connect(focusTile);
        } else {
            w.keepBelow = false;
        }
    } else if (running && isTile(w)) {
        w.frameGeometryChanged.connect(focusTile);
        w.captionChanged.connect(focusTile);
    }
}
var ws = workspace.windowList();
for (var i = 0; i < ws.length; i++) setup(ws[i]);
if (running) {
    workspace.windowAdded.connect(function (w) { setup(w); focusTile(); });
    workspace.windowRemoved.connect(function (w) { if (w === lastTile) lastTile = null; });
    workspace.windowActivated.connect(focusTile);
    focusTile();
}
"""


def nested_session_windows(launcher_pid: int, running: bool) -> bool:
    """Nested KWin only: see _NESTED_SESSION_JS. running=False undoes it."""
    if not is_nested():
        return False
    name = 'janus-nested-session'
    if not running:
        kwin_unload_script(name)
        return kwin_run_script(_NESTED_SESSION_JS % dict(
            pid=int(launcher_pid), running='false'), name='janus-nested-restore')
    return kwin_load_script(_NESTED_SESSION_JS % dict(
        pid=int(launcher_pid), running='true'), name)


def activate_pid_window(pid: int) -> bool:
    """Unminimize, raise and focus the normal window of process *pid*.

    A Wayland client can neither unminimize itself nor take focus (KWin's
    focus-stealing prevention), so the GUI asks KWin to do it."""
    return kwin_run_script(f"""\
var ws = workspace.windowList();
for (var i = 0; i < ws.length; i++) {{
    var w = ws[i];
    if (w.pid === {int(pid)} && w.normalWindow) {{
        w.minimized = false;
        workspace.activeWindow = w;
        break;
    }}
}}
""", name='janus-activate')


@dataclass
class Output:
    name: str
    scale: float
    width: int          # current-mode pixel width (physical)
    height: int         # current-mode pixel height (physical)
    pos_x: int
    pos_y: int
    enabled: bool
    primary: bool


def get_outputs() -> List[Output]:
    """Enumerate connected/enabled outputs via kscreen-doctor JSON."""
    if is_nested():
        # One virtual output, the size of the Game Mode screen; scale 1.
        try:
            w, h = (int(x) for x in os.environ.get('JANUS_NESTED_SIZE', '').split('x'))
        except ValueError:
            w, h = 1280, 800
        return [Output('Janus', 1.0, w, h, 0, 0, True, True)]
    r = _kscreen('-j')
    if r.returncode != 0:
        return []
    data = json.loads(r.stdout)
    outs: List[Output] = []
    for o in data.get('outputs', []):
        if not o.get('enabled'):
            continue
        modes = {m['id']: m for m in o.get('modes', [])}
        cur = modes.get(o.get('currentModeId'))
        size = (cur or {}).get('size') or o.get('size') or {}
        pos = o.get('pos') or {}
        outs.append(Output(
            name=o['name'],
            scale=float(o.get('scale', 1.0)),
            width=int(size.get('width', 0)),
            height=int(size.get('height', 0)),
            pos_x=int(pos.get('x', 0)),
            pos_y=int(pos.get('y', 0)),
            enabled=True,
            primary=(o.get('priority') == 1),
        ))
    return outs


def set_scale(output: str, scale: float) -> bool:
    """Set an output's scale (e.g. 1.0 for the session, 2.5 to restore)."""
    if is_nested():
        return True
    # kscreen-doctor accepts a dotted form: output.<name>.scale.<value>
    val = ('%g' % scale)
    r = _kscreen(f'output.{output}.scale.{val}')
    return r.returncode == 0


# ---------------------------------------------------------------------------
# KWin window rules
# ---------------------------------------------------------------------------

@dataclass
class Tile:
    """One gamescope instance's placement (logical px; == physical at scale 1)."""
    wmclass: str        # unique per instance, e.g. "janus-p1"
    x: int
    y: int


def move_windows(geometry: Dict[str, tuple]) -> bool:
    """Move and resize running tiles: {wmclass: (x, y, w, h)}. gamescope's SDL
    window follows a resize live (new swapchain); the game inside keeps its
    resolution and is scaled into the new size. Rewrite the rules first
    (WaylandSession.update_tiles), or the forced position snaps back."""
    if not geometry:
        return True
    table = ',\n'.join(
        f'    "{c}": {{x: {int(x)}, y: {int(y)}, width: {int(w)}, height: {int(h)}}}'
        for c, (x, y, w, h) in geometry.items())
    return kwin_run_script(f"""\
var geo = {{
{table}
}};
var ws = workspace.windowList();
for (var i = 0; i < ws.length; i++) {{
    var g = geo["" + ws[i].resourceClass];
    if (g) {{
        ws[i].noBorder = true;
        ws[i].frameGeometry = g;
    }}
}}
""", name='janus-reflow')


def _rules_text(tiles: List[Tile], keep_above: bool = False,
                accept_focus: bool = True, disable_fsp: bool = True) -> str:
    """Render kwinrulesrc for the tiles.

    Focus-related knobs:

    * ``keep_above``   — force the tile always-on-top (``above``). Off by
      default: some setups route keyboard oddly when a game tile is kept
      above, so tiles stack normally unless asked.
    * ``accept_focus`` — force ``acceptfocus`` so KWin will give the tile
      keyboard focus at all.
    * ``disable_fsp``  — force focus-stealing-prevention to None (``fsplevel=0``)
      so the tile can activate / grab the keyboard when it maps, instead of the
      keys staying on the launching terminal/desktop. This is the usual fix for
      "the game window won't receive keyboard input" on KWin Wayland.
    """
    names = ','.join(t.wmclass for t in tiles)
    lines = ['[General]', f'count={len(tiles)}', f'rules={names}', '']
    for t in tiles:
        block = [
            f'[{t.wmclass}]',
            f'Description=Janus tile {t.wmclass}',
            f'wmclass={t.wmclass}',
            f'wmclassmatch={_EXACT}',
            'wmclasscomplete=false',
            f'position={t.x},{t.y}',
            f'positionrule={_FORCE}',
            'noborder=true',
            f'noborderrule={_FORCE}',
            'skiptaskbar=true',
            f'skiptaskbarrule={_FORCE}',
        ]
        if keep_above:
            block += ['above=true', f'aboverule={_FORCE}']
        if accept_focus:
            block += ['acceptfocus=true', f'acceptfocusrule={_FORCE}']
        if disable_fsp:
            block += ['fsplevel=0', f'fsplevelrule={_FORCE}']
        block.append('')
        lines += block
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

class WaylandSession:
    """
    Context manager that applies Path A window management and guarantees
    restoration (atexit + SIGINT/SIGTERM, idempotent).

        with WaylandSession(tiles, scale_outputs={'HDMI-A-1'}) as s:
            # launch gamescope instances with SDL_VIDEO_WAYLAND_WMCLASS=<tile.wmclass>
            ...
    """

    def __init__(self, tiles: List[Tile],
                 scale_outputs: Optional[List[str]] = None,
                 hide_panel: bool = True,
                 install_signal_handlers: bool = True,
                 keep_above: bool = False,
                 accept_focus: bool = True,
                 disable_fsp: bool = True):
        self.tiles = tiles
        self.scale_outputs = list(scale_outputs or [])
        self.hide_panel = hide_panel
        # KWin window-rule focus knobs (see _rules_text).
        self.keep_above = keep_above
        self.accept_focus = accept_focus
        self.disable_fsp = disable_fsp
        # GUI callers run inside a Qt event loop and manage their own teardown;
        # they pass False so we don't clobber Qt's SIGINT/SIGTERM handling.
        self.install_signal_handlers = install_signal_handlers

        self._applied = False
        self._restored = False
        # saved state for restore
        self._saved_scales: Dict[str, float] = {}
        self._saved_panels: Optional[str] = None    # comma-joined hiding modes
        self._rules_backup: Optional[bytes] = None  # original file, or None
        self._rules_existed = False
        self._prev_sigint = None
        self._prev_sigterm = None

    # -- lifecycle ---------------------------------------------------------

    def setup(self) -> 'WaylandSession':
        if self._applied:
            return self
        self._applied = True

        # Register restore hooks BEFORE mutating anything.
        atexit.register(self.teardown)
        if self.install_signal_handlers:
            self._prev_sigint = signal.signal(signal.SIGINT, self._on_signal)
            self._prev_sigterm = signal.signal(signal.SIGTERM, self._on_signal)

        # 1. Scale -> 1.0 on the chosen outputs (save current first).
        current = {o.name: o.scale for o in get_outputs()}
        for name in self.scale_outputs:
            if name in current and abs(current[name] - 1.0) > 1e-3:
                self._saved_scales[name] = current[name]
                set_scale(name, 1.0)
                print(f"[janus] Wayland: {name} scale {current[name]:g} -> 1.0")

        # 2. Hide panel (save current hiding modes).
        if self.hide_panel:
            saved = _plasma_eval(
                'var o=[];var ps=panels();'
                'for(var i=0;i<ps.length;i++)o.push(ps[i].hiding);'
                'print(o.join(","))')
            if saved:
                self._saved_panels = saved
                _plasma_eval(
                    'var ps=panels();'
                    'for(var i=0;i<ps.length;i++)ps[i].hiding="autohide";')
                print(f"[janus] Wayland: panel hidden (was: {saved})")

        # 3. Window rules (back up any existing file, then replace).
        if self.tiles:
            if KWINRULES.exists():
                self._rules_existed = True
                self._rules_backup = KWINRULES.read_bytes()
            KWINRULES.parent.mkdir(parents=True, exist_ok=True)
            KWINRULES.write_text(_rules_text(
                self.tiles, keep_above=self.keep_above,
                accept_focus=self.accept_focus, disable_fsp=self.disable_fsp))
            _kwin_reconfigure()
            print(f"[janus] Wayland: wrote {len(self.tiles)} window rule(s)")

        # 4. Nested KWin (Game Mode): launcher below the tiles.
        if is_nested():
            nested_session_windows(os.getpid(), True)

        return self

    def teardown(self) -> None:
        if not self._applied or self._restored:
            return
        self._restored = True

        if is_nested():
            try:
                nested_session_windows(os.getpid(), False)
            except Exception as e:
                print(f"[janus] Wayland: nested restore failed: {e}")

        # Rules first (restore original file or remove ours).
        try:
            if self.tiles:
                if self._rules_existed and self._rules_backup is not None:
                    KWINRULES.write_bytes(self._rules_backup)
                else:
                    KWINRULES.unlink(missing_ok=True)
                _kwin_reconfigure()
        except Exception as e:
            print(f"[janus] Wayland: rule restore failed: {e}")

        # Panel.
        try:
            if self._saved_panels is not None:
                modes = self._saved_panels.split(',')
                js = 'var ps=panels();' + ''.join(
                    f'if(ps[{i}])ps[{i}].hiding="{m}";'
                    for i, m in enumerate(modes))
                _plasma_eval(js)
        except Exception as e:
            print(f"[janus] Wayland: panel restore failed: {e}")

        # Scale.
        try:
            for name, scale in self._saved_scales.items():
                set_scale(name, scale)
                print(f"[janus] Wayland: {name} scale restored -> {scale:g}")
        except Exception as e:
            print(f"[janus] Wayland: scale restore failed: {e}")

    def update_tiles(self, tiles: List[Tile]) -> None:
        """Replace the window rules mid-session (a player joined or left;
        dynamic players). Windows already mapped keep their place until
        move_windows() moves them; a new tile's window gets its rule."""
        if not self._applied or self._restored:
            return
        if not self.tiles and KWINRULES.exists():
            # No rules were written at setup: back up the file now.
            self._rules_existed = True
            self._rules_backup = KWINRULES.read_bytes()
        self.tiles = list(tiles)
        KWINRULES.parent.mkdir(parents=True, exist_ok=True)
        KWINRULES.write_text(_rules_text(
            self.tiles, keep_above=self.keep_above,
            accept_focus=self.accept_focus, disable_fsp=self.disable_fsp))
        _kwin_reconfigure()

    # -- internals ---------------------------------------------------------

    def _on_signal(self, signum, frame):
        self.teardown()
        # Chain to a Python handler; otherwise (SIG_DFL — SIGTERM always, SIGINT
        # when it started out ignored, e.g. a backgrounded script) unwind like
        # Ctrl-C. Re-killing ourselves with the default action used to skip the
        # caller's cleanup, leaving every game running on the restored desktop.
        prev = self._prev_sigint if signum == signal.SIGINT else self._prev_sigterm
        if callable(prev):
            prev(signum, frame)
        else:
            raise KeyboardInterrupt

    def __enter__(self):
        return self.setup()

    def __exit__(self, exc_type, exc, tb):
        self.teardown()
        return False


# ---------------------------------------------------------------------------
# Manual self-test:  python3 -m host.wayland_session
# Applies a 50/50 dummy session, waits for Enter, restores. Launch two
# gamescope cubes by hand (SDL_VIDEO_WAYLAND_WMCLASS=janus-p1 / janus-p2) to see
# them snap into place.
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    if not is_kwin_wayland():
        print("Not a KWin Wayland session — nothing to do.")
        raise SystemExit(1)

    outs = get_outputs()
    print("Outputs:")
    for o in outs:
        print(f"  {o.name}: scale={o.scale:g} mode={o.width}x{o.height} "
              f"pos={o.pos_x},{o.pos_y} primary={o.primary}")

    target = next((o for o in outs if o.primary), outs[0])
    half = target.width // 2   # native px (scale will be 1.0)
    tiles = [Tile('janus-p1', 0, 0), Tile('janus-p2', half, 0)]
    print(f"\nApplying 50/50 split on {target.name} "
          f"({target.width}x{target.height} @ scale 1.0): "
          f"janus-p1@0,0  janus-p2@{half},0")

    with WaylandSession(tiles, scale_outputs=[target.name]):
        print("\nLaunch two cubes to test, e.g.:")
        print(f"  SDL_VIDEO_WAYLAND_WMCLASS=janus-p1 gamescope --backend sdl "
              f"-W {half} -H {target.height} -w {half} -h {target.height} "
              f"-r 60 -o 60 -b -- vkcube &")
        print(f"  SDL_VIDEO_WAYLAND_WMCLASS=janus-p2 gamescope --backend sdl "
              f"-W {half} -H {target.height} -w {half} -h {target.height} "
              f"-r 60 -o 60 -b -- vkcube &")
        try:
            input("\nPress Enter to restore... ")
        except (KeyboardInterrupt, EOFError):
            pass
    print("Restored.")

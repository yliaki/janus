"""What Janus needs from the system that it can't ship itself.

- **gamescope** (required): every game instance runs in its own gamescope
  window. It isn't bundled — it links against the system's graphics stack
  (Vulkan, wlroots, libinput, …), so it comes from the distribution's
  package manager. Debian/Ubuntu install it in /usr/games, which isn't on
  every PATH (desktop entries, Steam shortcuts), so that's searched too.
- **Steam Linux Runtimes** (recommended): the pressure-vessel containers
  games are launched in (steam.select_runtime). Without the one a game wants
  Janus falls back to another runtime or a bare launch, which usually works
  but isn't what Steam would do — so only a warning.
- **the Steam client, running** (warning): Steam Input grabs the pads and
  creates the per-player virtual controllers. Checked at both GUIs' startup
  and again before a launch (Steam may have been closed meanwhile).

Used by both GUIs (startup + launch), the CLI and check_gui_deps.sh::

    python3 src/host/prereqs.py gamescope   # prints the problem, exit 1
    python3 src/host/prereqs.py runtimes    # one missing runtime per line
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

try:
    import steam
except ImportError:            # imported as src.host.prereqs
    from . import steam

_GAMESCOPE_DIRS = ('/usr/games', '/usr/local/games')
_gamescope_cache: dict = {}


def find_gamescope() -> Optional[str]:
    """Absolute path of the gamescope binary, or None."""
    found = shutil.which('gamescope')
    if found:
        return found
    for d in _GAMESCOPE_DIRS:
        path = os.path.join(d, 'gamescope')
        if os.access(path, os.X_OK):
            return path
    return None


def _os_release() -> dict:
    out = {}
    try:
        for line in Path('/etc/os-release').read_text().splitlines():
            key, sep, val = line.partition('=')
            if sep:
                out[key] = val.strip().strip('"')
    except OSError:
        pass
    return out


def gamescope_install_hint() -> str:
    """How to install gamescope on this distribution."""
    osr = _os_release()
    ids = {osr.get('ID', '')} | set(osr.get('ID_LIKE', '').split())
    if osr.get('ID') == 'debian' and osr.get('VERSION_CODENAME') == 'trixie':
        # Not in trixie itself, only in its backports (contrib area).
        return ("Debian 13 has it in trixie-backports (contrib):\n"
                "  echo 'deb http://deb.debian.org/debian trixie-backports main contrib'"
                " | sudo tee /etc/apt/sources.list.d/backports.list\n"
                "  sudo apt update\n"
                "  sudo apt install -t trixie-backports gamescope")
    if 'ubuntu' in ids:
        # Packaged (multiverse) from 26.04 on; 24.04 doesn't have it.
        return ("Ubuntu has it from 26.04 on, in multiverse:\n"
                "  sudo add-apt-repository multiverse\n"
                "  sudo apt install gamescope")
    for like, cmd in (('debian', 'sudo apt install gamescope'),
                      ('arch', 'sudo pacman -S gamescope'),
                      ('fedora', 'sudo dnf install gamescope'),
                      ('suse', 'sudo zypper install gamescope'),
                      ('opensuse', 'sudo zypper install gamescope')):
        if like in ids:
            return f"Install it with:\n  {cmd}"
    return "Install the gamescope package with your distribution's package manager."


def gamescope_problem() -> Optional[str]:
    """None if gamescope is usable, else a message for the user (cached)."""
    if 'problem' in _gamescope_cache:
        return _gamescope_cache['problem']
    path = find_gamescope()
    problem = None
    if path is None:
        problem = ("gamescope isn't installed. Janus runs every player's game in "
                   "its own gamescope window, so it can't start games without it.\n\n"
                   + gamescope_install_hint())
    else:
        # Janus places the tiles through gamescope's SDL backend (--backend,
        # gamescope 3.14+). A gamescope that can't even print its help is
        # broken (missing libraries), one without --backend is too old.
        try:
            r = subprocess.run([path, '--help'], capture_output=True, text=True,
                               timeout=10)
            text = r.stdout + r.stderr
        except (OSError, subprocess.SubprocessError) as e:
            text = ''
            problem = f"gamescope ({path}) doesn't run: {e}"
        if problem is None and '--backend' not in text:
            problem = (f"The installed gamescope ({path}) is too old or doesn't "
                       "work: Janus needs gamescope 3.14 or newer (its "
                       "--backend option).\n\n" + gamescope_install_hint())
    _gamescope_cache['problem'] = problem
    return problem


STEAM_NOT_RUNNING = (
    "Steam doesn't appear to be running.\n\n"
    "Steam Input needs the Steam client running to create and route the "
    "per-player virtual controllers — without it controllers may not work, or "
    "both players may end up sharing one pad.\n\n"
    "Start Steam and make sure your controllers show under Settings › Controller.")


def steam_running() -> bool:
    """True if the Steam client (steam / steamwebhelper) is running."""
    try:
        entries = [e for e in os.listdir('/proc') if e.isdigit()]
    except OSError:
        return False
    for e in entries:
        try:
            with open(f'/proc/{e}/comm') as f:
                if f.read().strip() in ('steam', 'steamwebhelper'):
                    return True
        except OSError:
            continue
    return False


def _install_runtime_text(appids: List[int]) -> str:
    names = '\n'.join(f'  • {steam.runtime_name(a)}  (steam://install/{a})'
                      for a in appids)
    return (f"{names}\n\nTo install: in Steam's Library, show Tools (the filter "
            "next to the search box), select the runtime and press Install — or "
            "open the steam://install/… link above.")


def runtime_warning(game, script=None) -> Optional[str]:
    """A warning if *game* would be launched without the Steam Linux Runtime
    it wants (steam.select_runtime, honouring the handler's Game.runtime and
    Game.useRuntime), else None."""
    if script is not None and (not script.get('useRuntime', True)
                               or getattr(script, 'non_steam', False)):
        return None
    try:
        choice = steam.select_runtime(
            game, script.get('runtime') if script is not None else None)
    except Exception:        # unreadable install etc.: the launch reports it
        return None
    if not choice.missing:
        return None
    instead = ('without a runtime' if not choice.wrapper
               else f'in {choice.label} instead')
    return (f"{game.name} should run in a Steam Linux Runtime that isn't "
            f"installed:\n{_install_runtime_text(choice.missing)}\n\n"
            f"Janus will start it {instead}, which may not work.")


LINUX, WINDOWS = 'linux', 'windows'
_BUILD_LABEL = {LINUX: 'Linux', WINDOWS: 'Windows'}


def expected_build(script, exe_name: Optional[str] = None,
                   launch_command: Optional[str] = None) -> Optional[str]:
    """The build the launch is set up for, from the effective launch command /
    executable (CLI flag or GUI setting, else the handler's Game.launchCommand
    / Game.exeName): a .exe means the Windows build (Proton), anything else a
    Linux one. None when nothing names an executable (auto-detection takes
    what's there), for non-Steam games, and when the handler adapts to what's
    installed itself (Game.onGameResolved)."""
    if script is not None and (getattr(script, 'non_steam', False)
                               or script.has_hook('onGameResolved')):
        return None
    if script is not None:
        launch_command = launch_command or script.get('launchCommand')
        exe_name = exe_name or script.get('exeName')
    if launch_command:
        try:
            first = steam._launch_command_parts(launch_command)[0]
        except (ValueError, IndexError):
            return None
        return WINDOWS if first.lower().endswith('.exe') else LINUX
    if exe_name:
        return WINDOWS if exe_name.lower().endswith('.exe') else LINUX
    return None


def installed_build(install_dir: Path) -> Optional[str]:
    """Which build Steam installed, from the files (not from the handler's
    command, unlike SteamGame.is_native): a native executable = Linux, else
    .exe files = Windows. None when it can't tell."""
    install_dir = Path(install_dir)
    if not install_dir.is_dir():
        return None
    if steam.has_native_build(install_dir):
        return LINUX
    base = len(install_dir.parts)
    for dirpath, dirnames, filenames in os.walk(install_dir):
        if len(Path(dirpath).parts) - base >= 3:
            dirnames[:] = []
        if any(f.lower().endswith('.exe') for f in filenames):
            return WINDOWS
    return None


def forced_compat_tool(appid: int) -> Optional[str]:
    """The compatibility tool the player forced for *appid* in Steam
    (Properties → Compatibility), from config.vdf's CompatToolMapping."""
    try:
        cfg = steam.parse_vdf(steam.find_steam_root() / 'config' / 'config.vdf')
        node = cfg
        for key in ('InstallConfigStore', 'Software', 'Valve', 'Steam',
                    'CompatToolMapping', str(appid)):
            node = next((v for k, v in node.items() if k.lower() == key.lower()), None)
            if not isinstance(node, dict):
                return None
        return node.get('name') or None
    except Exception:
        return None


def _tool_label(name: str) -> str:
    """proton_experimental -> Proton Experimental, proton_9 -> Proton 9;
    other tools (GE-Proton9-20, …) are named as they are."""
    if name.lower().startswith('proton_'):
        return 'Proton ' + ' '.join(w.capitalize() for w in name[7:].split('_'))
    return name


def build_problem(game, script, exe_name: Optional[str] = None,
                  launch_command: Optional[str] = None) -> Optional[str]:
    """Why the launch can't work: the handler is written for the game's Linux
    build and Steam installed the Windows one (or the other way round). Games
    with both builds get the Windows one when Proton is forced for them in
    Steam. None when they match or either side is unknown."""
    want = expected_build(script, exe_name, launch_command)
    if want is None:
        return None
    have = installed_build(game.install_dir)
    if have is None or have == want:
        return None
    try:
        import steam_input
    except ImportError:            # imported as src.host.prereqs
        from . import steam_input
    name = game.name or f'app {game.appid}'
    where = steam_input.properties_path(name) + ' → Compatibility'
    head = (f"Janus is set up to start the {_BUILD_LABEL[want]} build of {name} "
            f"(the handler's executable), but Steam installed the "
            f"{_BUILD_LABEL[have]} build.")
    if want == LINUX:
        tool = forced_compat_tool(game.appid)
        why = (f' Steam is set to run it with {_tool_label(tool)}.' if tool else '')
        fix = (f'In Steam: {where} → untick "Force the use of a specific Steam '
               f'Play compatibility tool". Steam then downloads the Linux build; '
               f'launch again once it has finished.')
        return f'{head}{why}\n\n{fix}'
    fix = (f'In Steam: {where} → tick "Force the use of a specific Steam Play '
           f'compatibility tool" and pick a Proton version. Steam then downloads '
           f'the Windows build; launch again once it has finished.')
    return f'{head}\n\n{fix}'


def wanted_runtimes(steam_root: Path) -> List[int]:
    """The runtimes worth having: sniper (native games), scout (32-bit native
    games) and whatever the installed Proton builds ask for."""
    wanted = [steam.RUNTIME_SNIPER, steam.RUNTIME_SCOUT]
    protons = [d for sa in steam.steamapps_dirs(steam_root)
               for d in (sa / 'common').glob('Proton*')]
    protons += list((Path(steam_root) / 'compatibilitytools.d').glob('*'))
    for p in protons:
        appid = steam.proton_required_runtime(p)
        if appid and appid not in wanted:
            wanted.append(appid)
    return wanted


def missing_runtimes() -> List[int]:
    """Wanted runtimes (see wanted_runtimes) that aren't installed. Raises
    FileNotFoundError without Steam."""
    root = steam.find_steam_root()
    return [a for a in wanted_runtimes(root) if not steam.find_runtime(root, a)]


def main(argv: List[str]) -> int:
    what = argv[1] if len(argv) > 1 else ''
    if what == 'gamescope':
        problem = gamescope_problem()
        if problem:
            print(problem)
            return 1
        print(find_gamescope())
        return 0
    if what == 'runtimes':
        try:
            missing = missing_runtimes()
        except FileNotFoundError as e:
            print(e)
            return 2
        for appid in missing:
            print(f'{steam.runtime_name(appid)}\tsteam://install/{appid}')
        return 0
    print(__doc__)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv))

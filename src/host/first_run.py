"""A Proton game Steam has never started: start it once through Steam.

Steam creates a Proton game's prefix (compatdata/<appid>, with its
config_info naming the Proton build) and runs its install scripts
(redistributables, sometimes the game's own setup) the first time the game
is played from Steam. Janus needs both: without config_info a Windows build
isn't recognised as a Proton game at all ("no Wine prefix", "No executable
found"). So when a launch finds a Windows build without it, Janus starts the
game once through Steam (steam://rungameid), waits for Steam to set it up and
the game to run, closes it again, and carries on with the launch.
"""

import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

try:
    import prereqs
    import steam
except ImportError:                    # imported as src.host.first_run
    from . import prereqs, steam


def config_info(game) -> Path:
    return Path(game.library_path) / 'compatdata' / str(game.appid) / 'config_info'


def needed(game, script=None) -> bool:
    """True for a Steam game whose Windows build is installed but which Steam
    has never started (no Proton prefix yet). Non-Steam games and handlers
    that resolve the game themselves (Game.onGameResolved) never need it."""
    if script is not None and (getattr(script, 'non_steam', False)
                               or script.has_hook('onGameResolved')):
        return False
    try:
        if config_info(game).exists():
            return False
        return prereqs.installed_build(game.install_dir) == prereqs.WINDOWS
    except (OSError, AttributeError):
        return False


def message(game) -> str:
    name = game.name or f'app {game.appid}'
    return (f'{name} has never been started from Steam, so Steam hasn\'t set it up '
            f'yet (its Proton prefix, its installers). Janus starts it once through '
            f'Steam now and closes it again; then the session starts.')


def _steam_game_pids(appid: int):
    """Processes Steam started for *appid* (SteamAppId in their environment,
    or Steam's reaper "SteamLaunch AppId=<appid>"), never Janus's own."""
    me = os.getpid()
    env_mark = f'SteamAppId={appid}\0'.encode()
    arg_mark = f'AppId={appid}'.encode()
    pids = []
    for entry in os.listdir('/proc'):
        if not entry.isdigit() or int(entry) == me:
            continue
        try:
            with open(f'/proc/{entry}/cmdline', 'rb') as f:
                cmd = f.read()
            with open(f'/proc/{entry}/environ', 'rb') as f:
                env = f.read()
        except OSError:
            continue
        if b'libjanus' in env:
            continue                          # a Janus session's process
        if arg_mark + b'\0' in cmd + b'\0' or env_mark in env:
            pids.append(int(entry))
    return pids


def run(game, progress: Callable[[str], None] = print, timeout: float = 900) -> Optional[str]:
    """Start *game* once through Steam and close it when it runs. Returns
    None when the prefix exists afterwards, else why not."""
    name = game.name or f'app {game.appid}'
    if not prereqs.steam_running():
        return f'Steam is not running: start Steam, then launch {name} again.'
    progress(f'Starting {name} once through Steam…')
    try:
        subprocess.Popen(['steam', f'steam://rungameid/{game.appid}'],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as e:
        return f"Couldn't ask Steam to start {name}: {e}"
    start = time.monotonic()
    seen = None                                  # when the game was first seen running
    told = False
    while time.monotonic() - start < timeout:
        time.sleep(1)
        running = _steam_game_pids(game.appid)
        if running and seen is None:
            seen = time.monotonic()
            progress(f'Steam is setting {name} up…')
        if not told and time.monotonic() - start > 20 and not running:
            told = True
            progress(f'Waiting for Steam to start {name} (answer any Steam dialog)…')
        # Running for a while with its prefix in place: Steam's part is done
        # (install scripts run before the game itself starts).
        if seen is not None and config_info(game).exists() \
                and time.monotonic() - seen > 15:
            break
    else:
        _close(game.appid)
        return (f'Steam didn\'t start {name} within {int(timeout / 60)} minutes. '
                f'Start it once from Steam yourself, then launch it again.')
    progress(f'Closing {name} again…')
    _close(game.appid)
    return None if config_info(game).exists() else \
        f'Steam started {name} but made no Proton prefix for it.'


def _close(appid: int):
    for sig, wait in ((signal.SIGTERM, 5), (signal.SIGKILL, 5)):
        pids = _steam_game_pids(appid)
        if not pids:
            break
        for pid in pids:
            try:
                os.kill(pid, sig)
            except OSError:
                pass
        end = time.monotonic() + wait
        while time.monotonic() < end and _steam_game_pids(appid):
            time.sleep(0.5)
    time.sleep(3)          # let Steam notice the game stopped before the session starts

"""Is Steam Input set to "Enable Steam Input" for a game?

Janus's per-instance controller isolation relies on Steam Input being on for
the game (Steam's virtual pads, virtualgamepadinfo.txt). Steam keeps the
per-game choice (game Properties -> Controller) in the account's
``userdata/<account id>/config/localconfig.vdf`` under
``UserLocalConfigStore/apps/<appid>/UseSteamControllerConfig``, rewritten
within a second of every change:

    no entry  never touched (follows Steam's default)
    "1"       Use default settings
    "0"       Disable Steam Input
    "2"       Enable Steam Input

Writing it ourselves doesn't work while Steam runs (Steam rewrites the file
from memory), so a launch only checks and refuses with instructions.
Handlers opt out with ``Game.requireSteamInput = false``; games whose Steam
layout can't work per player (a keyboard/mouse layout drives the shared
desktop pointer and keyboard) set ``Game.requireSteamInput = "disabled"``,
which requires "Disable Steam Input" instead.
"""

import os
import re
from pathlib import Path
from typing import Optional

ENABLED = '2'
DISABLED = '0'
_LABELS = {'0': 'Disable Steam Input', '1': 'Use default settings',
           '': 'Use default settings'}

# Text VDF token: a quoted string (with backslash escapes) or a brace.
_TOKEN = re.compile(r'"((?:\\.|[^"\\])*)"|([{}])')


class SteamInputNotEnabled(ValueError):
    """The game's Steam Input setting doesn't match the input mode.

    mode: the input mode that was checked ('janus'/'steam', controller_input.py).
    value: the game's UseSteamControllerConfig ('' = no entry).
    alternative: the other mode, when the handler allows it AND the current
    setting already suits it (the GUIs offer "play with … this time"); None
    otherwise."""

    def __init__(self, message, mode='steam', value='', alternative=None):
        super().__init__(message)
        self.mode = mode
        self.value = value
        self.alternative = alternative


def parse_text_vdf(text: str) -> dict:
    """Text VDF -> nested dicts. Unlike steam.parse_vdf it handles escaped
    quotes, which localconfig.vdf has (JSON blobs in string values)."""
    root, stack, key = {}, [], None
    cur = root
    for m in _TOKEN.finditer(text):
        s, brace = m.group(1), m.group(2)
        if brace == '{':
            child = {}
            cur[key] = child
            stack.append(cur)
            cur, key = child, None
        elif brace == '}':
            cur = stack.pop() if stack else root
        elif key is None:
            key = s
        else:
            cur[key] = s
            key = None
    return root


def _localconfig_files():
    """localconfig.vdf of the Steam account Janus treats as the owner (the
    most recent user in loginusers.vdf), on every Steam root."""
    try:
        from steam import find_all_steam_roots
        import profiles
    except ImportError:
        from .steam import find_all_steam_roots
        from . import profiles
    account_id = int(profiles.steam_owner().steam_id) & 0xFFFFFFFF
    return [root / 'userdata' / str(account_id) / 'config' / 'localconfig.vdf'
            for root in find_all_steam_roots()]


def setting(appid: int) -> Optional[str]:
    """The game's UseSteamControllerConfig value ('' = no entry), or None
    when the account's localconfig.vdf can't be found or read."""
    for f in _localconfig_files():
        try:
            cfg = parse_text_vdf(Path(f).read_text(errors='replace'))
        except OSError:
            continue
        apps = (cfg.get('UserLocalConfigStore') or {}).get('apps') or {}
        value = (apps.get(str(appid)) or {}).get('UseSteamControllerConfig')
        return '' if value is None else str(value)
    return None


def _is_steam(appid) -> bool:
    """A Steam appid, not a non-Steam game's id (nonsteam.py)."""
    return isinstance(appid, int) or str(appid).isdigit()


def required_value(script, mode: str = 'steam') -> Optional[str]:
    """The UseSteamControllerConfig value a launch in *mode* needs, None when
    any will do. Steam Input mode: "Enable Steam Input" (handlers: "Disable"
    for ``requireSteamInput = "disabled"``, anything for ``false``). Janus
    input: "Disable Steam Input", so Steam leaves the pads (and its own API's
    view of them) alone — unless the handler says the game doesn't deal with
    Steam Input at all (``requireSteamInput = false``)."""
    required = script.get('requireSteamInput', True) if script is not None else True
    if required is False:
        return None
    if mode == 'janus' or required == 'disabled':
        return DISABLED
    return ENABLED


def check(appid: int, script=None, game_name: Optional[str] = None,
          mode: str = 'steam'):
    """Raise SteamInputNotEnabled unless the game's Steam Input setting is
    what *mode* needs (required_value()). A setting that can't be read
    doesn't block the launch (logged)."""
    want = required_value(script, mode)
    if want is None or not _is_steam(appid):
        return
    value = setting(appid)
    if value is None:
        print(f"[janus] Steam Input setting for app {appid} unknown "
              f"(localconfig.vdf not found); not checked", flush=True)
        return
    if value == want:
        return
    import controller_input as input_mode
    name = game_name or (script.get('name') if script is not None else None) \
        or f'app {appid}'
    current = 'Enable Steam Input' if value == ENABLED else \
        _LABELS.get(value, f'unknown value "{value}"')
    alt = input_mode.other(mode)
    alternative = alt if (alt in input_mode.supported(script)
                          and required_value(script, alt) in (None, value)) else None
    or_else = (f'\n\nOr play with {input_mode.LABELS[alt]} this time.'
               if alternative else '')
    if mode == 'janus':
        raise SteamInputNotEnabled(
            f'Steam Input is set to "{current}" for {name}. Janus input needs '
            f'"Disable Steam Input", so Steam leaves the controllers to Janus.\n\n'
            f'In Steam: {properties_path(name)} → Controller → '
            f'"Disable Steam Input", then launch again.' + or_else,
            mode, value, alternative)
    if want == DISABLED:
        raise SteamInputNotEnabled(
            f'Steam Input is set to "{current}" for {name}. Steam\'s controller '
            f'layout for this game turns every controller into the shared mouse '
            f'and keyboard, so Janus needs "Disable Steam Input": each game then '
            f'reads its own controller directly.\n\n'
            f'In Steam: {properties_path(name)} → Controller → '
            f'"Disable Steam Input", then launch again.' + or_else,
            mode, value, alternative)
    raise SteamInputNotEnabled(
        f'Steam Input is set to "{current}" for {name}. Steam Input mode needs '
        f'"Enable Steam Input" to give each player their own controller.\n\n'
        f'In Steam: {properties_path(name)} → Controller → '
        f'"Enable Steam Input", then launch again.' + or_else,
        mode, value, alternative)

def properties_path(name: str) -> str:
    """How to reach a game's Properties in the Steam UI the player is in:
    Game Mode (SteamOS, nested KWin) has no right-click."""
    if os.environ.get('JANUS_NESTED_KWIN') == '1' or \
            os.environ.get('XDG_CURRENT_DESKTOP', '').lower() == 'gamescope':
        return f'open {name} in the library → ⚙ → Properties…'
    return f'right-click {name} → Properties'


# --- Gamepad layout per controller ---------------------------------------------
#
# Games whose default Steam layout is keyboard/mouse (Steam lists them as
# "Controllers Not Supported") drive the one shared desktop pointer and
# keyboard: every pad steers whichever tile has focus, and Steam creates no
# virtual pads. Picking a Gamepad template for the game fixes it, and Steam
# stores that choice per physical controller, in
# <steam root>/steamapps/common/Steam Controller Configs/<account id>/config/:
#
#     configset_<vid>-<pid>-<suffix>.vdf   (hex, no leading zeros; the suffix
#                                           is the end of Steam's controller
#                                           handle, sometimes with a letter)
#     configset_N<type><mac>.vdf            (Bluetooth Nintendo pads, by MAC)
#
#     "<appid>" { "template" "controller_xboxone_gamepad_joystick.vdf" }
#
# Handlers of such games set Game.requireGamepadLayout = true; launches then
# refuse until every pad in the session has a gamepad template for the game.
# NOTES.md "Gamepad layout check".

class GamepadLayoutMissing(SteamInputNotEnabled):
    """Some pads have no Gamepad template for the game."""


def _configset_dirs():
    try:
        from steam import find_all_steam_roots
        import profiles
    except ImportError:
        from .steam import find_all_steam_roots
        from . import profiles
    account_id = int(profiles.steam_owner().steam_id) & 0xFFFFFFFF
    return [root / 'steamapps' / 'common' / 'Steam Controller Configs'
            / str(account_id) / 'config' for root in find_all_steam_roots()]


def _configset_files(vid: int, pid: int, uniq: str = '', dirs=None):
    """The controller's configset files, newest first. A pad Steam keys by
    MAC (Bluetooth Nintendo pads: configset_N<type><mac>.vdf) uses only that
    file: a configset_<vid>-<pid> file for the same model can be left over
    from a USB connection, and its Gamepad entry doesn't apply (2026-10-27:
    the Pro Controller passed the check on such an entry, got Steam's
    keyboard/mouse layout for Sven and its trigger clicked into the focused
    tile)."""
    dirs = dirs if dirs is not None else _configset_dirs()
    mac = uniq.replace(':', '').lower() if uniq else ''
    files = []
    if len(mac) == 12:
        for d in dirs:
            files += Path(d).glob(f'configset_N*{mac}.vdf')
    if not files:
        for d in dirs:
            files += Path(d).glob(f'configset_{vid:x}-{pid:x}-*.vdf')
    return sorted(set(files), key=lambda f: f.stat().st_mtime, reverse=True)


def gamepad_layout(appid: int, vid: int, pid: int, uniq: str = '',
                   dirs=None) -> Optional[str]:
    """'gamepad' when the controller has a Gamepad template for *appid*,
    'missing' when it has none, the template name for another template,
    None when Steam has no configset file for the pad (can't tell)."""
    files = _configset_files(vid, pid, uniq, dirs)
    if not files:
        return None
    for f in files:
        try:
            cfg = parse_text_vdf(f.read_text(errors='replace'))
        except OSError:
            continue
        entry = (cfg.get('controller_config') or {}).get(str(appid))
        if not isinstance(entry, dict):
            continue
        template = entry.get('template')
        if template is None:
            return None             # workshop/personal config: can't classify
        return 'gamepad' if 'gamepad' in template else template
    return 'missing'


def check_gamepad_layout(appid: int, script, controllers,
                         game_name: Optional[str] = None, dirs=None):
    """Raise GamepadLayoutMissing with gamepad_layout_warning()'s text."""
    warning = gamepad_layout_warning(appid, script, controllers, game_name, dirs)
    if warning:
        raise GamepadLayoutMissing(warning)


def gamepad_layout_warning(appid: int, script, controllers,
                           game_name: Optional[str] = None,
                           dirs=None) -> Optional[str]:
    """The warning to show (the user may launch anyway) when the handler sets
    requireGamepadLayout and a pad in *controllers* (ControllerInfo-like:
    name, vendor_id, product_id, uniq) lacks a Gamepad template for the game;
    None when all is well. Pads Steam has no configset file for don't count
    (logged)."""
    if script is None or not script.get('requireGamepadLayout', False) \
            or not _is_steam(appid):
        return None
    bad = []
    for c in controllers:
        state = gamepad_layout(appid, c.vendor_id, c.product_id,
                               getattr(c, 'uniq', ''), dirs)
        if state is None:
            print(f'[janus] Steam layout of "{c.name}" for app {appid} unknown; '
                  f'not checked', flush=True)
        elif state != 'gamepad':
            bad.append(c.name if state == 'missing' else f'{c.name} ({state})')
    if not bad:
        return None
    name = game_name or script.get('name') or f'app {appid}'
    pads = '\n'.join(f'  • {b}' for b in bad)
    return (
        f"Steam's controller layout for {name} turns a controller into the "
        f"shared mouse and keyboard, so it would steer every player's game. "
        f"These controllers need a Gamepad layout for {name}:\n{pads}\n\n"
        f"In Steam: {properties_path(name)} → Controller → the layout "
        f"→ Templates → Gamepad, with each of these controllers connected, "
        f"then launch again.")


# --- Forcing the game's Steam Input layout for a session ----------------------
#
# Steam picks the Steam Input layout from the window it thinks has focus. It
# can't see native Wayland windows and doesn't recognise our gamescope tiles,
# so it often sat on its Desktop layout (app 413080: pad -> mouse/keys, nothing
# on the virtual pads) and the games got no input: after focus left a tile,
# and after a Unity game exited and left its virtual pads behind (the next
# launch only worked again after restarting Janus or opening Steam's
# controller settings). steam://forceinputappid/<appid> makes Steam apply that
# app's layout to every controller regardless of focus (tested 2026-10-17:
# immediate, and it holds). A session forces its game's layout and releases
# it at the end: forcing the Desktop layout switches back at once, /0 then
# clears the override. NOTES.md "Forcing the game's Steam Input layout".

DESKTOP_LAYOUT = 413080

_url_queue = None       # one worker, so the URLs reach Steam in call order
_forced: Optional[int] = None


def _steam_running() -> bool:
    try:
        pid = int((Path.home() / '.steam' / 'steam.pid').read_text().strip())
        return Path(f'/proc/{pid}/comm').read_text().strip() == 'steam'
    except (OSError, ValueError):
        return False


def _open_steam_url(url: str) -> None:
    import shutil
    import subprocess
    cmd = ['steam', url] if shutil.which('steam') else ['xdg-open', url]
    try:
        subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        print(f'[janus] {" ".join(cmd)} failed: {e}', flush=True)


def _queue_urls(*urls: str, gap: float = 1.0) -> None:
    global _url_queue
    import concurrent.futures
    import time
    if _url_queue is None:
        _url_queue = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix='janus-steam-url')

    def run():
        for i, url in enumerate(urls):
            if i:
                time.sleep(gap)
            _open_steam_url(url)
    _url_queue.submit(run)


def force_layout(appid: int, script=None) -> None:
    """Make Steam use *appid*'s Steam Input layout until release_layout().
    Not for games that need Steam Input off (``requireSteamInput =
    "disabled"``): their layout is the very thing that must not apply."""
    global _forced
    if script is not None and script.get('requireSteamInput', True) == 'disabled':
        return
    if not _is_steam(appid):     # a non-Steam game (nonsteam.py) has no layout
        return
    if not _steam_running():
        return
    _forced = appid
    print(f'[janus] Steam Input: forcing the layout of app {appid}', flush=True)
    _queue_urls(f'steam://forceinputappid/{appid}')


def release_layout(wait: bool = False) -> None:
    """Back to Steam's normal behaviour (Desktop layout until focus decides).
    No-op unless force_layout() ran. *wait*: block until sent (CLI exit)."""
    global _forced
    if _forced is None:
        return
    _forced = None
    print('[janus] Steam Input: releasing the forced layout', flush=True)
    _queue_urls(f'steam://forceinputappid/{DESKTOP_LAYOUT}', 'steam://forceinputappid/0')
    if wait and _url_queue is not None:
        _url_queue.submit(lambda: None).result(timeout=40)

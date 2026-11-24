"""UI scale for Unreal Engine (UE4/UE5) games (Proton and native Linux).

``Game.unrealUiScale = true`` gives a handler two automatic options, written
into the instance's user ``Engine.ini`` (``[/Script/Engine.UserInterfaceSettings]``):

- "Size UI by window height" (``uiScaleByHeight``, default on) ->
  ``UIScaleRule=Vertical``. UE sizes its UI from the viewport's shortest
  side by default — in a side-by-side tile (960x1080) the width — so the UI
  shrinks; by height it's as big as the game makes it at that height (Deep
  Rock Galactic: ~1.9x in a 960x1080 tile, its own DPI curve shrinks narrow
  viewports hard). No effect in wide tiles, where the shortest side is the
  height anyway.
- "UI scale" (``uiScale``, 1.0 ... 2.5, as ``Game.unityUiScale``) ->
  ``ApplicationScale``, multiplying all UI on top.

No mods: UE reads these from the per-user config. That is one factor for
all UI, so a menu can run off screen if it's set too high.

``Game.unrealUiFit = true`` (Proton only) makes "UI scale" a maximum instead,
fitted per screen like the Unity plugin: Janus deploys UE4SS
(``vendor/ue4ss/``) with its Lua mod ``src/unreal_uifit/main.lua`` next to
the game's exe in the per-instance mirror and passes the factor as
``JANUS_UI_SCALE``; the mod sets ``ApplicationScale`` at runtime to the
largest value that keeps the current screen on screen. ``ApplicationScale``
is then not written to Engine.ini. Engine builds UE4SS can't scan by itself
need the upstream per-game config (``UE4SS-settings.ini`` +
``UE4SS_Signatures/``) in the handler's ``payload/ue4ss/``.

The config dir is Saved/Config/Windows (UE5) or WindowsNoEditor (UE4) — Linux
or LinuxNoEditor for native builds — under the game's user Saved dir: the one
a ``-UserDir=`` argument names (Deep Rock Galactic), else
``AppData/Local/<Project>/Saved`` in the instance's Wine prefix, or
``~/.config/Epic/<Project>/Saved`` in the instance's home for native builds
(KLETKA). The file is backed up and restored at session end (Janus.backup), so
a player's own config is never left changed; with both off nothing is written.
"""

import os
import re
import shutil
from pathlib import Path
from typing import List, Optional

import bepinex

_REPO = Path(__file__).resolve().parent.parent.parent
UE4SS_DIR = _REPO / 'vendor' / 'ue4ss' / 'win64'
FIT_MOD = _REPO / 'src' / 'unreal_uifit' / 'main.lua'
FIT_MOD_NAME = 'JanusUiFit'

SECTION = '/Script/Engine.UserInterfaceSettings'
BY_HEIGHT_OPTION = 'uiScaleByHeight'


def by_height_option_spec() -> dict:
    return {
        'label': 'Size UI by window height',
        'type': 'bool',
        'default': True,
        'description': 'Unreal sizes its UI from the shorter side of the window, '
                       'which in a side-by-side tile is the width, so the UI '
                       'shrinks. On: size it from the height instead, as the game '
                       'would at that height. Turn off if menus run off the sides.',
    }


def option_spec(fit: bool = False) -> dict:
    spec = bepinex.ui_scale_option_spec()
    if fit:
        spec['description'] = ('Makes the game\'s menus and HUD bigger (Unreal '
                               'Engine UI scale, via UE4SS). Each screen is '
                               'shrunk back as far as needed to stay on screen, '
                               'never below the game\'s own size. 1.0 = off: '
                               'nothing is installed.')
    else:
        spec['description'] = ('Makes the game\'s menus and HUD bigger (Unreal '
                               'Engine UI scale, one factor for all UI: if a menu '
                               'runs off screen, lower it). 1.0 = off.')
    return spec


def scale(script) -> float:
    if script is None or not script.get('unrealUiScale'):
        return 1.0
    try:
        return float(script.options.get(bepinex.UI_SCALE_OPTION) or 1.0)
    except (TypeError, ValueError):
        return 1.0


def fit_needed(script) -> bool:
    """Game.unrealUiFit with a UI scale above 1.0: UE4SS + JanusUiFit."""
    return bool(script is not None and script.get('unrealUiFit')
                and scale(script) > 1.0 + 1e-6)


def resizes(script) -> bool:
    """Game.resizeGame = "unreal": the mod keeps the game at $JANUS_SIZE_FILE
    (r.SetRes, windowed) as the tiles re-flow (dynamic players, resize.py)."""
    return script is not None and script.get('resizeGame') == 'unreal'


def mod_needed(script) -> bool:
    """UE4SS + the Janus mod: for the UI fit, for resizing, or both."""
    return fit_needed(script) or resizes(script)


def deploy(script, mirror_root: Path, exe: Path, env: dict,
           is_proton: bool) -> bool:
    """Install UE4SS + JanusUiFit next to *exe* (the *-Shipping.exe inside
    *mirror_root*) and set the env it needs. Returns True if deployed;
    problems are reported to the user and the game launches without it."""
    if not mod_needed(script):
        return False

    def skip(why):
        script._user_message('warning', f'UI scale not applied: {why}')
        return False

    if not is_proton:
        return skip('the per-screen Unreal UI fit needs a Proton (Windows) game.')
    if not (UE4SS_DIR / 'dwmapi.dll').is_file() or not FIT_MOD.is_file():
        return skip(f'UE4SS or the fit mod is missing ({UE4SS_DIR}, {FIT_MOD}).')
    try:
        rel_dir = Path(os.path.abspath(exe.parent)).relative_to(
            os.path.abspath(mirror_root))
    except ValueError:
        return skip(f'the game exe {exe} is not inside the mirror.')
    if not exe.name.lower().endswith('-shipping.exe'):
        return skip(f'{exe.name} is not an Unreal *-Shipping.exe (set Game.exeName).')

    exe_dir = bepinex._materialize(mirror_root, rel_dir)
    for entry in UE4SS_DIR.iterdir():
        dst = exe_dir / entry.name
        if dst.is_symlink() or dst.exists():
            if dst.is_dir() and not dst.is_symlink():
                shutil.rmtree(dst)
            else:
                dst.unlink()
        if entry.is_dir():
            shutil.copytree(entry, dst)
        else:
            shutil.copy2(entry, dst)
    ue4ss = exe_dir / 'ue4ss'
    from scripting import payload_dir_for
    game_cfg = payload_dir_for(script.script_path) / 'ue4ss'
    if game_cfg.is_dir():
        shutil.copytree(game_cfg, ue4ss, dirs_exist_ok=True)
    mod = ue4ss / 'Mods' / FIT_MOD_NAME / 'scripts'
    mod.mkdir(parents=True, exist_ok=True)
    shutil.copy2(FIT_MOD, mod / 'main.lua')
    (ue4ss / 'Mods' / 'mods.txt').write_text(f'{FIT_MOD_NAME} : 1\n')
    (ue4ss / 'Mods' / 'mods.json').write_text(
        f'[{{"mod_name": "{FIT_MOD_NAME}", "mod_enabled": true}}]\n')

    overrides = env.get('WINEDLLOVERRIDES', '')
    if 'dwmapi' not in overrides:
        env['WINEDLLOVERRIDES'] = (f'{overrides};dwmapi=n,b' if overrides
                                   else 'dwmapi=n,b')
    fit = fit_needed(script)
    if fit:
        env['JANUS_UI_SCALE'] = f'{scale(script):g}'
    what = (f'UI scale up to {scale(script):g}' if fit else 'no UI fit') + \
        (', resizing' if resizes(script) else '')
    print(f'[janus] UE4SS -> {exe_dir}: {FIT_MOD_NAME} ({what}'
          f'{", game config" if game_cfg.is_dir() else ""})', flush=True)
    return True


def _project_name(install_dir: Path) -> Optional[str]:
    """<Project> of <install>/<Project>/Binaries/Win64/<x>-Win64-Shipping.exe
    (or Binaries/Linux/<x>-Linux-Shipping for native builds)."""
    for pattern in ('*/Binaries/Win64/*-Shipping.exe', '*/Binaries/Linux/*-Shipping'):
        for exe in sorted(install_dir.glob(pattern)):
            return exe.parent.parent.parent.name
    return None


def _wine_to_host(win: str, prefix: Path) -> Optional[Path]:
    m = re.match(r'^([A-Za-z]):[\\/](.*)$', win.strip().strip('"'))
    if not m:
        return None
    rest = m.group(2).replace('\\', '/')
    if m.group(1).lower() == 'z':
        return Path('/') / rest
    return prefix / f'drive_{m.group(1).lower()}' / rest


def saved_dir(cmd: List[str], install_dir: Path, prefix: Optional[Path],
              home: Path) -> Optional[Path]:
    for a in cmd:
        if a.lower().startswith('-userdir='):
            val = a.split('=', 1)[1].strip().strip('"')
            d = _wine_to_host(val, prefix) if prefix else Path(val)
            return d / 'Saved' if d else None
    project = _project_name(install_dir)
    if project is None:
        return None
    if prefix is None:                     # native Linux build
        return home / '.config/Epic' / project / 'Saved'
    return prefix / 'drive_c/users/steamuser/AppData/Local' / project / 'Saved'


def _edit_ini(path: Path, values: Optional[dict]):
    """Set *values* in SECTION (or, values None, remove our keys from it)."""
    lines = path.read_text(errors='replace').splitlines() if path.exists() else []
    keys = ('ApplicationScale', 'UIScaleRule')
    out, in_sec, found_sec = [], False, False
    for ln in lines:
        s = ln.strip()
        if s.startswith('[') and s.endswith(']'):
            if in_sec and values:
                at = len(out)
                while at > 0 and not out[at - 1].strip():
                    at -= 1                # before the blank line(s)
                out[at:at] = [f'{k}={v}' for k, v in values.items()]
                values = {}
            in_sec = s[1:-1] == SECTION
            found_sec = found_sec or in_sec
        elif in_sec and s.split('=', 1)[0].strip() in keys:
            continue                       # replaced (or removed)
        out.append(ln)
    if values:
        if not found_sec:
            out.extend(['', f'[{SECTION}]'] if out else [f'[{SECTION}]'])
        out.extend(f'{k}={v}' for k, v in values.items())
    if not out and not path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(out) + '\n')


def apply(script, cmd: List[str], install_dir: Path,
          prefix: Optional[str], home: str) -> bool:
    """Write (or clear) the UI settings in the instance's Engine.ini. Returns
    True if something was written."""
    if script is None or not script.get('unrealUiScale'):
        return False
    s = scale(script)
    # With the per-screen fit deployed it grows the UI from the game's own
    # size as far as each screen fits; sizing by height on top made narrow
    # tiles start too big (menus overlapping at 1.0, Far Far West 960x1080).
    by_height = bool(script.options.get(BY_HEIGHT_OPTION, True)) and not fit_needed(script)
    wanted = s > 1.0 + 1e-6 or by_height
    saved = saved_dir(cmd, install_dir, Path(prefix) if prefix else None, Path(home))
    if saved is None:
        if wanted:
            script._user_message('warning', 'UI scale not applied: could not find '
                                 'the Unreal project (no <Project>/Binaries/'
                                 '{Win64,Linux}/*-Shipping in the install).')
        return False
    config = saved / 'Config'
    names = ('Linux', 'LinuxNoEditor') if prefix is None else ('Windows', 'WindowsNoEditor')
    dirs = [config / n for n in names if (config / n).is_dir()]
    if not dirs:
        dirs = [config / n for n in names]
    values = {}
    if by_height:
        values['UIScaleRule'] = 'Vertical'
    if s > 1.0 + 1e-6 and not script.get('unrealUiFit'):
        values['ApplicationScale'] = f'{s:g}'
    values = values or None
    for d in dirs:
        ini = d / 'Engine.ini'
        if values is None and not ini.exists():
            continue
        script._api_backup(str(ini))       # restored at session end
        _edit_ini(ini, values)
    if values:
        print(f'[janus] Unreal UI: {", ".join(f"{k}={v}" for k, v in values.items())} -> '
              f'{", ".join(str(d / "Engine.ini") for d in dirs)}', flush=True)
    return bool(values)

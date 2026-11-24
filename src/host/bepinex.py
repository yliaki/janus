"""BepInEx 5 deployment for Unity (Mono) games under Proton.

Two handler sheet fields turn it on (SCRIPTING.md):

- ``Game.unityUiScale = true`` declares that the game supports Janus's UI
  scaling. The handler then gets a "UI scale" option (1.0 ... 2.5, step 0.1)
  automatically; above 1.0 Janus deploys BepInEx + the JanusUiScale plugin
  (``src/unity_uiscale/``, prebuilt in ``vendor/unity_uiscale/``) and passes
  the factor as ``JANUS_UI_SCALE``.
- ``Game.unityUiScaleGroups = true`` (experimental): when a canvas can't
  take the whole factor, the plugin grows its separate element groups (HUD
  corners) individually toward the screen centre (``JANUS_UI_GROUPS=1``).
- ``Game.unityUiScaleDisable = ["ScriptName", ...]``: game MonoBehaviours to
  switch off on UI canvases because they keep re-positioning UI and undo the
  fit (``JANUS_UI_DISABLE``).
- ``Game.unityUiScaleIgnore = ["ObjectName", ...]``: UI GameObjects (and their
  children) that never limit the fit — markers pinned to the screen edge,
  decorations hanging off screen (``JANUS_UI_IGNORE``).
- ``Game.bepinexPlugins = ["MyFix.dll", ...]``: extra BepInEx plugins, paths
  relative to the handler's payload dir (or absolute). Always deployed.
- ``Game.resizeGame = "unity"`` (dynamic players, resize.py): the plugin is
  deployed even at UI scale 1.0 and keeps the game at the size Janus writes
  to ``$JANUS_SIZE_FILE`` (its tile, re-flowed as players join and leave).

Nothing is deployed when neither applies, so the game launches exactly as
without Janus's Unity support. Files go next to the game exe in the
per-instance mirror, never into the real install; the mirror is rebuilt every
launch, so BepInEx's config/cache/log start fresh each time.
"""

import os
import shutil
from pathlib import Path
from typing import List, Optional, Tuple

_REPO = Path(__file__).resolve().parent.parent.parent
BEPINEX_DIR = _REPO / 'vendor' / 'bepinex' / 'win_x64'
UI_SCALE_PLUGIN = _REPO / 'vendor' / 'unity_uiscale' / 'JanusUiScale.dll'

# The auto-generated handler option (Game.unityUiScale).
UI_SCALE_OPTION = 'uiScale'
UI_SCALE_CHOICES = [f'{i / 10:.1f}' for i in range(10, 26)]   # "1.0" .. "2.5"


def ui_scale_option_spec() -> dict:
    return {
        'label': 'UI scale',
        'type': 'choice',
        'choices': list(UI_SCALE_CHOICES),
        'default': '1.0',
        'description': 'Makes the game\'s menus and HUD bigger (Unity UI, via '
                       'BepInEx). Each menu is shrunk back as far as needed to '
                       'stay on screen, never below the game\'s own size. '
                       '1.0 = off: nothing is installed.',
    }


def plan(script) -> Tuple[float, List[Path]]:
    """(ui_scale, extra plugin paths) the handler asks for; ui_scale 1.0 = off."""
    if script is None:
        return 1.0, []
    scale = 1.0
    if script.get('unityUiScale'):
        try:
            scale = float(script.options.get(UI_SCALE_OPTION) or 1.0)
        except (TypeError, ValueError):
            scale = 1.0
    plugins = []
    from scripting import payload_dir_for
    for p in script.get('bepinexPlugins') or []:
        path = Path(os.path.expanduser(str(p)))
        if not path.is_absolute():
            path = payload_dir_for(script.script_path) / path
        plugins.append(path)
    return scale, plugins


def runs_in_background(script) -> bool:
    """Game.unityRunInBackground: the plugin forces Application.runInBackground
    (a Unity game pauses while its tile hasn't the desktop's focus)."""
    return script is not None and bool(script.get('unityRunInBackground'))


def resizes(script) -> bool:
    """Game.resizeGame = "unity": the plugin resizes the game to its tile."""
    return script is not None and script.get('resizeGame') == 'unity'


def needed(script) -> bool:
    scale, plugins = plan(script)
    return (scale > 1.0 + 1e-6 or bool(plugins) or resizes(script)
            or runs_in_background(script))


def _materialize(root: Path, rel: Path) -> Path:
    """Make every directory on root/rel a real directory (a symlinked one is
    replaced by a real dir of symlinks to its children), so files written
    there land in the mirror and not in the install it points to."""
    cur = root
    for part in rel.parts:
        nxt = cur / part
        if nxt.is_symlink() and nxt.is_dir():
            target = nxt.resolve()
            nxt.unlink()
            nxt.mkdir()
            for child in target.iterdir():
                (nxt / child.name).symlink_to(child)
        cur = nxt
    return cur


def _unity_data_dir(exe: Path) -> Optional[Path]:
    data = exe.with_name(exe.stem + '_Data')
    if data.is_dir():
        return data
    for d in sorted(exe.parent.glob('*_Data')):
        if d.is_dir():
            return d
    return None


def deploy(script, mirror_root: Path, exe: Path, env: dict,
           is_proton: bool) -> bool:
    """Install BepInEx (+ plugins) next to *exe* (inside *mirror_root*) and
    set the env it needs. Returns True if deployed; problems are reported to
    the user (Janus.warn) and the game launches without it."""
    scale, plugins = plan(script)
    use_ui = scale > 1.0 + 1e-6
    resize = resizes(script)
    background = runs_in_background(script)
    if not use_ui and not plugins and not resize and not background:
        return False

    def skip(why):
        script._user_message('warning', f'Unity mods not installed: {why}')
        return False

    if not is_proton:
        return skip('only Proton (Windows) Unity games are supported for now.')
    try:
        # Unresolved on purpose: the exe dir may be a symlink into the install.
        rel_dir = Path(os.path.abspath(exe.parent)).relative_to(
            os.path.abspath(mirror_root))
    except ValueError:
        return skip(f'the game exe {exe} is not inside the mirror.')
    data = _unity_data_dir(exe)
    if data is None:
        return skip(f'no <game>_Data folder next to {exe.name} — not a Unity game?')
    if not (data / 'Managed').is_dir() or (exe.parent / 'GameAssembly.dll').exists():
        return skip('this is an IL2CPP Unity game; BepInEx 5 needs a Mono one.')
    for p in plugins:
        if not p.is_file():
            return skip(f'plugin {p} not found.')
    if (use_ui or resize or background) and not UI_SCALE_PLUGIN.is_file():
        return skip(f'{UI_SCALE_PLUGIN} is missing (rebuild with '
                    f'src/unity_uiscale/build.sh).')

    exe_dir = _materialize(mirror_root, rel_dir)
    bep = exe_dir / 'BepInEx'
    if bep.is_symlink():
        # The player installed BepInEx in the real game: use theirs, as a real
        # copy (BepInEx writes config/cache/logs into it).
        src = bep.resolve()
        bep.unlink()
        shutil.copytree(src, bep, symlinks=False)
        print(f'[janus] BepInEx: using the game\'s own install ({src})', flush=True)
    else:
        shutil.copytree(BEPINEX_DIR / 'BepInEx', bep, dirs_exist_ok=True)
    for name in ('winhttp.dll', 'doorstop_config.ini', '.doorstop_version'):
        dst = exe_dir / name
        if dst.is_symlink() or dst.exists():
            continue                     # the game's own Doorstop
        shutil.copy2(BEPINEX_DIR / name, dst)
    pdir = bep / 'plugins'
    pdir.mkdir(parents=True, exist_ok=True)
    for p in ([UI_SCALE_PLUGIN] if use_ui or resize or background else []) + plugins:
        shutil.copy2(p, pdir / p.name)

    overrides = env.get('WINEDLLOVERRIDES', '')
    if 'winhttp' not in overrides:
        env['WINEDLLOVERRIDES'] = (f'{overrides};winhttp=n,b' if overrides
                                   else 'winhttp=n,b')
    if use_ui:
        env['JANUS_UI_SCALE'] = f'{scale:g}'
        if script.get('unityUiScaleGroups'):
            env['JANUS_UI_GROUPS'] = '1'     # experimental per-group scaling
        ignore = script.get('unityUiScaleIgnore') or []
        if ignore:
            env['JANUS_UI_IGNORE'] = ','.join(str(n) for n in ignore)
        disable = script.get('unityUiScaleDisable') or []
        if disable:
            env['JANUS_UI_DISABLE'] = ','.join(str(n) for n in disable)
    if background:
        env['JANUS_RUN_IN_BACKGROUND'] = '1'
    if resize:
        env['JANUS_RESIZE'] = '1'
    extras = [w for w, on in (('resizing', resize), ('run in background', background)) if on]
    names = (['JanusUiScale (UI scale ' + f'{scale:g}' + ')'] if use_ui
             else ['JanusUiScale (' + ', '.join(extras) + ')'] if extras else []) \
        + [p.name for p in plugins]
    print(f'[janus] BepInEx -> {exe_dir}: {", ".join(names)}', flush=True)
    return True

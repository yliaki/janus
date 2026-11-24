"""Add Janus's Big Picture launcher to Steam as a non-Steam game.

Run through scripts/install_steam_shortcut.sh, which stops Steam first and
restarts it afterwards: Steam reads shortcuts.vdf only at startup and writes
its in-memory copy back on exit, so an edit made while it runs is lost.

For every Steam user on the machine (userdata/<id>/, any Steam root):
  - shortcuts.vdf: adds the shortcut, or updates the one already pointing at
    run_bigpicture.sh (found by its Exe, so a moved checkout is updated, not
    duplicated; its appid is kept so playtime and custom artwork survive).
  - localconfig.vdf: apps/<signed appid>/OverlayAppEnable = "0". That is
    where the Steam UI's "Enable Steam Overlay while in-game" toggle lives;
    the shortcut's own AllowOverlay flag is set to 0 too, but the client
    doesn't seem to read it (it stays 1 when the toggle is switched off).
  - config/grid/: artwork from src/bigpicture/assets/steam/, when present
    (see the README there). Files the folder doesn't have are left alone,
    so artwork set by hand in Steam isn't overwritten.
Both .vdf files are backed up to <name>.janus-bak before being written.
"""

import argparse
import re
import shutil
import subprocess
import sys
import zlib
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT / 'src' / 'host'))

from steam import find_all_steam_roots  # noqa: E402
from src.bigpicture.steam_shortcut import _dump_binary_vdf, _parse_binary_vdf  # noqa: E402

LAUNCHER = _ROOT / 'scripts' / 'run_bigpicture.sh'
ARTWORK_DIR = Path(__file__).resolve().parent / 'assets' / 'steam'
# artwork file stem -> grid file name after the (unsigned) appid
ARTWORK = {
    'cover': 'p',         # 600x900  library portrait capsule
    'capsule': '',        # 920x430  wide capsule (recent games, Big Picture)
    'hero': '_hero',      # 3840x1240 (or 1920x620) banner on the game page
    'logo': '_logo',      # transparent logo drawn over the hero
    'icon': '_icon',      # square icon (also the shortcut's "icon" field)
}
IMAGE_EXTS = ('.png', '.jpg')


def steam_running() -> bool:
    return subprocess.run(['pgrep', '-x', 'steam'], stdout=subprocess.DEVNULL).returncode == 0


def user_dirs():
    """userdata/<accountid>/ of every Steam user, every root, deduplicated."""
    seen, out = set(), []
    for root in find_all_steam_roots():
        for d in sorted(Path(root, 'userdata').glob('*')):
            if not (d.is_dir() and d.name.isdigit() and d.name != '0'):
                continue
            key = d.resolve()
            if key not in seen:
                seen.add(key)
                out.append(d)
    return out


def _signed(appid: int) -> int:
    return appid - (1 << 32) if appid >= 1 << 31 else appid


def _unsigned(appid: int) -> int:
    return appid & 0xFFFFFFFF


def new_appid(exe: str, name: str) -> int:
    """Steam's own scheme for shortcut ids: crc32(exe + name) | 0x80000000
    (signed, as stored in shortcuts.vdf)."""
    return _signed(zlib.crc32((exe + name).encode('utf-8')) | 0x80000000)


def _is_ours(entry: dict) -> bool:
    exe = str(entry.get('Exe', entry.get('exe', ''))).strip('"')
    return Path(exe).name == LAUNCHER.name


def _write(path: Path, data: bytes):
    """Back up *path* (if it exists) and replace it atomically."""
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + '.janus-bak'))
    tmp = path.with_name(path.name + '.janus-tmp')
    tmp.write_bytes(data)
    tmp.replace(path)


def _load_shortcuts(path: Path) -> dict:
    if not path.exists():
        return {'shortcuts': {}}
    doc, _ = _parse_binary_vdf(path.read_bytes())
    doc.setdefault('shortcuts', {})
    return doc


# --- localconfig.vdf (text VDF, edited in place) ----------------------------

_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|([{}])|//[^\n]*')


def _find_block(text: str, path):
    """(start, end) of the body of the block at key *path* — start just
    after its '{', end at its '}' — or None."""
    stack, key, start = [], None, None
    for m in _TOKEN.finditer(text):
        s, brace = m.group(1), m.group(2)
        if brace == '{':
            stack.append(key)
            key = None
            if stack == path:
                start = m.end()
        elif brace == '}':
            if stack == path and start is not None:
                return start, m.start()
            if stack:
                stack.pop()
            key = None
        elif s is not None:
            key = s if key is None else None
    return None


def _indent_before(text: str, pos: int) -> str:
    line = text[text.rfind('\n', 0, pos) + 1:pos]
    return line[:len(line) - len(line.lstrip('\t '))]


def disable_overlay(text: str, appid: int) -> str | None:
    """localconfig.vdf text with apps/<appid>/OverlayAppEnable = "0", or None
    when it has no UserLocalConfigStore/apps block."""
    apps = ['UserLocalConfigStore', 'apps']
    entry = _find_block(text, apps + [str(_signed(appid))])
    if entry:
        start, end = entry
        m = re.search(r'"OverlayAppEnable"(\s*)"[^"]*"', text[start:end])
        if m:
            a, b = start + m.start(), start + m.end()
            return text[:a] + f'"OverlayAppEnable"{m.group(1)}"0"' + text[b:]
        ind = _indent_before(text, start - 1) + '\t'
        return text[:start] + f'\n{ind}"OverlayAppEnable"\t\t"0"' + text[start:]
    block = _find_block(text, apps)
    if not block:
        return None
    start = block[0]
    ind = _indent_before(text, start - 1) + '\t'
    return (text[:start] + f'\n{ind}"{_signed(appid)}"\n{ind}{{\n'
            f'{ind}\t"OverlayAppEnable"\t\t"0"\n{ind}}}' + text[start:])


# --- artwork ----------------------------------------------------------------

def _artwork_sources() -> dict:
    out = {}
    for stem in ARTWORK:
        for ext in IMAGE_EXTS:
            f = ARTWORK_DIR / (stem + ext)
            if f.is_file():
                out[stem] = f
                break
    return out


def install_artwork(userdir: Path, appid: int, sources: dict) -> Path | None:
    """Copy the artwork into config/grid/; returns the installed icon path."""
    grid = userdir / 'config' / 'grid'
    icon = None
    for stem, src in sources.items():
        grid.mkdir(parents=True, exist_ok=True)
        base = f'{_unsigned(appid)}{ARTWORK[stem]}'
        for ext in IMAGE_EXTS:  # Steam picks either; don't leave a stale one
            (grid / (base + ext)).unlink(missing_ok=True)
        dest = grid / (base + src.suffix)
        shutil.copyfile(src, dest)
        if stem == 'icon':
            icon = dest
    return icon


def remove_artwork(userdir: Path, appid: int):
    grid = userdir / 'config' / 'grid'
    for suffix in ARTWORK.values():
        for ext in IMAGE_EXTS:
            (grid / f'{_unsigned(appid)}{suffix}{ext}').unlink(missing_ok=True)


# --- install / remove -------------------------------------------------------

def install(userdir: Path, name: str, sources: dict):
    cfg = userdir / 'config'
    cfg.mkdir(parents=True, exist_ok=True)
    path = cfg / 'shortcuts.vdf'
    doc = _load_shortcuts(path)
    shortcuts = doc['shortcuts']
    exe = f'"{LAUNCHER}"'

    entry = next((e for e in shortcuts.values() if isinstance(e, dict) and _is_ours(e)), None)
    if entry is None:
        appid = new_appid(exe, name)
        entry = {
            'appid': appid, 'AppName': name, 'Exe': exe, 'StartDir': '', 'icon': '',
            'ShortcutPath': '', 'LaunchOptions': '', 'IsHidden': 0,
            'AllowDesktopConfig': 1, 'AllowOverlay': 0, 'OpenVR': 0, 'Devkit': 0,
            'DevkitGameID': '', 'DevkitOverrideAppID': 0, 'LastPlayTime': 0,
            'FlatpakAppID': '', 'sortas': '', 'tags': {},
        }
        shortcuts[str(len(shortcuts))] = entry
        action = 'added'
    else:
        appid = entry.get('appid') or new_appid(exe, name)
        action = 'updated'
    entry.update({'appid': appid, 'AppName': name, 'Exe': exe,
                  'StartDir': f'"{LAUNCHER.parent}/"', 'AllowOverlay': 0})

    icon = install_artwork(userdir, appid, sources)
    if icon:
        entry['icon'] = str(icon)
    _write(path, _dump_binary_vdf(doc))

    overlay = 'overlay off'
    lc = cfg / 'localconfig.vdf'
    if lc.exists():
        text = disable_overlay(lc.read_text(encoding='utf-8', errors='surrogateescape'), appid)
        if text is None:
            overlay = 'overlay NOT changed (unexpected localconfig.vdf) — turn it off in Properties'
        else:
            _write(lc, text.encode('utf-8', errors='surrogateescape'))
    art = ', '.join(sources) if sources else 'no artwork'
    print(f'  {userdir}: {action} "{name}" (appid {_unsigned(appid)}; {overlay}; {art})')


def remove(userdir: Path):
    path = userdir / 'config' / 'shortcuts.vdf'
    if not path.exists():
        return
    doc = _load_shortcuts(path)
    entries = [e for e in doc['shortcuts'].values() if isinstance(e, dict)]
    ours = [e for e in entries if _is_ours(e)]
    if not ours:
        return
    for e in ours:
        if e.get('appid'):
            remove_artwork(userdir, e['appid'])
    doc['shortcuts'] = {str(i): e for i, e in enumerate(e for e in entries if not _is_ours(e))}
    _write(path, _dump_binary_vdf(doc))
    print(f'  {userdir}: removed {len(ours)} shortcut(s)')


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--name', default='Janus Split Screen', help='name shown in Steam (default: Janus Split Screen)')
    ap.add_argument('--remove', action='store_true', help='remove the shortcut instead')
    args = ap.parse_args(argv)

    if steam_running():
        print('Steam is running: close it first (Steam would overwrite the change).',
              file=sys.stderr)
        return 1
    users = user_dirs()
    if not users:
        print('No Steam user found (log in to Steam once first).', file=sys.stderr)
        return 1
    sources = {} if args.remove else _artwork_sources()
    for d in users:
        if args.remove:
            remove(d)
        else:
            install(d, args.name, sources)
    return 0


if __name__ == '__main__':
    sys.exit(main())

#!/usr/bin/env python3
"""package.py — build the Janus Split Screen release packages into dist/.

    dist/janus-split-screen-<version>.zip            per-user install, any distro
    dist/janus-split-screen_<version>_amd64.deb      Debian/Ubuntu, deps from apt

The zip holds app/ (the program, with prebuilt hooks and a bundled PySide6
in app/pylib so it runs where the distro has no PySide6, e.g. SteamOS),
install.sh, uninstall.sh and README.txt. The .deb installs the same program
(without pylib) to /usr/lib/janus-split-screen and takes Qt from apt.

Which files ship: everything git knows (tracked + untracked, not ignored)
under src/ vendor/ scripts/ packaging/ plus a few docs, so build leftovers
never leak in. No handlers/: users get them from the hub. An installed copy
is recognised by its VERSION file (scripting.INSTALLED: only the user's
handlers count).

Usage:
    scripts/package.py                 # both
    scripts/package.py --zip           # only the zip
    scripts/package.py --deb           # only the .deb
    scripts/package.py --no-build      # use build/libjanus*.so as they are
    scripts/package.py --version 0.2.0 # base version (default: version.BASE)

Needs gcc + gcc-multilib (hooks), dpkg-deb (the .deb; no root or fakeroot) and,
for the first zip, internet access to pypi.org (wheels are cached in dist/cache/).
"""

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'src' / 'host'))
from version import BASE as BASE_VERSION  # noqa: E402
DIST = ROOT / 'dist'
CACHE = DIST / 'cache'
NAME = 'janus-split-screen'

# What ships, relative to the repo root.
# No handlers/: users get handlers from the hub (src/host/hub.py).
SHIP_DIRS = ['src', 'vendor', 'scripts', 'packaging', 'LICENSES']
SHIP_FILES = ['LICENSE', 'README.md', 'SCRIPTING.md', 'AGENTS_WRITE_HANDLERS.md']
HOOKS = ['build/libjanus64.so', 'build/libjanus32.so', 'build/libjanus_gamescope.so']
# gbe_fork writes these next to its .so at runtime (also in .gitignore).
SKIP_PARTS = {'__pycache__', 'userdata'}
SKIP_NAMES = {'EMU_MISSING_INTERFACE.txt'}

# Bundled Python packages for the zip: (PyPI name, version, wheel tag suffix).
# PySide6-Essentials has QtCore/Gui/Widgets/Qml/Quick + the Wayland plugin;
# 6.11 wheels need Python >= 3.10 and glibc >= 2.34.
WHEELS = [
    ('shiboken6', '6.11.2', 'manylinux_2_34_x86_64.whl'),
    ('PySide6-Essentials', '6.11.2', 'manylinux_2_34_x86_64.whl'),
    ('python-xlib', '0.33', 'py2.py3-none-any.whl'),
    ('six', '1.17.0', 'py2.py3-none-any.whl'),
]

DEB_DEPENDS = [
    'python3 (>= 3.10)',
    'python3-pyside6.qtcore', 'python3-pyside6.qtgui', 'python3-pyside6.qtwidgets',
    'python3-pyside6.qtqml', 'python3-pyside6.qtquick',
    'qml6-module-qtquick', 'qml6-module-qtquick-window', 'qt6-wayland',
    'libsdl2-2.0-0', 'libkscreen-bin', 'qdbus-qt6', 'dbus-bin',
]
# gamescope is only in trixie-backports (contrib) on Debian 13.
# debian/copyright (machine-readable format). Licence texts: the ones Debian
# ships in /usr/share/common-licenses, the rest in LICENSES/.
DEB_COPYRIGHT = """\
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: Janus Split Screen
Comment: Overview of every bundled component: {lib}/LICENSES/README.md

Files: *
Copyright: Janus Split Screen authors
License: GPL-3.0-or-later
 See /usr/share/common-licenses/GPL-3 (also {lib}/LICENSE).

Files: vendor/goldberg/*
Copyright: gbe_fork authors (https://github.com/Detanup01/gbe_fork)
License: LGPL-3.0
 See /usr/share/common-licenses/LGPL-3 and GPL-3. Libraries built into these
 binaries: {lib}/LICENSES/gbe_fork-CREDITS.md

Files: vendor/steamless/*
Copyright: atom0s (https://github.com/atom0s/Steamless); SharpDisasm: Justin Stenning, udis86: Vivek Thampi
License: CC-BY-NC-ND-4.0
 Steamless: https://creativecommons.org/licenses/by-nc-nd/4.0/ ; SharpDisasm.dll
 is BSD-2-Clause, see {lib}/LICENSES/SharpDisasm-LICENSE.md

Files: vendor/bepinex/*
Copyright: BepInEx, HarmonyX, MonoMod, Mono.Cecil and UnityDoorstop authors
License: MIT and LGPL-2.1
 See {lib}/vendor/bepinex/licenses/ and /usr/share/common-licenses/LGPL-2.1

Files: vendor/ue4ss/*
Copyright: 2022 Narknon and the RE-UE4SS contributors
License: MIT
 See {lib}/vendor/ue4ss/win64/ue4ss/LICENSE

Files: vendor/quickjs/*
Copyright: quickjs-ng authors
License: MIT
 See {lib}/vendor/quickjs/quickjs_ng-0.16.0.1.dist-info/licenses/LICENSE
"""

DEB_RECOMMENDS = ['gamescope', 'python3-xlib', 'psmisc']
DEB_SUGGESTS = ['xdotool', 'x11-utils']


def run(cmd, **kw):
    print('+', ' '.join(str(c) for c in cmd), flush=True)
    return subprocess.run(cmd, check=True, **kw)


def git(*args) -> str:
    return subprocess.run(['git', '-C', str(ROOT), *args], check=True,
                          capture_output=True, text=True).stdout


def version_string(base: str) -> str:
    """A release: exactly <base>, when HEAD carries the tag v<base> and the
    tree is clean (scripts/release.py makes it). Anything else:
    <base>+git<date>.<hash>[.dirty], which sorts after <base> for dpkg too."""
    dirty = '.dirty' if git('status', '--porcelain').strip() else ''
    if not dirty and f'v{base}' in git('tag', '--points-at', 'HEAD').split():
        return base
    date = time.strftime('%Y%m%d', time.gmtime(int(git('log', '-1', '--format=%ct').strip())))
    commit = git('rev-parse', '--short', 'HEAD').strip()
    return f'{base}+git{date}.{commit}{dirty}'


def ship_files() -> list[Path]:
    """Repo-relative paths of everything that goes into the program dir."""
    out = git('ls-files', '-z', '--cached', '--others', '--exclude-standard',
              '--', *SHIP_DIRS, *SHIP_FILES)
    files = []
    for rel in sorted(set(filter(None, out.split('\0')))):
        p = Path(rel)
        if SKIP_PARTS & set(p.parts) or p.name in SKIP_NAMES:
            continue
        if not (ROOT / p).is_file() and not (ROOT / p).is_symlink():
            continue   # deleted in the working tree
        files.append(p)
    return files


def build_hooks(skip: bool):
    if not skip:
        run([str(ROOT / 'scripts' / 'compile_hooks.sh'), 'all'])
    missing = [h for h in HOOKS if not (ROOT / h).is_file()]
    if missing:
        sys.exit(f'error: missing {", ".join(missing)} (run scripts/compile_hooks.sh)')


def stage_app(dest: Path, version: str):
    """The program tree, as installed."""
    if dest.exists():
        shutil.rmtree(dest)
    for rel in ship_files() + [Path(h) for h in HOOKS]:
        src, dst = ROOT / rel, dest / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_symlink():
            dst.symlink_to(os.readlink(src))
        else:
            shutil.copy2(src, dst)
    (dest / 'VERSION').write_text(version + '\n')


# --- bundled Python packages (zip) ------------------------------------------

def _download(url: str, tmp: Path, size: int, attempts: int = 8) -> None:
    """Download *url* to *tmp*, resuming (HTTP Range) when the connection drops:
    PyPI's CDN cut the 80 MB PySide6 wheel short (ContentTooShortError)."""
    for attempt in range(1, attempts + 1):
        have = tmp.stat().st_size if tmp.exists() else 0
        if have > size:
            tmp.unlink()
            have = 0
        if have == size:
            return
        req = urllib.request.Request(url, headers={'Range': f'bytes={have}-'} if have else {})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                if have and r.status != 206:      # server ignored the range
                    have = 0
                with open(tmp, 'ab' if have else 'wb') as out:
                    shutil.copyfileobj(r, out, 1 << 20)
        except (OSError, urllib.error.URLError) as e:
            print(f'  download interrupted ({e}); retrying ({attempt}/{attempts})',
                  flush=True)
            time.sleep(min(2 * attempt, 10))
            continue
        if tmp.stat().st_size == size:
            return
        print(f'  got {tmp.stat().st_size} of {size} bytes; resuming '
              f'({attempt}/{attempts})', flush=True)
    sys.exit(f'error: could not download {url} after {attempts} attempts')


def fetch_wheel(name: str, version: str, suffix: str) -> Path:
    """Download (once) and sha256-check a wheel from PyPI."""
    wheels = CACHE / 'wheels'
    wheels.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(f'https://pypi.org/pypi/{name}/{version}/json') as r:
        meta = json.load(r)
    files = [f for f in meta['urls'] if f['filename'].endswith(suffix)]
    if not files:
        sys.exit(f'error: no {suffix} wheel for {name} {version} on PyPI')
    f = files[0]
    path = wheels / f['filename']
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != f['digests']['sha256']:
        print(f'Downloading {f["filename"]} ({f["size"] // 2**20} MB) ...', flush=True)
        tmp = path.with_suffix('.part')
        _download(f['url'], tmp, int(f['size']))
        if hashlib.sha256(tmp.read_bytes()).hexdigest() != f['digests']['sha256']:
            tmp.unlink()
            sys.exit(f'error: sha256 mismatch for {f["filename"]}')
        tmp.rename(path)
    return path


def build_pylib() -> Path:
    """dist/cache/pylib-<key>/: the WHEELS unpacked side by side (a wheel is a
    zip laid out as site-packages). Only extracted, never executed."""
    key = hashlib.sha256(repr(WHEELS).encode()).hexdigest()[:12]
    pylib = CACHE / f'pylib-{key}'
    if (pylib / '.complete').is_file():
        return pylib
    if pylib.exists():
        shutil.rmtree(pylib)
    for name, version, suffix in WHEELS:
        whl = fetch_wheel(name, version, suffix)
        with zipfile.ZipFile(whl) as z:
            for info in z.infolist():
                target = (pylib / info.filename).resolve()
                if not str(target).startswith(str(pylib.resolve()) + os.sep):
                    sys.exit(f'error: unsafe path {info.filename!r} in {whl.name}')
                z.extract(info, pylib)
                mode = info.external_attr >> 16
                if mode & 0o111:
                    os.chmod(target, 0o755)
    (pylib / '.complete').write_text('')
    return pylib


# --- zip ----------------------------------------------------------------------

def add_tree(z: zipfile.ZipFile, src: Path, arc: str):
    """Add *src* recursively under *arc*, keeping Unix modes and symlinks."""
    for p in sorted([src, *src.rglob('*')]):
        name = arc + ('/' + str(p.relative_to(src)) if p != src else '')
        st = p.lstat()
        if stat.S_ISLNK(st.st_mode):
            info = zipfile.ZipInfo(name, time.localtime(st.st_mtime)[:6])
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            z.writestr(info, os.readlink(p))
        elif p.is_dir():
            info = zipfile.ZipInfo(name + '/', time.localtime(st.st_mtime)[:6])
            info.external_attr = (stat.S_IFDIR | 0o755) << 16 | 0x10
            z.writestr(info, b'')
        elif p.name != '.complete':
            info = zipfile.ZipInfo.from_file(p, name)
            info.compress_type = zipfile.ZIP_DEFLATED
            exe = 0o755 if st.st_mode & 0o111 else 0o644
            info.external_attr = (stat.S_IFREG | exe) << 16
            with open(p, 'rb') as fh:
                z.writestr(info, fh.read(), compresslevel=9)


def build_zip(app: Path, version: str) -> Path:
    pylib = build_pylib()
    top = DIST / 'stage' / 'zip'
    if top.exists():
        shutil.rmtree(top)
    top.mkdir(parents=True)
    shutil.copy2(ROOT / 'packaging' / 'install.sh', top / 'install.sh')
    shutil.copy2(ROOT / 'packaging' / 'uninstall.sh', top / 'uninstall.sh')
    readme = (ROOT / 'packaging' / 'README.txt').read_text().replace('@VERSION@', version)
    (top / 'README.txt').write_text(readme)

    out = DIST / f'{NAME}-{version}.zip'
    tmp = out.with_suffix('.zip.part')
    base = f'{NAME}-{version}'
    print(f'Writing {out.name} ...', flush=True)
    with zipfile.ZipFile(tmp, 'w') as z:
        for f in ('README.txt', 'install.sh', 'uninstall.sh'):
            add_tree(z, top / f, f'{base}/{f}')
        add_tree(z, app, f'{base}/app')
        add_tree(z, pylib, f'{base}/app/pylib')
    tmp.rename(out)
    return out


# --- deb ----------------------------------------------------------------------

def normalise_modes(tree: Path):
    """Debian wants 0755 dirs/executables and 0644 files."""
    for p in [tree, *tree.rglob('*')]:
        if p.is_symlink():
            continue
        if p.is_dir() or p.stat().st_mode & 0o111:
            p.chmod(0o755)
        else:
            p.chmod(0o644)


def build_deb(app: Path, version: str) -> Path:
    root = DIST / 'stage' / 'deb'
    if root.exists():
        shutil.rmtree(root)
    lib = root / 'usr' / 'lib' / NAME
    shutil.copytree(app, lib, symlinks=True)

    bindir = root / 'usr' / 'bin'
    bindir.mkdir(parents=True)
    for cmd, script in (('janus', 'run_gui.sh'), ('janus-bigpicture', 'run_bigpicture.sh'),
                        ('janus-cli', 'run_cli.sh'),
                        ('janus-steam-shortcut', 'install_steam_shortcut.sh')):
        w = bindir / cmd
        w.write_text(f'#!/bin/sh\nexec /usr/lib/{NAME}/scripts/{script} "$@"\n')
        w.chmod(0o755)

    apps = root / 'usr' / 'share' / 'applications'
    apps.mkdir(parents=True)
    for d in (ROOT / 'packaging').glob('*.desktop'):
        (apps / d.name).write_text(d.read_text().replace('@BINDIR@', ''))
    icons = root / 'usr' / 'share' / 'icons' / 'hicolor' / 'scalable' / 'apps'
    icons.mkdir(parents=True)
    shutil.copy2(ROOT / 'src' / 'gui' / 'assets' / 'gamepad.svg', icons / f'{NAME}.svg')
    doc = root / 'usr' / 'share' / 'doc' / NAME
    doc.mkdir(parents=True)
    (doc / 'copyright').write_text(DEB_COPYRIGHT.format(lib=f'/usr/lib/{NAME}'))

    normalise_modes(root)
    size_kb = sum(p.lstat().st_size for p in root.rglob('*') if not p.is_dir()) // 1024
    md5 = []
    for p in sorted(root.rglob('*')):
        if p.is_file() and not p.is_symlink():
            md5.append(f'{hashlib.md5(p.read_bytes()).hexdigest()}  {p.relative_to(root)}')

    name = git('config', 'user.name').strip() or 'Janus'
    email = git('config', 'user.email').strip() or 'janus@localhost'
    ctrl = root / 'DEBIAN'
    ctrl.mkdir()
    (ctrl / 'control').write_text(
        f'Package: {NAME}\n'
        f'Version: {version}\n'
        'Architecture: amd64\n'
        f'Maintainer: {name} <{email}>\n'
        f'Installed-Size: {size_kb}\n'
        f'Depends: {", ".join(DEB_DEPENDS)}\n'
        f'Recommends: {", ".join(DEB_RECOMMENDS)}\n'
        f'Suggests: {", ".join(DEB_SUGGESTS)}\n'
        'Section: games\n'
        'Priority: optional\n'
        'Description: split-screen local multiplayer for Steam games\n'
        ' Runs one game instance per player, each in its own gamescope window\n'
        ' with only its own controller, on KDE Plasma (Wayland).\n')
    (ctrl / 'md5sums').write_text('\n'.join(md5) + '\n')
    for p in ctrl.iterdir():
        p.chmod(0o644)

    out = DIST / f'{NAME}_{version}_amd64.deb'
    run(['dpkg-deb', '--root-owner-group', '-Zxz', '--build', str(root), str(out)])
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--zip', action='store_true', help='build the zip')
    ap.add_argument('--deb', action='store_true', help='build the .deb')
    ap.add_argument('--version', default=BASE_VERSION,
                    help=f'base version (default {BASE_VERSION}, src/host/version.py)')
    ap.add_argument('--no-build', action='store_true', help="don't rebuild the hooks")
    args = ap.parse_args()
    both = not (args.zip or args.deb)

    version = version_string(args.version)
    print(f'Janus Split Screen {version}', flush=True)
    build_hooks(args.no_build)
    app = DIST / 'stage' / 'app'
    stage_app(app, version)

    built = []
    if args.zip or both:
        built.append(build_zip(app, version))
    if args.deb or both:
        if shutil.which('dpkg-deb'):
            built.append(build_deb(app, version))
        else:
            print('dpkg-deb not found, skipping the .deb', flush=True)
    print()
    for p in built:
        print(f'{p.relative_to(ROOT)}  ({p.stat().st_size / 2**20:.0f} MB)')


if __name__ == '__main__':
    main()

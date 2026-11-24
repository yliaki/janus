#!/usr/bin/env python3
"""Unit tests for the Steam Linux Runtime selection (steam.select_runtime) and
src/host/prereqs.py (gamescope + runtime checks). Uses a fake Steam root,
never the real one.

    python3 tests/test_prereqs.py
"""

import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'src' / 'host'))

import prereqs  # noqa: E402
import steam  # noqa: E402

fails = []


def check(cond, msg):
    print(('ok   ' if cond else 'FAIL ') + msg)
    if not cond:
        fails.append(msg)


class FakeGame:
    def __init__(self, root, proton=None, is_32bit=False):
        self.steam_root = root
        self.name = 'Game'
        self.is_proton = proton is not None
        self.proton_path = proton
        self.is_32bit = is_32bit


class FakeScript:
    def __init__(self, **sheet):
        self.sheet = sheet

    def get(self, field, default=None):
        return self.sheet.get(field, default)


def add_runtime(steamapps, appid, installdir):
    (steamapps / f'appmanifest_{appid}.acf').write_text(
        f'"AppState"\n{{\n\t"appid"\t\t"{appid}"\n\t"installdir"\t\t"{installdir}"\n}}\n')
    d = steamapps / 'common' / installdir
    d.mkdir(parents=True)
    entry = ('scout-on-soldier-entry-point-v2' if appid == steam.RUNTIME_SCOUT
             else '_v2-entry-point')
    (d / entry).write_text('')
    return d


with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp) / 'Steam'
    main = root / 'steamapps'
    lib2 = Path(tmp) / 'Library2' / 'steamapps'
    (main / 'common').mkdir(parents=True)
    (lib2 / 'common').mkdir(parents=True)
    (main / 'libraryfolders.vdf').write_text(
        f'"libraryfolders"\n{{\n\t"0"\t{{ "path" "{root}" }}\n'
        f'\t"1"\t{{ "path" "{lib2.parent}" }}\n}}\n')
    proton = main / 'common' / 'Proton - Experimental'
    proton.mkdir()
    (proton / 'toolmanifest.vdf').write_text(
        '"manifest"\n{\n  "require_tool_appid" "4183110"\n}\n')

    # Nothing installed.
    c = steam.select_runtime(FakeGame(root))
    check(c.wrapper == [] and c.missing == [steam.RUNTIME_SNIPER],
          'native, no runtimes: bare launch, sniper missing')
    c = steam.select_runtime(FakeGame(root, is_32bit=True))
    check(c.missing == [steam.RUNTIME_SCOUT, steam.RUNTIME_SNIPER],
          '32-bit native, no runtimes: scout + sniper missing')
    c = steam.select_runtime(FakeGame(root, proton=proton))
    check(c.wrapper == [] and c.missing == [4183110],
          'Proton, no runtimes: proton run directly, its runtime (SLR 4) missing')
    check(prereqs.runtime_warning(FakeGame(root)) is not None, 'warning when missing')
    check(prereqs.runtime_warning(FakeGame(root), FakeScript(useRuntime=False)) is None,
          'no warning with Game.useRuntime = false')
    check(prereqs.runtime_warning(FakeGame(root), FakeScript(runtime='none')) is None,
          "no warning with Game.runtime = 'none'")

    # Sniper in the SECOND library (Steam installs a runtime next to the game
    # that first needed it): must be found.
    sniper = add_runtime(lib2, steam.RUNTIME_SNIPER, 'SteamLinuxRuntime_sniper')
    check(steam.find_runtime(root, steam.RUNTIME_SNIPER) == sniper,
          'runtime found in a secondary library')
    c = steam.select_runtime(FakeGame(root))
    check(c.wrapper[:2] == [str(sniper / '_v2-entry-point'), '--verb=waitforexitandrun']
          and not c.missing, 'native: sniper from the secondary library')
    c = steam.select_runtime(FakeGame(root, proton=proton))
    check(c.wrapper[:2] == [str(sniper / '_v2-entry-point'), '--verb=run']
          and c.missing == [4183110], 'Proton: falls back to sniper, SLR 4 still missing')
    check(prereqs.runtime_warning(FakeGame(root)) is None, 'no warning when installed')

    # A runtime dir without its entry point (half-downloaded) doesn't count.
    (main / 'common' / 'SteamLinuxRuntime').mkdir()
    check(steam.find_runtime(root, steam.RUNTIME_SCOUT) is None,
          'runtime without entry point / manifest is not installed')
    c = steam.select_runtime(FakeGame(root, is_32bit=True))
    check(c.missing == [steam.RUNTIME_SCOUT] and c.wrapper,
          '32-bit native without scout: sniper, scout missing')
    c = steam.select_runtime(FakeGame(root), 'scout')
    check(c.missing == [steam.RUNTIME_SCOUT], "Game.runtime = 'scout' missing scout")

    scout = add_runtime(main, steam.RUNTIME_SCOUT, 'SteamLinuxRuntime_scout')
    c = steam.select_runtime(FakeGame(root, is_32bit=True))
    check(str(scout / 'scout-on-soldier-entry-point-v2') in c.wrapper and not c.missing,
          '32-bit native: sniper + scout-on-soldier')
    c = steam.select_runtime(FakeGame(root), 'soldier')
    check(c.missing == [steam.RUNTIME_SOLDIER] and c.wrapper,
          "Game.runtime = 'soldier' missing: reported, falls back to sniper")

    slr4 = add_runtime(main, 4183110, 'SteamLinuxRuntime_4')
    c = steam.select_runtime(FakeGame(root, proton=proton))
    check(c.wrapper[0] == str(slr4 / '_v2-entry-point') and not c.missing,
          "Proton: the runtime its toolmanifest asks for")

    check(sorted(prereqs.wanted_runtimes(root)) ==
          sorted([steam.RUNTIME_SNIPER, steam.RUNTIME_SCOUT, 4183110]),
          'wanted: sniper, scout + what installed Protons ask for')

    # Another app (a dedicated server) in the SECOND library: Janus.appInstallDir.
    real_roots = steam.find_all_steam_roots
    steam.find_all_steam_roots = lambda: [root]
    server = add_runtime(lib2, 2394010, 'PalServer')
    check(steam.find_app_install_dir(2394010) == server,
          'app install dir found in the second library')
    check(steam.find_app_install_dir(1) is None, 'uninstalled app: None')
    (lib2 / 'appmanifest_2.acf').write_text('"AppState"\n{\n\t"installdir"\t\t"Gone"\n}\n')
    check(steam.find_app_install_dir(2) is None, 'manifest without its dir: None')
    steam.find_all_steam_roots = real_roots

# gamescope install hints per distribution.
for osr, want in [
    ({'ID': 'debian', 'VERSION_CODENAME': 'trixie'}, 'trixie-backports'),
    ({'ID': 'kubuntu', 'ID_LIKE': 'ubuntu debian'}, 'multiverse'),
    ({'ID': 'endeavouros', 'ID_LIKE': 'arch'}, 'pacman -S gamescope'),
    ({'ID': 'fedora'}, 'dnf install gamescope'),
    ({'ID': 'opensuse-tumbleweed', 'ID_LIKE': 'opensuse suse'}, 'zypper'),
    ({'ID': 'gentoo'}, "distribution's package manager"),
]:
    prereqs._os_release = lambda osr=osr: osr
    check(want in prereqs.gamescope_install_hint(), f'hint for {osr["ID"]}: {want}')

prereqs._gamescope_cache.clear()
prereqs.find_gamescope = lambda: None
check("isn't installed" in (prereqs.gamescope_problem() or ''), 'missing gamescope reported')

# --- Linux vs Windows build (prereqs.build_problem) ---------------------------
class BuildScript:
    def __init__(self, hooks=(), non_steam=False, **sheet):
        self.sheet, self.hooks, self.non_steam = sheet, set(hooks), non_steam

    def get(self, k, d=None):
        return self.sheet.get(k, d)

    def has_hook(self, name):
        return name in self.hooks


class BuildGame:
    def __init__(self, d, appid=242680):
        self.install_dir, self.name, self.appid = Path(d), 'Nuclear Throne', appid


def elf(path):
    path.write_bytes(b'\x7fELF' + b'\0' * 60)
    path.chmod(0o755)


with tempfile.TemporaryDirectory() as t:
    win, lin, empty = Path(t) / 'win', Path(t) / 'lin', Path(t) / 'empty'
    for d in (win, lin, empty):
        d.mkdir()
    (win / 'nuclearthrone.exe').write_bytes(b'MZ')
    (win / 'data.win').write_bytes(b'')
    elf(lin / 'nuclearthrone')
    (lin / 'Setup.exe').write_bytes(b'MZ')      # a stray .exe doesn't make it Windows

    check(prereqs.installed_build(win) == 'windows', 'only .exe files: Windows build')
    check(prereqs.installed_build(lin) == 'linux', 'a native ELF: Linux build')
    check(prereqs.installed_build(empty) is None, 'nothing to tell: unknown')

    native = BuildScript(exeName='nuclearthrone')
    check(prereqs.expected_build(native) == 'linux', 'exeName without .exe: Linux')
    check(prereqs.expected_build(BuildScript(exeName='Game.exe')) == 'windows', 'exeName .exe: Windows')
    check(prereqs.expected_build(BuildScript(launchCommand='./jre/bin/java -jar X.jar')) == 'linux',
          'launchCommand: its first token decides')
    check(prereqs.expected_build(BuildScript(launchCommand='"bin/My Game.exe" -x')) == 'windows',
          'launchCommand .exe (quoted): Windows')
    check(prereqs.expected_build(BuildScript()) is None, 'no exe named: no expectation')
    check(prereqs.expected_build(BuildScript(hooks={'onGameResolved'}, exeName='x')) is None,
          'handler adapts itself (onGameResolved): no expectation')
    check(prereqs.expected_build(BuildScript(non_steam=True, exeName='x')) is None,
          'non-Steam handler: no expectation')

    prereqs.forced_compat_tool = lambda appid: 'proton_experimental'
    msg = prereqs.build_problem(BuildGame(win), native) or ''
    check('Linux build' in msg and 'Windows build' in msg, 'Linux handler + Windows install: refused')
    check('Proton Experimental' in msg and 'untick' in msg, 'names the forced tool, says to untick it')
    check(prereqs.build_problem(BuildGame(lin), native) is None, 'matching build: fine')
    msg = prereqs.build_problem(BuildGame(lin), BuildScript(exeName='Game.exe')) or ''
    check('tick "Force' in msg and 'Proton' in msg, 'Windows handler + Linux install: says to force Proton')
    check(prereqs.build_problem(BuildGame(empty), native) is None, 'unknown install: no complaint')
    check(prereqs.build_problem(BuildGame(win), native, exe_name='nuclearthrone.exe') is None,
          'an explicit --exe / GUI executable wins over the handler')
    check(prereqs._tool_label('proton_9') == 'Proton 9', 'proton_9 -> Proton 9')
    check(prereqs._tool_label('GE-Proton9-20') == 'GE-Proton9-20', 'custom tool names unchanged')

print(f'\n{"FAILED: " + str(len(fails)) if fails else "all passed"}')
sys.exit(1 if fails else 0)

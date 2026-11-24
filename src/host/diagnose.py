"""Diagnostics for Steam install + runtime + Proton detection.

Prints what `src/host/steam.py` would decide for a given setup. Run via::

    python3 src/host/main.py diagnose            # overview
    python3 src/host/main.py diagnose 550        # specific appid

Output is intentionally verbose and human-readable; this is a debugging aid,
not a parser target.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import List, Optional, Tuple

try:
    from . import steam  # type: ignore
except ImportError:  # script-style import (main.py does sys.path tricks)
    import steam  # type: ignore


def _hr(char: str = "─", width: int = 60) -> str:
    return char * width


def _yes(b: bool) -> str:
    return "yes" if b else "no"


def _exists_tag(path: Path) -> str:
    return "ok" if path.exists() else "MISSING"


def _disk_free(path: Path) -> Optional[str]:
    try:
        free = shutil.disk_usage(path).free
    except OSError:
        return None
    gb = free / (1024 ** 3)
    return f"{gb:.1f} GB free"


def _installed_runtimes(root: Path) -> List[Tuple[str, int, Path]]:
    """Return [(name, appid, path), ...] for the installed Steam Linux Runtimes."""
    out = []
    for appid, name in steam.RUNTIME_NAMES.items():
        path = steam.find_runtime(root, appid)
        if path:
            out.append((name, appid, path))
    return out


def print_overview() -> None:
    """Top-level diagnostic: roots, runtimes, environment overrides."""
    print(_hr("═"))
    print("Janus Split Screen — Steam detection diagnostics")
    print(_hr("═"))
    print()

    # Environment overrides
    print("Environment overrides:")
    for var in ('JANUS_STEAM_ROOT', 'STEAM_COMPAT_CLIENT_INSTALL_PATH'):
        val = os.environ.get(var)
        print(f"  {var}: {val if val else '(unset)'}")
    print()

    # Discovered roots
    try:
        roots = steam.find_all_steam_roots()
    except FileNotFoundError as e:
        print(f"  ✗ {e}")
        return

    if not roots:
        print("No Steam installation found. Searched native, Flatpak, and Snap.")
        print("Set JANUS_STEAM_ROOT to override.")
        return

    print(f"Discovered Steam root(s): {len(roots)}")
    for i, root in enumerate(roots):
        tag = " (PRIMARY)" if i == 0 else ""
        print()
        print(f"[{i}] {root}{tag}")
        free = _disk_free(root)
        if free:
            print(f"    {free}")

        libs = steam.steamapps_dirs(root)
        print(f"    Library folders ({len(libs)}):")
        for lib in libs:
            free = _disk_free(lib)
            note = f"  ({free})" if free else ""
            print(f"      - {lib}{note}")

        rts = _installed_runtimes(root)
        if rts:
            print(f"    Runtimes installed ({len(rts)}):")
            for name, appid, path in rts:
                print(f"      - {name:34s} appid {appid:<8} {path}")
        else:
            print("    Runtimes: none installed (will fall back to direct launch)")
        absent = [(a, n) for a, n in steam.RUNTIME_NAMES.items()
                  if all(a != r[1] for r in rts)]
        for appid, name in absent:
            print(f"      ✗ {name:34s} not installed  (steam://install/{appid})")

    # Installed games count (across all roots)
    try:
        games = steam.get_installed_games()
        print()
        print(f"Installed games across all roots: {len(games)}")
    except FileNotFoundError:
        pass

    print()
    print("Hint: pass an appid to diagnose a specific game")
    print("      e.g.  janus diagnose 550")


def _classify(game: steam.SteamGame) -> dict:
    """Mirror steam.py classification + record the reason."""
    result = {
        'is_native': game.is_native,
        'is_proton': game.is_proton,
        'is_32bit': game.is_32bit,
    }

    # Why-is-native: which heuristic fired?
    install = game.install_dir
    reasons = []
    if install.exists():
        for pattern in ('*.x86_64', '*.x86', '*.sh', 'start.sh', 'launch.sh'):
            if list(install.glob(pattern)):
                reasons.append(f"glob match: {pattern}")
                break
        else:
            if list(install.glob('*.so')) or list(install.glob('lib*/*.so*')):
                reasons.append("has .so libraries")
            elif steam._find_native_executables(install):
                try:
                    rel = steam.find_game_executable(game).relative_to(install)
                    reasons.append(f"nested native binary: {rel}")
                except (FileNotFoundError, ValueError, OSError):
                    reasons.append("nested native binary found")
    result['native_reason'] = reasons[0] if reasons else "no native files found"

    # Why-32bit
    bit_reasons = []
    if install.exists():
        for pattern in ('*.x86', '*_32', '*32.so'):
            if list(install.glob(pattern)):
                bit_reasons.append(f"glob match: {pattern}")
                break
        if (install / 'lib32').is_dir():
            bit_reasons.append("has lib32/ dir")
        for exe_name in ('hl2_linux', 'hl2.sh'):
            exe = install / exe_name
            if exe.exists():
                bit_reasons.append(f"known 32-bit binary: {exe_name}")
                break
        if not bit_reasons and result['is_32bit']:
            bit_reasons.append("ELF class of main executable (or its wrapper target)")
    result['bitness_reason'] = bit_reasons[0] if bit_reasons else "default (64-bit)"
    return result


def print_game(appid: int) -> None:
    print(_hr("═"))
    print(f"Game diagnostics: appid {appid}")
    print(_hr("═"))
    print()

    try:
        game = steam.SteamGame(appid)
        # Force the lazy lookups now so we can report errors cleanly.
        _ = game.library_path
    except FileNotFoundError as e:
        print(f"  ✗ {e}")
        # Show what we did find
        roots = steam.find_all_steam_roots()
        if roots:
            print(f"\n  Searched {len(roots)} root(s):")
            for r in roots:
                print(f"    - {r}")
        return

    print(f"  Name:        {game.name}")
    print(f"  Steam root:  {game.steam_root}")
    print(f"  Library:     {game.library_path}")
    install = game.install_dir
    print(f"  Install dir: {install}  [{_exists_tag(install)}]")
    print()

    cls = _classify(game)
    print("Classification:")
    if cls['is_proton']:
        print(f"  Platform: Proton  (compatdata present, no native files)")
    elif cls['is_native']:
        print(f"  Platform: native  ({cls['native_reason']})")
    else:
        print(f"  Platform: unknown  ({cls['native_reason']})")
    print(f"  Bitness:  {'32-bit' if cls['is_32bit'] else '64-bit'}  ({cls['bitness_reason']})")
    print()

    if cls['is_proton']:
        print("Proton:")
        proton = game.proton_path
        if proton:
            print(f"  Path: {proton}")
            print(f"  Name: {proton.name}")
        else:
            print("  ✗ Proton not resolved (config_info missing or unparsable)")
            print("    Run the game once via Steam to populate compatdata.")
        print()

    print("Runtime selection:")
    choice = steam.select_runtime(game)
    print(f"  Chosen:      {choice.label}")
    if choice.wrapper:
        print(f"  Entry point: {choice.wrapper[0]}")
    for appid in choice.missing:
        print(f"  ✗ Wanted {steam.runtime_name(appid)}: not installed "
              f"(steam://install/{appid})")
    print()

    print("Executable:")
    try:
        exe = steam.find_game_executable(game)
        print(f"  {exe}  [{_exists_tag(exe)}]")
    except FileNotFoundError as e:
        print(f"  ✗ {e}")
    print()


def run(appid: Optional[int]) -> int:
    if appid is None:
        print_overview()
    else:
        print_game(appid)
    return 0


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Diagnose Steam install + runtime detection')
    parser.add_argument('appid', type=int, nargs='?', help='Specific Steam appid to inspect')
    args = parser.parse_args()
    sys.exit(run(args.appid))

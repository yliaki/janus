"""Tests for Steam root + library discovery (Phase 1 + 2)."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.host import steam  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers for building fake Steam install trees
# ---------------------------------------------------------------------------

def make_steam_root(base: Path, *, with_libraryfolders: bool = True) -> Path:
    """Create a minimal valid Steam root at ``base``."""
    (base / 'steamapps').mkdir(parents=True, exist_ok=True)
    if with_libraryfolders:
        (base / 'steamapps' / 'libraryfolders.vdf').write_text(
            '"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t"' + str(base) + '"\n\t}\n}\n'
        )
    return base


def make_app_manifest(steamapps: Path, appid: int, name: str, installdir: str) -> Path:
    steamapps.mkdir(parents=True, exist_ok=True)
    path = steamapps / f'appmanifest_{appid}.acf'
    path.write_text(
        '"AppState"\n{\n'
        f'\t"appid"\t"{appid}"\n'
        f'\t"name"\t"{name}"\n'
        f'\t"installdir"\t"{installdir}"\n'
        '}\n'
    )
    (steamapps / 'common' / installdir).mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Redirect Path.home() to a temp dir and clear Steam-related env vars."""
    monkeypatch.setenv('HOME', str(tmp_path))
    for var in ('JANUS_STEAM_ROOT', 'STEAM_COMPAT_CLIENT_INSTALL_PATH'):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


# ---------------------------------------------------------------------------
# Phase 1: Steam root discovery
# ---------------------------------------------------------------------------

def test_no_steam_install_raises(fake_home):
    with pytest.raises(FileNotFoundError):
        steam.find_steam_root()
    assert steam.find_all_steam_roots() == []


def test_native_install_discovered(fake_home):
    root = make_steam_root(fake_home / '.local' / 'share' / 'Steam')
    assert steam.find_steam_root() == root
    assert root in steam.find_all_steam_roots()


def test_flatpak_install_discovered(fake_home):
    root = make_steam_root(
        fake_home / '.var' / 'app' / 'com.valvesoftware.Steam' / 'data' / 'Steam'
    )
    assert steam.find_steam_root() == root


def test_snap_install_discovered(fake_home):
    root = make_steam_root(
        fake_home / 'snap' / 'steam' / 'common' / '.local' / 'share' / 'Steam'
    )
    assert steam.find_steam_root() == root


def test_env_override_takes_precedence(fake_home, monkeypatch):
    # Native install present...
    make_steam_root(fake_home / '.local' / 'share' / 'Steam')
    # ...but override points at a custom path.
    custom = make_steam_root(fake_home / 'custom_steam')
    monkeypatch.setenv('JANUS_STEAM_ROOT', str(custom))
    assert steam.find_steam_root() == custom


def test_steam_compat_env_used_when_janus_unset(fake_home, monkeypatch):
    custom = make_steam_root(fake_home / 'compat_steam')
    monkeypatch.setenv('STEAM_COMPAT_CLIENT_INSTALL_PATH', str(custom))
    assert steam.find_steam_root() == custom


def test_janus_env_outranks_compat_env(fake_home, monkeypatch):
    janus_root = make_steam_root(fake_home / 'janus_steam')
    compat_root = make_steam_root(fake_home / 'compat_steam')
    monkeypatch.setenv('JANUS_STEAM_ROOT', str(janus_root))
    monkeypatch.setenv('STEAM_COMPAT_CLIENT_INSTALL_PATH', str(compat_root))
    assert steam.find_steam_root() == janus_root


def test_janus_env_invalid_raises(fake_home, monkeypatch):
    """Typo'd JANUS_STEAM_ROOT must fail loudly, not fall through silently."""
    make_steam_root(fake_home / '.local' / 'share' / 'Steam')
    monkeypatch.setenv('JANUS_STEAM_ROOT', str(fake_home / 'does_not_exist'))
    with pytest.raises(FileNotFoundError, match='JANUS_STEAM_ROOT'):
        steam.find_steam_root()


def test_compat_env_invalid_falls_through(fake_home, monkeypatch):
    """Steam-set STEAM_COMPAT_CLIENT_INSTALL_PATH is lenient — ignored if invalid."""
    real = make_steam_root(fake_home / '.local' / 'share' / 'Steam')
    monkeypatch.setenv('STEAM_COMPAT_CLIENT_INSTALL_PATH', str(fake_home / 'stale'))
    assert steam.find_steam_root() == real


def test_invalid_candidate_rejected(fake_home):
    # Dir exists but has no steamapps subdir — must be rejected.
    (fake_home / '.local' / 'share' / 'Steam').mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        steam.find_steam_root()


def test_multiple_installs_all_returned(fake_home):
    native = make_steam_root(fake_home / '.local' / 'share' / 'Steam')
    flatpak = make_steam_root(
        fake_home / '.var' / 'app' / 'com.valvesoftware.Steam' / 'data' / 'Steam'
    )
    roots = steam.find_all_steam_roots()
    assert native in roots
    assert flatpak in roots


def test_symlinked_candidates_deduped(fake_home):
    real = make_steam_root(fake_home / 'real_steam')
    link = fake_home / '.local' / 'share' / 'Steam'
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(real)
    roots = steam.find_all_steam_roots()
    # Even though the candidate list contains both, the resolved real path
    # should appear once.
    assert roots.count(real) == 1


# ---------------------------------------------------------------------------
# Phase 2: SteamGame multi-root library discovery
# ---------------------------------------------------------------------------

def test_game_in_native_install(fake_home):
    root = make_steam_root(fake_home / '.local' / 'share' / 'Steam')
    make_app_manifest(root / 'steamapps', 550, 'Left 4 Dead 2', 'Left 4 Dead 2')
    game = steam.SteamGame(550)
    assert game.steam_root == root
    assert game.library_path == root / 'steamapps'
    assert game.name == 'Left 4 Dead 2'


def test_game_in_flatpak_when_native_present_but_empty(fake_home):
    """User has native Steam installed (no games) and Flatpak Steam (with game)."""
    native = make_steam_root(fake_home / '.local' / 'share' / 'Steam')
    flatpak = make_steam_root(
        fake_home / '.var' / 'app' / 'com.valvesoftware.Steam' / 'data' / 'Steam'
    )
    make_app_manifest(flatpak / 'steamapps', 550, 'Left 4 Dead 2', 'L4D2')
    game = steam.SteamGame(550)
    assert game.steam_root == flatpak
    assert game.library_path == flatpak / 'steamapps'
    assert native != flatpak


def test_game_in_extra_library_folder(fake_home):
    """Manifest lives on a secondary library folder referenced by libraryfolders.vdf."""
    root = fake_home / '.local' / 'share' / 'Steam'
    (root / 'steamapps').mkdir(parents=True)
    extra = fake_home / 'extra_drive' / 'SteamLibrary'
    (extra / 'steamapps').mkdir(parents=True)
    (root / 'steamapps' / 'libraryfolders.vdf').write_text(
        '"libraryfolders"\n{\n'
        '\t"0"\n\t{\n\t\t"path"\t"' + str(root) + '"\n\t}\n'
        '\t"1"\n\t{\n\t\t"path"\t"' + str(extra) + '"\n\t}\n'
        '}\n'
    )
    make_app_manifest(extra / 'steamapps', 550, 'L4D2', 'L4D2')
    game = steam.SteamGame(550)
    assert game.steam_root == root  # root that owned the libraryfolders.vdf
    assert game.library_path == extra / 'steamapps'


def test_game_not_found_raises(fake_home):
    make_steam_root(fake_home / '.local' / 'share' / 'Steam')
    game = steam.SteamGame(99999)
    with pytest.raises(FileNotFoundError):
        _ = game.library_path


def test_explicit_steam_root_overrides_search(fake_home):
    # Game lives in Flatpak; explicit root points at native (no game).
    native = make_steam_root(fake_home / '.local' / 'share' / 'Steam')
    flatpak = make_steam_root(
        fake_home / '.var' / 'app' / 'com.valvesoftware.Steam' / 'data' / 'Steam'
    )
    make_app_manifest(flatpak / 'steamapps', 550, 'L4D2', 'L4D2')
    game = steam.SteamGame(550, steam_root=native)
    with pytest.raises(FileNotFoundError):
        _ = game.library_path
    assert game.steam_root == native


# ---------------------------------------------------------------------------
# get_installed_games() multi-root aggregation
# ---------------------------------------------------------------------------

def test_installed_games_spans_all_roots(fake_home):
    native = make_steam_root(fake_home / '.local' / 'share' / 'Steam')
    flatpak = make_steam_root(
        fake_home / '.var' / 'app' / 'com.valvesoftware.Steam' / 'data' / 'Steam'
    )
    make_app_manifest(native / 'steamapps', 220, 'Half-Life 2', 'Half-Life 2')
    make_app_manifest(flatpak / 'steamapps', 550, 'L4D2', 'L4D2')
    games = steam.get_installed_games()
    appids = {g['appid'] for g in games}
    assert 220 in appids
    assert 550 in appids


def test_installed_games_dedupes_by_appid(fake_home):
    """Same appid installed in two roots: only one entry returned."""
    native = make_steam_root(fake_home / '.local' / 'share' / 'Steam')
    flatpak = make_steam_root(
        fake_home / '.var' / 'app' / 'com.valvesoftware.Steam' / 'data' / 'Steam'
    )
    make_app_manifest(native / 'steamapps', 550, 'L4D2-native', 'L4D2')
    make_app_manifest(flatpak / 'steamapps', 550, 'L4D2-flatpak', 'L4D2')
    games = [g for g in steam.get_installed_games() if g['appid'] == 550]
    assert len(games) == 1

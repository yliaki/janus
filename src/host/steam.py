"""Steam game launcher with runtime support."""

import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional, List, Dict, Tuple

import bepinex
import steamstub
import unreal
import pad_matcher

try:
    import profiles
except ImportError:            # imported as src.host.steam (tests)
    from . import profiles


class SteamGame:
    """Represents a Steam game installation."""

    def __init__(self, appid: int, steam_root: Path = None,
                 launch_command: Optional[str] = None):
        self.appid = appid
        self._explicit_root = steam_root
        self._manifest = None
        self._library_path = None
        self._steam_root = None
        # Explicit "how to start this game", relative to install_dir. Overrides
        # all executable auto-detection; see resolve_launch_command().
        self.launch_command = launch_command

    @property
    def steam_root(self) -> Path:
        """Steam root that owns this game's manifest.

        Resolved lazily by locating the manifest across all known Steam roots
        (handles users with both native and Flatpak Steam, multi-install setups).
        Falls back to the first discovered root if the manifest is missing.
        """
        if self._steam_root is None:
            # Caller-provided root wins.
            if self._explicit_root is not None:
                self._steam_root = self._explicit_root
            else:
                # Trigger library lookup; it sets _steam_root as a side effect.
                self._find_library_path()
                if self._steam_root is None:
                    # Manifest wasn't found anywhere — fall back to first root
                    # so callers reading `steam_root` don't crash.
                    self._steam_root = find_steam_root()
        return self._steam_root

    @steam_root.setter
    def steam_root(self, value: Path) -> None:
        self._explicit_root = value
        self._steam_root = value

    def _find_library_path(self) -> Path:
        """Find the library folder containing this game.

        Searches every discovered Steam root (native, Flatpak, Snap, custom).
        Sets ``self._steam_root`` to the root that owns the manifest.
        """
        if self._explicit_root is not None:
            roots = [self._explicit_root]
        else:
            roots = list(find_all_steam_roots())
            if not roots:
                raise FileNotFoundError("Steam installation not found")

        for root in roots:
            # Check main steamapps folder
            main_steamapps = root / 'steamapps'
            manifest = main_steamapps / f'appmanifest_{self.appid}.acf'
            if manifest.exists():
                self._steam_root = root
                return main_steamapps

            # Check library folders from libraryfolders.vdf
            library_folders = parse_vdf(main_steamapps / 'libraryfolders.vdf')
            if not library_folders:
                continue
            for key, folder in library_folders.get('libraryfolders', {}).items():
                if not key.isdigit():
                    continue
                folder_path = folder.get('path') if isinstance(folder, dict) else folder
                if not folder_path:
                    continue
                steamapps = Path(folder_path) / 'steamapps'
                manifest = steamapps / f'appmanifest_{self.appid}.acf'
                if manifest.exists():
                    self._steam_root = root
                    return steamapps

        raise FileNotFoundError(f"Could not find library containing app {self.appid}")

    @property
    def library_path(self) -> Path:
        """Get the steamapps folder containing this game."""
        if self._library_path is None:
            self._library_path = self._find_library_path()
        return self._library_path

    @property
    def manifest(self) -> dict:
        """Parse appmanifest_<appid>.acf"""
        if self._manifest is None:
            manifest_path = self.library_path / f'appmanifest_{self.appid}.acf'
            self._manifest = parse_vdf(manifest_path)
        return self._manifest

    @property
    def name(self) -> str:
        """Game name from manifest."""
        app_state = self.manifest.get('AppState', {})
        return app_state.get('name', f'App {self.appid}')

    @property
    def install_dir(self) -> Path:
        """Game installation directory."""
        app_state = self.manifest.get('AppState', {})
        installdir = app_state.get('installdir', '')
        return self.library_path / 'common' / installdir

    @property
    def is_native(self) -> bool:
        """True if game has native Linux executables (not Proton)."""
        # An explicit launch command settles it without any probing: a .exe entry
        # point means Proton, anything else is a native binary/script/interpreter.
        # (Read from the raw string — this property is hot and shouldn't touch
        # the filesystem to answer a question the user already answered.)
        if self.launch_command:
            try:
                first = _launch_command_parts(self.launch_command)[0]
            except ValueError:
                first = ''
            if first:
                return not first.lower().endswith('.exe')

        return has_native_build(self.install_dir)

    @property
    def is_proton(self) -> bool:
        """True if game uses Proton (has compatdata and no native executables)."""
        if self.is_native:
            return False
        config_info = self.library_path / 'compatdata' / str(self.appid) / 'config_info'
        return config_info.exists()

    @property
    def proton_path(self) -> Optional[Path]:
        """Get Proton installation path from compatdata config_info."""
        config_info = self.library_path / 'compatdata' / str(self.appid) / 'config_info'
        if not config_info.exists():
            return None
        try:
            lines = config_info.read_text().strip().split('\n')
            if len(lines) >= 2:
                # Line 2 contains fonts path like .../proton_xxx/files/share/fonts/
                fonts_path = lines[1]
                proton_path = Path(fonts_path.split('/files/share/fonts')[0])
                if proton_path.exists():
                    return proton_path
        except (IOError, IndexError):
            pass
        return None

    @property
    def compat_data_path(self) -> Path:
        """Get compatdata path for this game."""
        return self.library_path / 'compatdata' / str(self.appid)

    @property
    def is_32bit(self) -> bool:
        """True if game is 32-bit (needs scout runtime)."""
        if not self.install_dir.exists():
            return False

        # Look for 32-bit indicators by filename
        for pattern in ['*.x86', '*_32', '*32.so']:
            if list(self.install_dir.glob(pattern)):
                return True

        # Check lib32 directory
        if (self.install_dir / 'lib32').exists():
            return True

        # Check ELF header of known executables
        for exe_name in ['hl2_linux', 'hl2.sh']:  # Source engine
            exe = self.install_dir / exe_name
            if exe.exists():
                if exe_name.endswith('.sh'):
                    # Check the actual binary for shell wrappers
                    actual_exe = self.install_dir / 'hl2_linux'
                    if actual_exe.exists() and _is_elf_32bit(actual_exe):
                        return True
                elif _is_elf_32bit(exe):
                    return True

        # Generic fallback: read the ELF class of whatever we'd actually launch.
        # Covers nested layouts and games that don't follow any naming pattern.
        # Proton games have no ELF entry point — skip the (recursive) exe search.
        if self.is_proton:
            return False
        try:
            main_exe = find_game_executable(self)
        except (FileNotFoundError, OSError):
            return False
        if _is_elf(main_exe):
            return _is_elf_32bit(main_exe)

        # Shell wrapper (svencoop.sh, hl2.sh, run.sh): find the binary it execs.
        real_bin = _resolve_shell_wrapper_binary(main_exe)
        if real_bin is not None:
            return _is_elf_32bit(real_bin)

        return False


def _is_elf(path: Path) -> bool:
    """Check if a file is an ELF binary."""
    try:
        with open(path, 'rb') as f:
            return f.read(4) == b'\x7fELF'
    except (IOError, OSError):
        return False


def _is_elf_32bit(path: Path) -> bool:
    """Check if an ELF binary is 32-bit by reading the header."""
    try:
        with open(path, 'rb') as f:
            magic = f.read(5)
            # ELF magic: 0x7f 'E' 'L' 'F' class
            # class: 1 = 32-bit, 2 = 64-bit
            if magic[:4] == b'\x7fELF':
                return magic[4] == 1
    except (IOError, OSError):
        pass
    return False


def parse_vdf(path: Path) -> dict:
    """
    Parse a Valve Data Format (VDF) file.
    Simple recursive parser for Steam's config format.
    """
    if not path.exists():
        return {}

    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
    except IOError:
        return {}

    return _parse_vdf_content(content)


def _parse_vdf_content(content: str) -> dict:
    """Parse VDF content string into a dict."""
    result = {}
    stack = [result]

    # Regex for quoted strings and braces
    token_re = re.compile(r'"([^"]*)"|\{|\}')

    tokens = token_re.findall(content)
    # findall with groups returns tuples, flatten
    tokens = []
    for match in token_re.finditer(content):
        if match.group(1) is not None:
            tokens.append(match.group(1))
        else:
            tokens.append(match.group(0))

    i = 0
    while i < len(tokens):
        token = tokens[i]

        if token == '{':
            i += 1
            continue
        elif token == '}':
            if len(stack) > 1:
                stack.pop()
            i += 1
            continue
        else:
            # This is a key
            key = token
            i += 1
            if i >= len(tokens):
                break

            next_token = tokens[i]
            if next_token == '{':
                # Value is a nested dict
                new_dict = {}
                stack[-1][key] = new_dict
                stack.append(new_dict)
                i += 1
            else:
                # Value is a string
                stack[-1][key] = next_token
                i += 1

    return result


def _is_valid_steam_root(path: Path) -> bool:
    """A path is a valid Steam root if it contains steamapps/libraryfolders.vdf
    (or at least a steamapps dir — fresh installs have it before any library
    is registered).
    """
    if not path.is_dir():
        return False
    steamapps = path / 'steamapps'
    if not steamapps.is_dir():
        return False
    # libraryfolders.vdf isn't strictly required (a fresh install lacks it
    # until the first game is installed) — but if it's there, treat the
    # root as canonical. Bare steamapps/ is still acceptable.
    return True


def _steam_root_candidates() -> List[Path]:
    """Ordered list of candidate Steam root paths, highest precedence first.

    Explicit env overrides come first, then well-known package layouts,
    then legacy fallbacks. Duplicate / symlinked entries are collapsed.
    """
    home = Path.home()
    raw: List[Path] = []

    # 1. Explicit overrides
    for env_var in ('JANUS_STEAM_ROOT', 'STEAM_COMPAT_CLIENT_INSTALL_PATH'):
        val = os.environ.get(env_var)
        if val:
            raw.append(Path(val))

    # 2. Native packages (Debian/Arch/Fedora/etc.)
    raw.extend([
        home / '.steam' / 'steam',
        home / '.steam' / 'debian-installation',
        home / '.local' / 'share' / 'Steam',
    ])

    # 3. Flatpak (com.valvesoftware.Steam)
    flatpak_base = home / '.var' / 'app' / 'com.valvesoftware.Steam'
    raw.extend([
        flatpak_base / 'data' / 'Steam',
        flatpak_base / '.local' / 'share' / 'Steam',
        flatpak_base / '.steam' / 'steam',
    ])

    # 4. Snap
    snap_base = home / 'snap' / 'steam' / 'common'
    raw.extend([
        snap_base / '.local' / 'share' / 'Steam',
        snap_base / '.steam' / 'steam',
    ])

    # 5. System-wide install
    raw.append(Path('/usr/share/steam'))

    # Resolve symlinks and dedupe while preserving order.
    seen: set = set()
    resolved: List[Path] = []
    for p in raw:
        try:
            if p.is_symlink():
                target = p.resolve()
            else:
                target = p
        except (OSError, RuntimeError):
            continue
        key = str(target)
        if key in seen:
            continue
        seen.add(key)
        resolved.append(target)
    return resolved


def find_all_steam_roots() -> List[Path]:
    """Return every valid Steam root on this machine, in precedence order.

    Useful for users with multiple Steam installs (e.g., native + Flatpak).

    If ``JANUS_STEAM_ROOT`` is set, it is treated as an authoritative override:
    invalid values raise instead of falling through to discovery. This catches
    typo'd configuration. ``STEAM_COMPAT_CLIENT_INSTALL_PATH`` is treated as
    a hint (Steam itself sets it) and silently ignored if invalid.
    """
    override = os.environ.get('JANUS_STEAM_ROOT')
    if override:
        path = Path(override)
        if path.is_symlink():
            try:
                path = path.resolve()
            except (OSError, RuntimeError):
                pass
        if not _is_valid_steam_root(path):
            raise FileNotFoundError(
                f"JANUS_STEAM_ROOT={override!r} is not a valid Steam root "
                "(must contain a steamapps/ subdirectory)."
            )
        return [path]
    return [p for p in _steam_root_candidates() if _is_valid_steam_root(p)]


def find_steam_root() -> Path:
    """Find Steam installation directory (highest-precedence valid root)."""
    roots = find_all_steam_roots()
    if not roots:
        raise FileNotFoundError(
            "Steam installation not found. Searched native, Flatpak, and Snap "
            "layouts. Set JANUS_STEAM_ROOT to override."
        )
    return roots[0]


def get_hook_paths() -> List[Path]:
    """Get paths to hook libraries (64-bit and 32-bit)."""
    here = Path(__file__).parent
    build_dir = here.parent.parent / 'build'

    paths = []
    for name in ['libjanus64.so', 'libjanus32.so']:
        path = build_dir / name
        if path.exists():
            paths.append(path.resolve())

    # Fallback to single library
    if not paths:
        single = build_dir / 'libjanus.so'
        if single.exists():
            paths.append(single.resolve())

    return paths


def _launch_command_parts(launch_command: str) -> List[str]:
    """Split a user-supplied launch command into ``[exe, *args]``.

    Shell-style quoting, so paths with spaces work: ``"bin/My Game" --fullscreen``.
    """
    parts = shlex.split(launch_command)
    if not parts:
        raise ValueError("Launch command is empty")
    return parts


def resolve_launch_command(install_dir: Path,
                           launch_command: str) -> Tuple[Path, List[str]]:
    """Resolve an explicit launch command against a game's install directory.

    The first token is the executable, taken **relative to install_dir** unless
    it's an absolute path — ``./jre/bin/java``, ``bin/linux64/game``, and
    ``/usr/bin/java`` are all valid. Remaining tokens are passed verbatim, ahead
    of any game args.

    Argument tokens are deliberately NOT rewritten into absolute paths: the game
    runs with its install dir (or, under Goldberg, the mirror) as cwd, so a
    relative ``-jar Necesse.jar`` resolves correctly in both.

    Needed for games whose real entry point isn't a discoverable binary — e.g.
    Necesse (appid 1169040), which Steam launches as
    ``./jre/bin/java ... -jar Necesse.jar``; the install dir's only scripts are
    the dedicated-server ones.
    """
    parts = _launch_command_parts(launch_command)
    exe_token, args = parts[0], parts[1:]

    exe = Path(exe_token)
    if not exe.is_absolute():
        exe = install_dir / exe_token
    # Normalize away "./" and "../" textually. Deliberately not resolve(): a
    # Goldberg mirror is a symlink farm, and resolving would rewrite the path
    # back into the real install dir, defeating the mirror rewrite in
    # launch_steam_game().
    exe = Path(os.path.normpath(str(exe)))

    if not exe.exists():
        raise FileNotFoundError(
            f"Launch command executable not found: {exe}\n"
            f"  (from launch command: {launch_command!r})\n"
            f"  Paths are relative to the game's install dir: {install_dir}"
        )
    if exe.is_dir():
        raise FileNotFoundError(
            f"Launch command executable is a directory: {exe}\n"
            f"  (from launch command: {launch_command!r})"
        )
    # Common mistake: naming the payload instead of the interpreter that runs it.
    # A .jar is never directly execve-able, and Steam sets the exec bit on every
    # shipped file, so the os.access() check below can't catch it — name it here.
    if exe.suffix.lower() == '.jar':
        raise ValueError(
            f"Launch command starts with a .jar, which can't be executed directly: {exe.name}\n"
            f"  (from launch command: {launch_command!r})\n"
            f"  Name the JVM instead, e.g. './jre/bin/java -jar {exe.name}'"
        )
    if exe.suffix.lower() != '.exe' and not os.access(exe, os.X_OK):
        raise PermissionError(
            f"Launch command executable is not executable: {exe}\n"
            f"  (from launch command: {launch_command!r})\n"
            f"  If this is a data file, name the interpreter instead, e.g. "
            f"'./jre/bin/java -jar {exe.name}'"
        )
    return exe, args


def find_game_executable(game: SteamGame, exe_name: Optional[str] = None,
                         launch_command: Optional[str] = None) -> Path:
    """Find the main executable for a game.

    An explicit launch_command (argument, or the one carried on the game) wins
    over everything. Otherwise, if exe_name is set, search only for that
    filename (recursively if needed).
    """
    install_dir = game.install_dir

    if not install_dir.exists():
        raise FileNotFoundError(f"Game directory not found: {install_dir}")

    launch_command = launch_command or getattr(game, 'launch_command', None)
    if launch_command:
        return resolve_launch_command(install_dir, launch_command)[0]

    # Explicit exe name override: search for exactly that file
    if exe_name:
        # Try direct path first (top-level)
        direct = install_dir / exe_name
        if direct.exists():
            return direct
        # Recursive search
        matches = list(install_dir.rglob(exe_name))
        if matches:
            return matches[0]
        raise FileNotFoundError(
            f"Executable '{exe_name}' not found in {install_dir}"
        )

    # Proton games: find .exe
    if game.is_proton:
        return _find_proton_executable(install_dir)

    # Native Linux: common executable patterns (in order of preference)
    patterns = [
        '*.x86_64',      # 64-bit Linux
        '*.x86',         # 32-bit Linux
        'start.sh',
        'launch.sh',
        'run.sh',
        '*.sh',
    ]

    for pattern in patterns:
        matches = list(install_dir.glob(pattern))
        if matches:
            # Filter out obvious non-game scripts
            for match in matches:
                name = match.name.lower()
                if 'uninstall' in name or 'setup' in name or 'config' in name:
                    continue
                return match

    # Nothing at the top level: scan the tree and rank candidates. Handles games
    # that nest their whole build in a subdirectory (DDNet: <install>/ddnet/DDNet)
    # as well as top-level binaries with no telling extension.
    candidates = _find_native_executables(install_dir)
    if candidates:
        names = _game_name_aliases(game)
        ranked = sorted(
            ((_score_native_executable(p, install_dir, names), str(p), p) for p in candidates),
            key=lambda t: (-t[0], t[1]),
        )
        usable = [p for score, _, p in ranked if score > _EXE_EXCLUDE_PENALTY]
        if not usable:
            raise FileNotFoundError(
                f"No suitable executable in {install_dir}. Available: "
                f"{', '.join(str(p.relative_to(install_dir)) for _, _, p in ranked)}"
            )
        best = usable[0]
        # Only warn when the pick wasn't decided by a name match — otherwise every
        # game shipping helper tools next to its binary would print a false alarm.
        # Once per install dir: this runs from is_32bit/build_command/... repeatedly.
        if (not _exe_name_matches(best, names) and len(usable) > 1
                and str(install_dir) not in _WARNED_EXE_DIRS):
            _WARNED_EXE_DIRS.add(str(install_dir))
            print(f"[janus] Multiple executables found, using: {best.relative_to(install_dir)}")
            print(f"[janus] Available: {', '.join(str(p.relative_to(install_dir)) for p in usable)}")
        return best

    raise FileNotFoundError(f"No executable found in {install_dir}")


_EXE_EXCLUDE = re.compile(
    r'_dev|_debug|server|dedicated|tool|unins|setup|crash|config|editor|sdk|redist|launcher',
    re.IGNORECASE
)

# Subdirectories to skip when searching recursively for executables
_DIR_EXCLUDE = re.compile(
    r'redist|__installer|directx|vcredist|dotnet|support|tools|sdk|editor',
    re.IGNORECASE
)

# Suffixes a native Linux entry point can carry. Anything else (.txt, .so, .dat)
# is never the thing we launch, even when the file has the exec bit set.
_NATIVE_EXE_SUFFIXES = {'', '.x86_64', '.x86', '.x64', '.i386', '.amd64', '.bin', '.sh', '.run'}

# How deep to walk looking for a native entry point. DDNet needs 1; a couple of
# levels of headroom covers layouts like <install>/bin/linux64/<game>.
_MAX_EXE_SEARCH_DEPTH = 3

_EXE_EXCLUDE_PENALTY = -1000
_EXE_NAME_MATCH_BONUS = 100

# Install dirs we've already printed an ambiguity warning for (detection runs
# several times per launch: is_32bit, build_command, launch_steam_game).
_WARNED_EXE_DIRS = set()

# Suffix preference among otherwise equal candidates.
_NATIVE_SUFFIX_BONUS = {
    '.x86_64': 12,
    '.sh': 10,
    '.x64': 10,
    '.amd64': 10,
    '.x86': 8,
    '': 6,
    '.i386': 6,
    '.bin': 4,
    '.run': 2,
}


def _normalize_name(name: str) -> str:
    """Collapse spacing/punctuation for fuzzy name matching ("DDNet " -> "ddnet")."""
    return re.sub(r'[\s\-_.]+', '', name).lower()


def _game_name_aliases(game: 'SteamGame') -> set:
    """Normalized names the main binary is likely to be called.

    Steam's manifest name and the install directory often differ (DDNet installs
    to "DDraceNetwork" but the binary is "DDNet"), so both are candidates.
    """
    aliases = set()
    for raw in (game.name, game.install_dir.name):
        if raw:
            aliases.add(_normalize_name(raw))
    aliases.discard('')
    return aliases


def _looks_like_native_exe(path: Path) -> bool:
    """True if path could be a native Linux entry point (ELF binary or shell script)."""
    if path.suffix.lower() not in _NATIVE_EXE_SUFFIXES:
        return False
    try:
        if not path.is_file() or not os.access(path, os.X_OK):
            return False
    except OSError:
        return False
    if _is_elf(path):
        return True
    # Shell wrappers (start.sh and friends) aren't ELF — accept them by shebang.
    try:
        with open(path, 'rb') as f:
            return f.read(2) == b'#!'
    except (IOError, OSError):
        return False


def has_native_build(install_dir: Path) -> bool:
    """True if *install_dir* holds a native Linux build (whatever a handler's
    launch command says)."""
    if not install_dir.exists():
        return False

    # Look for common native game patterns
    for pattern in ['*.x86_64', '*.x86', '*.sh', 'start.sh', 'launch.sh']:
        if list(install_dir.glob(pattern)):
            return True

    # Check for .so files (native libraries)
    if list(install_dir.glob('*.so')) or list(install_dir.glob('lib*/*.so*')):
        return True

    # Nested layouts (e.g. DDNet installs to <install>/ddnet/DDNet) keep the
    # whole native build one or more levels down, so nothing above matches.
    # An executable ELF binary anywhere in the tree is a strong native signal
    # (Windows-only games ship .exe/.dll, never ELF).
    return bool(_find_native_executables(install_dir))


def _find_native_executables(install_dir: Path,
                             max_depth: int = _MAX_EXE_SEARCH_DEPTH) -> List[Path]:
    """Collect plausible native entry points under install_dir (bounded depth)."""
    if not install_dir.is_dir():
        return []

    results: List[Path] = []
    base_depth = len(install_dir.parts)
    for dirpath, dirnames, filenames in os.walk(install_dir):
        current = Path(dirpath)
        depth = len(current.parts) - base_depth
        if depth >= max_depth:
            dirnames[:] = []
        else:
            dirnames[:] = [d for d in dirnames
                           if not d.startswith('.') and not _DIR_EXCLUDE.search(d)]
        for fname in filenames:
            candidate = current / fname
            if _looks_like_native_exe(candidate):
                results.append(candidate)
    return results


def _resolve_shell_wrapper_binary(script: Path) -> Optional[Path]:
    """Find the ELF binary a launcher script actually execs.

    Scans the script for tokens naming a sibling ELF file (svencoop.sh ->
    svencoop_linux, run.sh -> nuclearthrone). Used for bitness detection: the
    wrapper itself says nothing about the game's architecture.
    """
    try:
        text = script.read_text(encoding='utf-8', errors='replace')[:65536]
    except (IOError, OSError):
        return None

    stem = script.stem.lower()
    best = None
    for token in set(re.findall(r'[\w./\-]+', text)):
        name = token.rsplit('/', 1)[-1]
        if not name or name == script.name:
            continue
        sibling = script.parent / name
        # Suffix filter keeps the game's own .so libraries out of the running.
        if sibling.suffix.lower() not in _NATIVE_EXE_SUFFIXES or not _is_elf(sibling):
            continue
        if stem in name.lower():
            return sibling        # svencoop.sh -> svencoop_linux: unambiguous
        if best is None:
            best = sibling
    return best


def _exe_name_matches(path: Path, names: set) -> bool:
    """True if the binary is named after the game (exact, normalized)."""
    return _normalize_name(path.stem) in names


def _score_native_executable(path: Path, install_dir: Path, names: set) -> int:
    """Rank a native executable candidate — higher is more likely the game."""
    depth_penalty = (len(path.parts) - len(install_dir.parts) - 1) * 8

    # Excluded names (servers, editors, crash handlers, config helpers) can never
    # outrank a real candidate, no matter how well they match the game's name —
    # "DDNet-Server" would otherwise beat every other binary in the tree.
    if _EXE_EXCLUDE.search(path.stem):
        return _EXE_EXCLUDE_PENALTY - depth_penalty

    score = _NATIVE_SUFFIX_BONUS.get(path.suffix.lower(), 0)

    stem = _normalize_name(path.stem)
    if _exe_name_matches(path, names):
        score += _EXE_NAME_MATCH_BONUS
    elif any(stem.startswith(n) or n.startswith(stem) for n in names if len(n) >= 4):
        score += 50
    elif _normalize_name(path.parent.name) in names:
        # Binary sitting in a directory named after the game (<install>/ddnet/...)
        score += 20

    # Prefer shallower matches when everything else ties.
    return score - depth_penalty


def _find_proton_executable(install_dir: Path) -> Path:
    """Find the main .exe for a Proton game."""
    game_name = install_dir.name
    # Normalize: "Borderlands 2" -> "borderlands2" for fuzzy matching
    game_name_compact = re.sub(r'[\s\-_]+', '', game_name).lower()

    # Priority 1: Exact match with game folder name (top-level)
    for exe in install_dir.glob('*.exe'):
        if exe.stem.lower() == game_name.lower():
            return exe

    # Priority 2: Any top-level exe, excluding dev/debug/server/etc
    candidates = []
    for exe in install_dir.glob('*.exe'):
        if not _EXE_EXCLUDE.search(exe.stem):
            candidates.append(exe)

    if candidates:
        if len(candidates) > 1:
            print(f"[janus] Warning: Multiple executables found, using: {candidates[0].name}")
            print(f"[janus] Available: {', '.join(e.name for e in candidates)}")
        return candidates[0]

    # Priority 3: Recursive search in subdirectories (e.g. Binaries/Win32/)
    all_exes = []
    for exe in install_dir.rglob('*.exe'):
        # Skip excluded directories
        if any(_DIR_EXCLUDE.search(p) for p in exe.relative_to(install_dir).parts[:-1]):
            continue
        all_exes.append(exe)

    if all_exes:
        # Prefer exe whose name matches the game name
        for exe in all_exes:
            stem_compact = re.sub(r'[\s\-_]+', '', exe.stem).lower()
            if stem_compact == game_name_compact:
                return exe

        # Fall back to non-excluded candidates
        sub_candidates = [e for e in all_exes if not _EXE_EXCLUDE.search(e.stem)]
        if sub_candidates:
            if len(sub_candidates) > 1:
                print(f"[janus] Warning: Multiple executables found, using: {sub_candidates[0].relative_to(install_dir)}")
                print(f"[janus] Available: {', '.join(str(e.relative_to(install_dir)) for e in sub_candidates)}")
            return sub_candidates[0]

        raise FileNotFoundError(
            f"No suitable executable in {install_dir}. "
            f"Available: {', '.join(str(e.relative_to(install_dir)) for e in all_exes)}"
        )

    raise FileNotFoundError(f"No executable found in {install_dir}")


def detect_game_args(game: SteamGame) -> List[str]:
    """Detect default arguments for a game."""
    args = []

    # Source engine games need -game argument
    if (game.install_dir / 'hl2.sh').exists():
        # The game directory has a gameinfo.txt. So do DLC and update dirs
        # (Left 4 Dead 2: left4dead2, left4dead2_dlc1..3, update), and the
        # directory listing order is arbitrary: it picked left4dead2_dlc3.
        # Skip those and take the shortest name (the base game).
        candidates = [
            d.name for d in game.install_dir.iterdir()
            if d.is_dir() and (d / 'gameinfo.txt').exists()
            and d.name not in ('hl2', 'platform', 'bin', 'update')
            and '_dlc' not in d.name.lower()]
        if candidates:
            args.extend(['-game', min(candidates, key=lambda n: (len(n), n))])

    return args


# Directories inside pfx/drive_c/ that contain writable user data.
# Everything else (windows/, Program Files/, vrclient/, etc.) gets symlinked.
_COPY_DIRS = {'ProgramData'}
_SKELETON_DIRS = {'users'}


def _copy_skeleton(source: Path, dest: Path):
    """Recreate a directory tree structure with symlinks but without regular files."""
    dest.mkdir(parents=True, exist_ok=True)
    for item in source.rglob('*'):
        rel = item.relative_to(source)
        dest_item = dest / rel
        if item.is_symlink():
            # Preserve internal symlinks (Wine needs these, e.g. My Documents -> ./Documents)
            dest_item.symlink_to(os.readlink(item))
        elif item.is_dir():
            dest_item.mkdir(parents=True, exist_ok=True)


def _isolate_proton_prefix(source: Path, dest: Path, copy_userdata: bool = False):
    """
    Create a smart isolated copy of a Proton Wine prefix.

    Symlinks heavy read-only directories (windows/, Program Files/, etc.),
    creates empty skeleton for user data directories (users/), and copies
    only small writable data (ProgramData/, registry files).

    If copy_userdata is True, users/ is fully copied instead of skeleton-only.
    """
    if dest.exists():
        return  # Already set up

    dest.mkdir(parents=True)

    # Copy small top-level files (config_info, version, tracked_files, etc.)
    for item in source.iterdir():
        if item.is_file():
            shutil.copy2(item, dest / item.name)

    # Process pfx/ directory
    source_pfx = source / 'pfx'
    if not source_pfx.exists():
        return

    dest_pfx = dest / 'pfx'
    dest_pfx.mkdir()

    # Copy pfx-level files (*.reg, *.reg.old, etc.)
    for item in source_pfx.iterdir():
        if item.is_file():
            shutil.copy2(item, dest_pfx / item.name)

    # Process drive_c/
    source_drive_c = source_pfx / 'drive_c'
    if source_drive_c.exists():
        dest_drive_c = dest_pfx / 'drive_c'
        dest_drive_c.mkdir()

        for item in source_drive_c.iterdir():
            dest_item = dest_drive_c / item.name

            if item.is_dir():
                if item.name in _SKELETON_DIRS and not copy_userdata:
                    # Recreate directory structure only (clean slate for saves/config)
                    _copy_skeleton(item, dest_item)
                elif item.name in (_SKELETON_DIRS | _COPY_DIRS):
                    # Copy small writable directories
                    shutil.copytree(item, dest_item, symlinks=True)
                else:
                    # Symlink everything else (windows/, Program Files/, etc.)
                    dest_item.symlink_to(item.resolve())
            elif item.is_file():
                shutil.copy2(item, dest_item)

    # Recreate dosdevices/ with updated symlinks
    source_dosdevices = source_pfx / 'dosdevices'
    if source_dosdevices.exists():
        dest_dosdevices = dest_pfx / 'dosdevices'
        dest_dosdevices.mkdir()

        for link in source_dosdevices.iterdir():
            if link.is_symlink():
                if link.name == 'c:':
                    # Point c: to our new drive_c, relative like Wine's own
                    # link so the prefix survives being moved
                    (dest_dosdevices / link.name).symlink_to('../drive_c')
                else:
                    # Preserve other drive mappings (z: -> /, etc.)
                    target = os.readlink(link)
                    (dest_dosdevices / link.name).symlink_to(target)

    # Copy any remaining pfx subdirectories not yet handled
    for item in source_pfx.iterdir():
        dest_item = dest_pfx / item.name
        if dest_item.exists() or item.is_file():
            continue  # Already handled
        if item.is_dir():
            if item.name in ('drive_c', 'dosdevices'):
                continue  # Already handled above
            # Other pfx subdirs: symlink by default
            dest_item.symlink_to(item.resolve())


_GOLDBERG_MIRRORS_DIR = Path.home() / '.local' / 'share' / 'janus' / 'goldberg_mirrors'


def goldberg_mirror_dir(appid: int, instance: int) -> Path:
    """Return the per-instance Goldberg mirror directory.

    Used by the GUI lobby watcher to locate gbe_fork's STEAM_LOG_*.log files
    (under <mirror>/_janus_gbe_logs/ when GseAppPath is set, or scattered
    inside the mirror otherwise).
    """
    return _GOLDBERG_MIRRORS_DIR / str(appid) / str(instance)
# Repo-vendored gbe_fork DLLs
_REPO_GOLDBERG_DIR = Path(__file__).resolve().parents[2] / 'vendor' / 'goldberg'

# steam_api filenames that Goldberg replaces
_STEAM_API_FILES = {'steam_api.dll', 'steam_api64.dll', 'libsteam_api.so'}


def goldberg_dir(debug: bool = False) -> Path:
    """Return the vendored gbe_fork build directory: <repo>/vendor/goldberg/
    {regular,debug}/. 'debug' is a non-release build that writes
    STEAM_LOG_*.log next to the steam_api library — used by the lobby-ID
    watcher in the GUI.
    """
    return _REPO_GOLDBERG_DIR / ('debug' if debug else 'regular')


def _pick_goldberg_so(original: Path, debug: bool = False) -> Optional[Path]:
    """Pick the correct Goldberg libsteam_api.so based on ELF class of the original."""
    base = goldberg_dir(debug)
    try:
        with open(original, 'rb') as f:
            magic = f.read(5)
        if len(magic) >= 5 and magic[:4] == b'\x7fELF':
            ei_class = magic[4]
            if ei_class == 1:  # ELFCLASS32
                return base / 'libsteam_api32.so'
            elif ei_class == 2:  # ELFCLASS64
                return base / 'libsteam_api.so'
    except OSError:
        pass
    return base / 'libsteam_api.so'


def create_goldberg_mirror(game: SteamGame, instance: int = 1,
                           debug: bool = False,
                           copy_instead: Optional[List[str]] = None,
                           exclude: Optional[List[str]] = None,
                           replace_files: Optional[Dict[str, Path]] = None,
                           goldberg: bool = True) -> Path:
    """
    Create a symlinked mirror of the game directory with Goldberg steam_api replacements.
    goldberg=False: a plain mirror (real steam_api kept), for handlers that
    only need a writable copy of the install (e.g. BepInEx, bepinex.py).

    The original game directory is never modified. Only directories containing
    steam_api files get real copies; everything else is symlinked.
    Each instance gets its own mirror to avoid race conditions.

    Returns the mirror root path.
    """
    mirror_root = _GOLDBERG_MIRRORS_DIR / str(game.appid) / str(instance)
    install_dir = game.install_dir

    # Find all directories containing steam_api files
    api_dirs = set()
    for dirpath, dirnames, filenames in os.walk(install_dir) if goldberg else ():
        for fname in filenames:
            if fname.lower() in _STEAM_API_FILES:
                api_dirs.add(Path(dirpath))

    if goldberg and not api_dirs:
        print(f"[janus] Warning: No steam_api files found in {install_dir}", flush=True)
        print("[janus] Goldberg mirror will be a plain symlink mirror", flush=True)

    # Collect all ancestor directories that need to be real dirs (not symlinks)
    # because they contain a descendant with steam_api files
    real_dirs = set()
    for api_dir in api_dirs:
        current = api_dir
        while current != install_dir:
            real_dirs.add(current)
            current = current.parent
        real_dirs.add(install_dir)

    # Handler-script mirror tweaks (Game.mirrorCopyInstead / mirrorExclude):
    # install-dir-relative paths to copy for real instead of symlinking (games
    # that write inside their own install dir), or to omit from the mirror
    # entirely. Their ancestors must be real dirs so the walk reaches them.
    def _sane_mirror_entries(entries, label):
        """Only clean relative sub-paths are usable: absolute paths would
        escape the install dir entirely (pathlib: base / '/x' == '/x'), and
        '.'/''/'..' entries would send the ancestor walk past install_dir
        into an endless Path('/').parent loop."""
        out = set()
        for p in entries or []:
            ps = str(p)
            parts = Path(ps).parts
            if (not ps or ps == '.' or Path(ps).is_absolute()
                    or '..' in parts):
                print(f"[janus] Warning: ignoring bad {label} entry {ps!r} "
                      f"(must be a relative path inside the install dir)",
                      flush=True)
                continue
            out.add(install_dir / ps)
        return out

    copy_paths = _sane_mirror_entries(copy_instead, 'mirrorCopyInstead')
    exclude_paths = _sane_mirror_entries(exclude, 'mirrorExclude')
    # replace_files: {install-relative path: source file} — the mirror gets a
    # real copy of *source* at that path (e.g. a Steamless-unpacked exe). Its
    # ancestors become real dirs, so the copy can never land inside a symlinked
    # directory and overwrite the real install.
    replace_map = {}
    for rel, src_file in (replace_files or {}).items():
        for target in _sane_mirror_entries([rel], 'replace_files'):
            replace_map[target] = Path(src_file)
    for target in (copy_paths | exclude_paths | set(replace_map)):
        current = target.parent
        while current != install_dir:
            if current == current.parent:   # reached '/': entry escaped root
                break
            real_dirs.add(current)
            current = current.parent
        real_dirs.add(install_dir)

    # Remove stale mirror and recreate
    if mirror_root.exists():
        shutil.rmtree(mirror_root)
    mirror_root.mkdir(parents=True)

    def _mirror_dir(src: Path, dst: Path):
        """Recursively mirror src into dst."""
        dst.mkdir(parents=True, exist_ok=True)
        for item in src.iterdir():
            dst_item = dst / item.name
            if item in exclude_paths:
                continue
            if item in replace_map:
                try:    # a hard link when on the same filesystem (big exes)
                    os.link(replace_map[item], dst_item)
                except OSError:
                    shutil.copy2(str(replace_map[item]), str(dst_item))
                print(f"[janus] Mirror: {item.relative_to(install_dir)} <- "
                      f"{replace_map[item].name}", flush=True)
                continue
            if item in copy_paths:
                # Real copy requested by the handler script. Note: steam_api
                # files inside a copied tree are NOT goldberg-replaced.
                if item.is_dir() and not item.is_symlink():
                    def _skip_excluded(dirpath, names):
                        base = Path(dirpath)
                        return [n for n in names
                                if (base / n) in exclude_paths]
                    shutil.copytree(item, dst_item, symlinks=True,
                                    ignore=_skip_excluded)
                else:
                    shutil.copy2(str(item), str(dst_item))
                continue
            if item.is_dir() and not item.is_symlink():
                if item in real_dirs:
                    # This dir (or a descendant) has steam_api files — recurse
                    _mirror_dir(item, dst_item)
                else:
                    # Safe to symlink entire directory
                    dst_item.symlink_to(item.resolve())
            elif item.is_file() or item.is_symlink():
                if item.name.lower() in _STEAM_API_FILES and src in api_dirs:
                    # Replace with Goldberg version (arch-aware for .so files)
                    goldberg_src = goldberg_dir(debug) / item.name
                    if item.name.lower() == 'libsteam_api.so':
                        # Detect ELF class of original to pick matching Goldberg lib
                        goldberg_src = _pick_goldberg_so(item, debug)
                    if goldberg_src and goldberg_src.exists():
                        shutil.copy2(str(goldberg_src), str(dst_item))
                        print(f"[janus] Replaced {item.relative_to(install_dir)} with Goldberg version", flush=True)
                    else:
                        print(f"[janus] Warning: Goldberg library not found for {item.name}", flush=True)
                        dst_item.symlink_to(item.resolve())
                else:
                    dst_item.symlink_to(item.resolve())

    _mirror_dir(install_dir, mirror_root)
    print(f"[janus] Created {'Goldberg ' if goldberg else ''}mirror at {mirror_root}",
          flush=True)
    return mirror_root


def ticket_proxy_dll() -> Path:
    """The ticket proxy DLL, committed prebuilt (rebuild: make -C src/ticketproxy)."""
    return (Path(__file__).parent.parent.parent / 'vendor' / 'ticketproxy'
            / 'steam_api64_ticket.dll')


def install_ticket_proxy(mirror_dir: Path) -> List[Path]:
    """Put the ticket proxy in front of Goldberg's steam_api64.dll (Game.goldbergRealTicket).

    Each Goldberg steam_api64.dll in the mirror (real files; the originals are
    symlinks into the install) is renamed to steam_api64_gbe.dll and the proxy
    takes its name. Returns the ticket files the proxy will read (one per
    proxied DLL), for steam_ticket.py to write. See src/ticketproxy/proxy.c.
    """
    proxy = ticket_proxy_dll()
    if not proxy.exists():
        raise RuntimeError(f"ticket proxy DLL missing ({proxy}); it is committed "
                           f"in the repo — restore it with `git checkout -- {proxy.parent}` "
                           f"or rebuild with `make -C src/ticketproxy` "
                           f"(needs gcc-mingw-w64-x86-64)")
    tickets = []
    for dll in mirror_dir.rglob('steam_api64.dll'):
        if dll.is_symlink():
            continue
        dll.replace(dll.with_name('steam_api64_gbe.dll'))
        shutil.copy2(proxy, dll)
        ticket = dll.with_name('janus_ticket.bin')
        ticket.unlink(missing_ok=True)
        tickets.append(ticket)
        print(f"[janus] Real-ticket proxy installed: {dll.relative_to(mirror_dir)}", flush=True)
    if not tickets:
        print("[janus] Warning: Game.goldbergRealTicket: no Goldberg steam_api64.dll "
              "in the mirror (only 64-bit Proton games are supported)", flush=True)
    return tickets


def spawn_ticket_helper(appid: int, tickets: List[Path], watch_pid: int,
                        env: dict) -> Optional[subprocess.Popen]:
    """Start steam_ticket.py: real Steam session tickets for the proxy to serve."""
    if not tickets:
        return None
    # Host process, not the game's env: no LD_PRELOAD hook, no container vars,
    # but the real $HOME so libsteam_api finds the running Steam client.
    henv = {k: v for k, v in env.items()
            if not k.startswith(('LD_PRELOAD', 'PRESSURE_VESSEL', 'STEAM_COMPAT',
                                 'JANUS_', 'WINE', 'PROTON'))}
    henv['HOME'] = os.path.expanduser('~')
    cmd = [sys.executable, str(Path(__file__).parent / 'steam_ticket.py'),
           '--appid', str(appid), '--watch-pid', str(watch_pid)]
    for t in tickets:
        cmd += ['--out', str(t)]
    return subprocess.Popen(cmd, env=henv)


def write_goldberg_invite_settings(mirror_dir: Path,
                                   gse_app_path: Optional[Path] = None) -> None:
    """Enable gbe_fork in-game invites between instances (Game.goldbergInvites).

    Writes EMPTY ``auto_send_invite.txt`` + ``auto_accept_invite.txt`` (empty =
    "any friend") into every ``steam_settings/`` gbe_fork may read:

    * auto_send: ActivateGameOverlayInviteDialog ("Invite players") sends a real
      InviteUserToLobby to every LAN friend — the other instances — instead of
      only opening the (absent) overlay.
    * auto_accept: accept without an overlay prompt. With no overlay a received
      lobby invite already fires GameLobbyJoinRequested; kept for overlay builds.

    gbe_fork reads ``steam_settings/`` next to its steam_api library, except
    when ``GseAppPath`` is set (Janus pins it for the debug build's lobby log),
    which moves it under that path — so write both. Requires LAN discovery on,
    so the instances see each other as friends. See NOTES.md.
    """
    dirs = set()
    for lib in mirror_dir.rglob('*'):
        # Goldberg libs are the only real (copied) steam_api files in the mirror;
        # everything else is a symlink into the install dir.
        if (lib.name.lower() in _STEAM_API_FILES and lib.is_file()
                and not lib.is_symlink()):
            dirs.add(lib.parent / 'steam_settings')
    if gse_app_path is not None:
        dirs.add(Path(gse_app_path) / 'steam_settings')
    for d in sorted(dirs):
        d.mkdir(parents=True, exist_ok=True)
        for name in ('auto_send_invite.txt', 'auto_accept_invite.txt'):
            (d / name).write_text('')
    print(f"[janus] Goldberg invites enabled ({len(dirs)} steam_settings dir(s))",
          flush=True)


def write_goldberg_config(home_dir: Path, appid: int, instance: int,
                          compat_data_path: Optional[Path] = None,
                          no_overlay: bool = True,
                          disable_lan: bool = True,
                          account_name: Optional[str] = None,
                          steam_id: Optional[str] = None,
                          language: Optional[str] = None):
    """
    Write per-instance Goldberg Steam Emulator configuration.

    Creates settings files that give each instance a unique Steam ID and name,
    allowing them to discover each other on LAN.

    For native Linux games: writes txt files to ~/.local/share/Goldberg SteamEmu Saves/
    For Proton games: writes INI config to <prefix>/pfx/drive_c/users/steamuser/AppData/Roaming/GSE Saves/
    (gbe_fork's Windows DLL uses 'GSE Saves' with INI format, not the old txt files)
    """
    # Identity = the instance's player profile (build_environment).
    steam_id = steam_id or profiles.default_for_instance(instance).steam_id
    account_name = account_name or profiles.default_for_instance(instance).name
    language = language or 'english'

    ini_content = (
        '[user::general]\n'
        f'account_name={account_name}\n'
        f'account_steamid={steam_id}\n'
        f'language={language}\n'
        'ip_country=US\n'
    )

    # Disable gbe_fork's LAN auto-discovery when requested. Without this, two
    # split instances on the same machine broadcast their emulated Steam
    # presence on UDP and auto-connect via the game's matchmaking, then the
    # netcode deadlocks (Lethal Company: both instances enter a half-connected
    # state and all Unity threads stall in futex_wait_multiple). With LAN-only
    # off, the connection is routed through gbe_fork's emulated Steam lobby
    # (loopback-friendly) instead, and the handshake actually completes.
    main_ini = (
        '[main::connectivity]\n'
        'disable_lan_only=1\n'
        'disable_source_query=1\n'
    ) if disable_lan else None

    # Native Linux: gbe_fork uses ~/.local/share/GSE Saves/ (INI format)
    gse_linux = home_dir / '.local' / 'share' / 'GSE Saves' / 'settings'
    gse_linux.mkdir(parents=True, exist_ok=True)
    (gse_linux / 'configs.user.ini').write_text(ini_content)
    if main_ini is not None:
        (gse_linux / 'configs.main.ini').write_text(main_ini)

    # Also write old-format txt files for compatibility
    settings_dir = home_dir / '.local' / 'share' / 'Goldberg SteamEmu Saves' / 'settings'
    settings_dir.mkdir(parents=True, exist_ok=True)
    (settings_dir / 'account_name.txt').write_text(account_name)
    (settings_dir / 'user_steam_id.txt').write_text(steam_id)

    game_settings = home_dir / '.local' / 'share' / 'Goldberg SteamEmu Saves' / str(appid) / 'settings'
    game_settings.mkdir(parents=True, exist_ok=True)
    (game_settings / 'steam_appid.txt').write_text(str(appid))

    # Proton: INI config in Wine prefix's %APPDATA%/GSE Saves/ (gbe_fork format)
    if compat_data_path is not None:
        appdata = compat_data_path / 'pfx' / 'drive_c' / 'users' / 'steamuser' / 'AppData' / 'Roaming'
        gse_settings = appdata / 'GSE Saves' / 'settings'
        gse_settings.mkdir(parents=True, exist_ok=True)
        ini_content = (
            '[user::general]\n'
            f'account_name={account_name}\n'
            f'account_steamid={steam_id}\n'
            f'language={language}\n'
            'ip_country=US\n'
        )
        (gse_settings / 'configs.user.ini').write_text(ini_content)
        if main_ini is not None:
            (gse_settings / 'configs.main.ini').write_text(main_ini)

        overlay_ini = gse_settings / 'configs.overlay.ini'
        overlay_ini.write_text(
            '[overlay::general]\n'
            f'enable_experimental_overlay={0 if no_overlay else 1}\n'
        )

    # Native Linux: mirror overlay state in GSE Saves and old config location
    linux_overlay_ini = gse_linux / 'configs.overlay.ini'
    old_disable_marker = (
        home_dir / '.local' / 'share' / 'Goldberg SteamEmu Saves'
        / 'settings' / 'disable_overlay.txt'
    )
    linux_overlay_ini.write_text(
        '[overlay::general]\n'
        f'enable_experimental_overlay={0 if no_overlay else 1}\n'
    )
    if no_overlay:
        old_disable_marker.parent.mkdir(parents=True, exist_ok=True)
        old_disable_marker.write_text('')
    else:
        old_disable_marker.unlink(missing_ok=True)

    print(f"[janus] Wrote Goldberg config for instance {instance} ({account_name}) in {home_dir}", flush=True)


def apply_hook_env(env: Dict[str, str], game_key, instance: int,
                   controller: Optional[int], verbose: bool,
                   controller_vid: int = 0, controller_pid: int = 0,
                   controller_name: Optional[str] = None,
                   steam_input_filter: bool = False) -> List[Path]:
    """Preload the hook into *env* and give it this instance's controller
    (in place). Shared by Steam and non-Steam launches (nonsteam.py).
    Returns the preloaded hook libraries ([] with JANUS_NO_HOOK=1)."""
    hook_paths = get_hook_paths()
    if os.environ.get('JANUS_NO_HOOK') == '1':
        # Diagnostic: launch exactly as usual but without the hook, to tell
        # hook-caused breakage from the rest of the launch pipeline.
        print("[janus] JANUS_NO_HOOK=1: hook library NOT preloaded", flush=True)
        hook_paths = []
    if hook_paths:
        preload_str = ':'.join(str(p) for p in hook_paths)
        existing = env.get('LD_PRELOAD', '')
        if existing:
            env['LD_PRELOAD'] = f"{preload_str}:{existing}"
        else:
            env['LD_PRELOAD'] = preload_str

    if controller is not None:
        env['JANUS_CONTROLLER'] = str(controller)

    # Per-instance RANK (0-based). Used by the hook as a DISTINCT tie-breaker when
    # identical MAC-less pads make the physical->Steam-virtual match ambiguous: instance
    # rank r takes the r-th virtual pad (consistently sorted), so instances never collide
    # onto the same virtual node (worst case swapped, which the dynamic matcher corrects).
    env['JANUS_INSTANCE_RANK'] = str(max(0, instance - 1))

    # Steam handle of this instance's pad when it's one of several identical
    # pads, written by the session's PadMatcher (pad_matcher.py) and read by the
    # hook. Cleared here so a file from an earlier session can't apply.
    pad_map = pad_matcher.pad_map_path(game_key, instance)
    pad_map.parent.mkdir(parents=True, exist_ok=True)
    try:
        pad_map.unlink()
    except OSError:
        pass
    env['JANUS_PAD_MAP'] = str(pad_map)
    # The size the game should render at, rewritten when the tiles re-flow
    # (dynamic players; resize.py). Same directory, so already shared below.
    import resize
    env['JANUS_SIZE_FILE'] = str(resize.size_file(game_key, instance))
    existing_ro = env.get('PRESSURE_VESSEL_FILESYSTEMS_RO', '')
    env['PRESSURE_VESSEL_FILESYSTEMS_RO'] = (
        f"{existing_ro}:{pad_map.parent}" if existing_ro else str(pad_map.parent))

    # Steam Input vtable filter: keep Steam Input active per-instance (instead of
    # nulling it) and filter GetConnectedControllers to this instance's assigned
    # handle. Needed for controllers that only work via Steam Input (no SDL mapping).
    if steam_input_filter:
        env['JANUS_STEAM_INPUT_FILTER'] = '1'

    # Pass controller *identity* (VID:PID + name) so the hook binds by hardware,
    # not by bare kernel index. Wireless pads (BT Xbox/DS4) drop and renumber
    # constantly; an index that was correct at launch can slide onto a different
    # pad seconds later, misaligning the per-instance assignment (classic
    # "one controller drives both instances"). The hook prefers VID:PID, keeps
    # the index as a tie-breaker for duplicate pads, and re-matches on hot-plug.
    if controller_vid and controller_pid:
        env['JANUS_CONTROLLER_VID'] = str(controller_vid)
        env['JANUS_CONTROLLER_PID'] = str(controller_pid)
    if controller_name:
        env['JANUS_CONTROLLER_NAME'] = controller_name

    if verbose:
        env['JANUS_VERBOSE'] = '1'

        # Per-instance persistent hook log. The hook appends ".<pid>" per process,
        # so each instance's Wine processes land in their own files even inside
        # gamescope/pressure-vessel (which swallow stderr). Lets us diff a real
        # multi-instance session and catch intermittent controller-filter races.
        log_dir = Path.home() / '.local' / 'share' / 'janus' / 'logs' / str(game_key)
        log_dir.mkdir(parents=True, exist_ok=True)
        env['JANUS_LOG_FILE'] = str(log_dir / f'i{instance}')
        # Share the log dir with the pressure-vessel container (belt-and-suspenders;
        # $HOME is usually already visible, but not guaranteed on all runtimes).
        existing_fs = env.get('PRESSURE_VESSEL_FILESYSTEMS_RW', '')
        env['PRESSURE_VESSEL_FILESYSTEMS_RW'] = (
            f"{existing_fs}:{log_dir}" if existing_fs else str(log_dir)
        )
    return hook_paths


# GPU-offload variables (NVIDIA PRIME render offload, the Vulkan optimus layer,
# AMD's switchable-graphics layer) that wrap_in_gamescope never lets through.
OFFLOAD_ENV = ('__NV_PRIME_RENDER_OFFLOAD', '__NV_PRIME_RENDER_OFFLOAD_PROVIDER',
               '__GLX_VENDOR_LIBRARY_NAME', '__VK_LAYER_NV_optimus',
               'DISABLE_LAYER_AMD_SWITCHABLE_GRAPHICS_1')


def wrap_in_gamescope(cmd: List[str], env: Dict[str, str],
                      gamescope_args: List[str], nvidia_prime: bool = False,
                      wmclass: Optional[str] = None) -> List[str]:
    """*cmd* run inside gamescope; sets gamescope's own env vars in *env*
    (in place). Raises FileNotFoundError when gamescope is unusable."""
    # Absolute path: Debian/Ubuntu install it in /usr/games, which isn't
    # on every PATH (desktop entries, Steam shortcuts).
    import prereqs
    problem = prereqs.gamescope_problem()
    if problem:
        raise FileNotFoundError(problem)
    gamescope_cmd = [prereqs.find_gamescope()] + gamescope_args + ['--']
    # GPU offload is Janus's own choice (nvidia_prime, for the game only).
    # Inherited offload vars (Steam sets them for a shortcut on a hybrid-GPU
    # laptop) made gamescope composite on one GPU while the game rendered on
    # the other: the tiles never mapped, the game ran unseen (NOTES.md
    # "Big Picture from Steam: tiles never appeared").
    for var in OFFLOAD_ENV:
        env.pop(var, None)
    game_env = []
    if nvidia_prime:
        game_env += ['__NV_PRIME_RENDER_OFFLOAD=1', '__GLX_VENDOR_LIBRARY_NAME=nvidia']
    # Diagnostics: variables for the game only, not gamescope (e.g. MangoHud,
    # which in gamescope itself would log gamescope's own frames):
    # JANUS_GAME_ENV="MANGOHUD=1;MANGOHUD_CONFIG=autostart_log=5,output_folder=/x"
    game_env += [kv.strip() for kv in env.get('JANUS_GAME_ENV', '').split(';')
                 if '=' in kv]
    if game_env:
        gamescope_cmd.extend(['env', *game_env])

    # gamescope-side shim: the desktop arrow over a tile that doesn't have
    # the mouse (src/hook/gamescope_shim.c). It removes itself from
    # LD_PRELOAD on load, so only gamescope gets it, never the game.
    shim = Path(__file__).resolve().parent.parent.parent / 'build' / 'libjanus_gamescope.so'
    if shim.exists():
        existing = env.get('LD_PRELOAD', '')
        env['LD_PRELOAD'] = f"{shim}:{existing}" if existing else str(shim)

    # Set environment for gamescope window behavior
    env['SDL_VIDEODRIVER'] = 'x11'
    if wmclass:
        # Per-instance window identity so KWin window rules can match each
        # gamescope tile (Wayland "Path A" — see wayland_session.py). gamescope's
        # SDL backend picks this up for its host window; covers both the XWayland
        # (x11 driver, set above) and native-Wayland cases.
        env['SDL_VIDEO_X11_WMCLASS'] = wmclass
        env['SDL_VIDEO_WAYLAND_WMCLASS'] = wmclass
    return gamescope_cmd + cmd


def apply_perf_env(env: Dict[str, str], fps_limit: Optional[int], proton: bool,
                   library: Optional[Path] = None, appid=None) -> None:
    """Frame limit + (Steam games) the shared shader cache, see perf.py."""
    import perf
    limit = perf.DEFAULT_FPS_LIMIT if fps_limit is None else int(fps_limit)
    how = perf.apply_fps_limit(env, limit, proton)
    if limit > 0:
        print(f"[janus] Frame limit: {how or f'{limit} fps only for vsync games (install MangoHud for the rest)'}",
              flush=True)
    if library is not None and appid is not None:
        sc = perf.apply_shader_cache(env, library, appid)
        if sc:
            print(f"[janus] Shader cache: {sc}", flush=True)


def apply_janus_input_env(env: Dict[str, str], nodes: List[str]) -> None:
    """Janus input mode: the hook allows exactly *nodes* (this instance's
    virtual pad) and refuses other pads and hidraw — the fallback for when the
    private /dev can't be made, and harmless inside it. No Steam Input
    filtering: the game's Steam Input is off."""
    env['JANUS_ALLOW_DEVICES'] = ':'.join(nodes)
    env['JANUS_INPUT_MODE'] = 'janus'
    for var in ('JANUS_STEAM_INPUT_FILTER', 'JANUS_CONTROLLER', 'JANUS_CONTROLLER_VID',
                'JANUS_CONTROLLER_PID', 'JANUS_CONTROLLER_NAME'):
        env.pop(var, None)


def wrap_input_namespace(cmd: List[str], nodes: List[str], instance: int) -> List[str]:
    """*cmd* in a private /dev showing only *nodes* (input_ns.py); unchanged,
    with a log line, where user namespaces are unavailable (the hook's
    allowlist then does the job alone)."""
    import input_ns
    if input_ns.available():
        out = input_ns.wrap(cmd, nodes)
        print(f"[janus] Janus input: instance {instance} sees only "
              f"{' '.join(nodes)}", flush=True)
    else:
        out = list(cmd)
        print("[janus] Janus input: no unprivileged user namespaces (bubblewrap "
              "fails); only the hook keeps the other pads away", flush=True)
    return out


def build_environment(
    game: SteamGame,
    controller: Optional[int],
    instance: int,
    isolation: str,
    verbose: bool,
    copy_prefix: bool = False,
    copy_userdata: bool = False,
    goldberg_enabled: bool = False,
    goldberg_no_overlay: bool = True,
    goldberg_disable_lan: bool = True,
    goldberg_debug: bool = False,
    controller_vid: int = 0,
    controller_pid: int = 0,
    controller_name: Optional[str] = None,
    steam_input_filter: bool = False,
    goldberg_language: Optional[str] = None,
    profile: Optional['profiles.Profile'] = None,
) -> Dict[str, str]:
    """Build environment for Steam Runtime launch.

    profile: who plays this instance (profiles.py). Decides the storage
    (profiles/<id>/<appid>/ when isolated) and the Goldberg identity, and
    whether this instance may use the real home/prefix: only the owner (the
    Steam account) does, under 'secondary'. Default: the instance's legacy
    profile (owner for instance 1).
    goldberg_language: Goldberg's language (Game.goldbergLanguage).
    """
    if profile is None:
        profile = profiles.default_for_instance(instance)
    env = os.environ.copy()

    # Isolation has two independent axes (kept separate so we can isolate the
    # minimum that two instances actually clash on):
    #   - isolate_prefix: give this instance its own Wine prefix (saves, registry,
    #     pfx.lock). This is the real per-instance state a Proton game writes.
    #   - isolate_home:   unshare $HOME via pressure-vessel. This HIDES the real
    #     ~/.steam / ~/.local/share/Steam, which breaks Steam Input's controller
    #     UI (menu navigation, on-screen keyboard) and input signals. Only
    #     'full'/'secondary' do it; 'prefix' isolates the prefix but keeps the
    #     real home, so the controller keeps working for local co-op.
    # 'secondary' = everyone but the owner (the real Steam account) is isolated,
    # so the real saves are only ever played by their owner, whatever player
    # number they have (and by nobody when the owner isn't playing).
    isolate_home = (
        isolation == 'full' or
        (isolation == 'secondary' and not profile.owner)
    )
    isolate_prefix = (
        isolation in ('full', 'prefix') or
        (isolation == 'secondary' and not profile.owner)
    )
    should_isolate = isolate_home or isolate_prefix

    hook_paths = apply_hook_env(
        env, game.appid, instance, controller, verbose,
        controller_vid=controller_vid, controller_pid=controller_pid,
        controller_name=controller_name, steam_input_filter=steam_input_filter)
    # Share with the pressure-vessel container: the hook directory, the scout
    # runtime (scout-on-soldier writes its var/ there) and, when not isolating,
    # host /tmp (isolated instances keep their own: lock file conflicts). The
    # last two don't depend on the hook: with JANUS_NO_HOOK=1 they used to be
    # skipped too and scout games died on a read-only var/.
    shared_paths = []
    if hook_paths:
        shared_paths.append(str(hook_paths[0].parent))
    scout_runtime = find_runtime(game.steam_root, RUNTIME_SCOUT)
    if scout_runtime:
        shared_paths.append(str(scout_runtime))
    if not should_isolate:
        shared_paths.append('/tmp')
    existing_fs = env.get('PRESSURE_VESSEL_FILESYSTEMS_RW', '')
    if existing_fs:
        shared_paths.append(existing_fs)
    if shared_paths:
        env['PRESSURE_VESSEL_FILESYSTEMS_RW'] = ':'.join(shared_paths)

    # Instance isolation via pressure-vessel environment variables
    if should_isolate:
        # The profile's storage for this game: the same person gets the same
        # home/prefix whichever instance number they play as.
        instance_base = profile.data_dir(game.appid)
        instance_home = instance_base / 'home'
        instance_tmp = instance_base / 'tmp'
        instance_home.mkdir(parents=True, exist_ok=True)
        instance_tmp.mkdir(parents=True, exist_ok=True)

        if isolate_home:
            env['PRESSURE_VESSEL_SHARE_HOME'] = '0'  # Enable --unshare-home
            env['PRESSURE_VESSEL_HOME'] = str(instance_home)  # Set --home

            # With the real $HOME unshared, the hook can no longer find
            # ~/.steam/steam/config/virtualgamepadinfo.txt (Steam's physical->virtual
            # controller map). Without it the hook decides "no Steam Input", falls
            # back to direct index matching, and leaks Steam's virtual pads to the
            # game (duplicate/phantom controller input -> menu glitches). Bind the
            # real Steam config dir read-only into the container and point the hook
            # straight at the file via JANUS_VGI_PATH. (Not needed when the home is
            # shared — the 'prefix' mode — because the real path is already visible.)
            steam_config_dir = (Path.home() / '.steam' / 'steam' / 'config').resolve()
            if steam_config_dir.is_dir():
                ro_paths = [str(steam_config_dir)]
                existing_ro = env.get('PRESSURE_VESSEL_FILESYSTEMS_RO', '')
                if existing_ro:
                    ro_paths.append(existing_ro)
                env['PRESSURE_VESSEL_FILESYSTEMS_RO'] = ':'.join(ro_paths)
                env['JANUS_VGI_PATH'] = str(steam_config_dir / 'virtualgamepadinfo.txt')

        # Use Steam's existing pressure-vessel cache (700MB+) by default.
        # Only isolate it per-instance when full copy is requested.
        if copy_prefix:
            instance_var = instance_base / 'var'
            instance_var.mkdir(parents=True, exist_ok=True)
            env['PRESSURE_VESSEL_VARIABLE_DIR'] = str(instance_var)
        # Redirect temp files to avoid lock conflicts (checked before container starts)
        env['TMPDIR'] = str(instance_tmp)
        env['TEMP'] = str(instance_tmp)
        env['TMP'] = str(instance_tmp)

    # Steam environment
    env['SteamAppId'] = str(game.appid)
    env['SteamGameId'] = str(game.appid)

    # Proton environment
    if game.is_proton:
        if isolate_prefix:
            # Each isolated instance needs its own Wine prefix (saves, registry, etc.)
            instance_compat = instance_base / 'compatdata'

            # If an existing prefix was built with a different mode than requested,
            # rebuild it. Smart mode symlinks drive_c/windows to the source library,
            # but pressure-vessel doesn't bind-mount external Steam libraries
            # (e.g. /mnt/storage), so those symlinks dangle inside the container
            # and Proton's setup_prefix fails creating font symlinks.
            if instance_compat.exists():
                windows_path = instance_compat / 'pfx' / 'drive_c' / 'windows'
                has_symlinks = windows_path.is_symlink()
                if copy_prefix and has_symlinks:
                    print(f"[janus] Existing prefix is smart-mode but --copy-prefix requested; rebuilding...", flush=True)
                    shutil.rmtree(instance_compat)

            if not instance_compat.exists():
                if copy_prefix:
                    print(f"[janus] Full-copying Wine prefix for {profile.name}...", flush=True)
                    shutil.copytree(game.compat_data_path, instance_compat,
                                    symlinks=True, dirs_exist_ok=True)
                    print(f"[janus] Wine prefix ready (full copy)", flush=True)
                else:
                    print(f"[janus] Creating smart Wine prefix for {profile.name}...", flush=True)
                    _isolate_proton_prefix(game.compat_data_path, instance_compat, copy_userdata)
                    print(f"[janus] Wine prefix ready (symlink + copy)", flush=True)
            env['STEAM_COMPAT_DATA_PATH'] = str(instance_compat)
            # Smart-mode prefixes symlink drive_c/windows (and Program Files, etc.)
            # into the source compatdata. When that lives on an external Steam
            # library (e.g. /mnt/storage), pressure-vessel won't auto-bind it
            # because PRESSURE_VESSEL_SHARE_HOME=0 hides everything outside
            # PRESSURE_VESSEL_HOME. Share the source compatdata explicitly so
            # the symlinks resolve inside the container.
            if not copy_prefix:
                src_compat = str(game.compat_data_path)
                existing_fs = env.get('PRESSURE_VESSEL_FILESYSTEMS_RW', '')
                if existing_fs:
                    env['PRESSURE_VESSEL_FILESYSTEMS_RW'] = f"{existing_fs}:{src_compat}"
                else:
                    env['PRESSURE_VESSEL_FILESYSTEMS_RW'] = src_compat
        else:
            env['STEAM_COMPAT_DATA_PATH'] = str(game.compat_data_path)
        env['STEAM_COMPAT_CLIENT_INSTALL_PATH'] = str(game.steam_root)

    # Goldberg Steam Emulator setup
    if goldberg_enabled:
        # Write per-instance Goldberg config
        if should_isolate:
            goldberg_home = instance_home
        else:
            goldberg_home = Path.home()
        # For Proton: also write config to Wine prefix (gbe_fork INI format)
        proton_compat = None
        if game.is_proton:
            proton_compat = Path(env.get('STEAM_COMPAT_DATA_PATH', str(game.compat_data_path)))
        write_goldberg_config(goldberg_home, game.appid, instance,
                              compat_data_path=proton_compat,
                              no_overlay=goldberg_no_overlay,
                              disable_lan=goldberg_disable_lan,
                              account_name=profile.name,
                              steam_id=profile.steam_id,
                              language=goldberg_language)

        # Proton: disable Steam client integration so Goldberg's DLL takes over
        if game.is_proton:
            env['PROTON_DISABLE_LSTEAMCLIENT'] = '1'

        # Share Goldberg paths with pressure-vessel container.
        # - mirror_dir: the per-instance symlink mirror (cwd inside container)
        # - install_dir: so the mirror's symlinks resolve inside the container
        #   (pressure-vessel doesn't auto-bind external Steam libraries like /mnt/storage)
        # - goldberg_dir: so the dlopen hook can load JANUS_GOLDBERG_LIB inside
        #   the container (when PRESSURE_VESSEL_SHARE_HOME=0, real $HOME is hidden)
        mirror_dir = goldberg_mirror_dir(game.appid, instance)
        gb_dir = goldberg_dir(goldberg_debug)
        goldberg_shared = []
        if mirror_dir.exists():
            goldberg_shared.append(str(mirror_dir))
        goldberg_shared.append(str(game.install_dir))
        if gb_dir.exists():
            goldberg_shared.append(str(gb_dir))
        existing_fs = env.get('PRESSURE_VESSEL_FILESYSTEMS_RW', '')
        if existing_fs:
            env['PRESSURE_VESSEL_FILESYSTEMS_RW'] = ':'.join([existing_fs] + goldberg_shared)
        else:
            env['PRESSURE_VESSEL_FILESYSTEMS_RW'] = ':'.join(goldberg_shared)

        # Native Linux: tell our dlopen hook to redirect libsteam_api.so
        # 64-bit and 32-bit hooks use separate env vars (arch-specific)
        if not game.is_proton:
            goldberg_so = gb_dir / 'libsteam_api.so'
            if goldberg_so.exists():
                env['JANUS_GOLDBERG_LIB'] = str(goldberg_so)
            goldberg_so32 = gb_dir / 'libsteam_api32.so'
            if goldberg_so32.exists():
                env['JANUS_GOLDBERG_LIB32'] = str(goldberg_so32)

            # Hybrid: keep REAL Steam Input while Goldberg handles multiplayer.
            # Point the hook at Valve's ORIGINAL libsteam_api.so (untouched in the
            # install dir — Goldberg copies to a separate mirror) so it routes
            # ISteamInput/ISteamController there instead of Goldberg's stub. Requires
            # the real Steam client running. Arch-matched to the game.
            # Honor either the param (CLI --steam-input-filter) or the env the GUI
            # injects (JANUS_STEAM_INPUT_FILTER), so both launch paths get the hybrid.
            if steam_input_filter or env.get('JANUS_STEAM_INPUT_FILTER') == '1':
                want_32 = game.is_32bit
                for cand in sorted(game.install_dir.rglob('libsteam_api.so')):
                    try:
                        with open(cand, 'rb') as fh:
                            hdr = fh.read(5)
                    except OSError:
                        continue
                    if len(hdr) < 5 or hdr[:4] != b'\x7fELF':
                        continue
                    if (hdr[4] == 1) == want_32:   # ELFCLASS32 == 1
                        env['JANUS_STEAM_INPUT_REAL_LIB'] = str(cand)
                        print(f"[janus] Hybrid Steam Input: routing to real {cand}", flush=True)
                        break
                else:
                    # No libsteam_api.so on disk. Games that bundle it inside a jar
                    # (steamworks4j/LWJGL — Necesse) only extract it at runtime, so
                    # the hook captures the path in its dlopen redirect instead.
                    print("[janus] Hybrid Steam Input: no libsteam_api.so in the install dir "
                          "— hook will capture it at dlopen time", flush=True)

        # GseAppPath (gbe_fork log dir) is set in launch_steam_game() AFTER
        # create_goldberg_mirror() runs — the mirror is wiped+recreated each
        # launch, which would destroy a log dir created here.

    return env


# Steam Linux Runtimes (Steam "tools"), by appid. Found through their
# appmanifest in ANY library of the Steam root: Steam installs a runtime into
# the library of the first game that needed it, not always the main one.
RUNTIME_SCOUT = 1070560      # 32-bit native games, via scout-on-soldier
RUNTIME_SOLDIER = 1391110
RUNTIME_SNIPER = 1628350     # native games; Proton 8/9, GE-Proton
RUNTIME_NAMES = {
    RUNTIME_SCOUT: 'Steam Linux Runtime 1.0 (scout)',
    RUNTIME_SOLDIER: 'Steam Linux Runtime 2.0 (soldier)',
    RUNTIME_SNIPER: 'Steam Linux Runtime 3.0 (sniper)',
    4183110: 'Steam Linux Runtime 4.0',
}


def runtime_name(appid: int) -> str:
    return RUNTIME_NAMES.get(appid, f'Steam Linux Runtime (app {appid})')


def steamapps_dirs(steam_root: Path) -> List[Path]:
    """The root's main steamapps dir + every library's, deduped."""
    main = Path(steam_root) / 'steamapps'
    dirs = [main] if main.is_dir() else []
    for key, folder in parse_vdf(main / 'libraryfolders.vdf').get(
            'libraryfolders', {}).items():
        if not key.isdigit():
            continue
        path = folder.get('path') if isinstance(folder, dict) else folder
        sa = Path(path) / 'steamapps' if path else None
        if sa and sa.is_dir() and all(not sa.samefile(d) for d in dirs):
            dirs.append(sa)
    return dirs


def find_app_install_dir(appid: int) -> Optional[Path]:
    """Install dir of Steam app *appid* in any library of any Steam root, or
    None if it isn't installed (no manifest, or its dir is missing)."""
    try:
        d = SteamGame(appid).install_dir
    except FileNotFoundError:
        return None
    return d if d.name != 'common' and d.is_dir() else None


def find_runtime(steam_root: Path, appid: int) -> Optional[Path]:
    """Install dir of the Steam Linux Runtime *appid*, if it's installed (has
    its entry point; scout's is scout-on-soldier-entry-point-v2)."""
    entry = ('scout-on-soldier-entry-point-v2' if appid == RUNTIME_SCOUT
             else '_v2-entry-point')
    for sa in steamapps_dirs(steam_root):
        state = parse_vdf(sa / f'appmanifest_{appid}.acf').get('AppState', {})
        d = state.get('installdir')
        if d and (sa / 'common' / d / entry).is_file():
            return sa / 'common' / d
    return None


def proton_required_runtime(proton_path: Optional[Path]) -> Optional[int]:
    """The Steam Linux Runtime a Proton build asks for (toolmanifest.vdf
    ``require_tool_appid``), like Steam uses. None if it doesn't say."""
    if not proton_path:
        return None
    try:
        manifest = (Path(proton_path) / 'toolmanifest.vdf').read_text()
    except OSError:
        return None
    m = re.search(r'"require_tool_appid"\s+"(\d+)"', manifest)
    return int(m.group(1)) if m else None


class RuntimeChoice:
    """How build_command wraps a game: *wrapper* goes in front of the game's
    command (``proton run …`` for Proton games), [] = launched bare.
    *missing* lists the runtimes the game wanted that aren't installed."""

    def __init__(self, wrapper: List[str], label: str, missing: List[int]):
        self.wrapper = wrapper
        self.label = label
        self.missing = missing


def select_runtime(game: SteamGame,
                   runtime_override: Optional[str] = None) -> RuntimeChoice:
    """The Steam Linux Runtime a game is launched in (see build_command)."""
    root = game.steam_root
    newest = find_runtime(root, RUNTIME_SNIPER) or find_runtime(root, RUNTIME_SOLDIER)
    missing: List[int] = []

    def native(runtime: Path, label: str, scout: Optional[Path] = None):
        wrapper = [str(runtime / '_v2-entry-point'), '--verb=waitforexitandrun', '--']
        if scout:
            wrapper += [str(scout / 'scout-on-soldier-entry-point-v2'), '--']
        return RuntimeChoice(wrapper, label, missing)

    if game.is_proton:
        # The runtime this Proton declares, exactly like Steam. Hardcoding
        # sniper broke Proton Experimental, which now targets Steam Linux
        # Runtime 4: Wine's TLS (gnutls) misbehaves on the older runtime, so
        # Epic Online Services HTTPS failed ("SSL connect error") and
        # EOS-backed multiplayer (Palworld) reported "multiplayer not
        # available". See NOTES.md.
        wanted = proton_required_runtime(game.proton_path) or RUNTIME_SNIPER
        runtime = find_runtime(root, wanted)
        if runtime is None:
            missing.append(wanted)
            runtime = newest
        if runtime is None:
            return RuntimeChoice([], 'none (proton run directly)', missing)
        return RuntimeChoice([str(runtime / '_v2-entry-point'), '--verb=run', '--'],
                             f'{runtime.name} (Proton)', missing)

    # Native games. Game.runtime (handler) first; if what it names isn't
    # installed, fall back to the automatic choice.
    if runtime_override == 'none':
        return RuntimeChoice([], 'none (Game.runtime)', missing)
    if runtime_override == 'scout':
        scout = find_runtime(root, RUNTIME_SCOUT)
        if newest and scout:
            return native(newest, f'{newest.name} + scout-on-soldier (Game.runtime)', scout)
        missing += [RUNTIME_SCOUT] if not scout else [RUNTIME_SNIPER]
    elif runtime_override in ('soldier', 'sniper'):
        wanted = RUNTIME_SOLDIER if runtime_override == 'soldier' else RUNTIME_SNIPER
        runtime = find_runtime(root, wanted)
        if runtime:
            return native(runtime, f'{runtime.name} (Game.runtime)')
        missing.append(wanted)

    if game.is_32bit:
        scout = find_runtime(root, RUNTIME_SCOUT)
        if newest and scout:
            return native(newest, f'{newest.name} + scout-on-soldier (32-bit native)', scout)
        if not scout and RUNTIME_SCOUT not in missing:
            missing.append(RUNTIME_SCOUT)
    if newest:
        return native(newest, f'{newest.name} (native)')
    if RUNTIME_SNIPER not in missing:
        missing.append(RUNTIME_SNIPER)
    return RuntimeChoice([], 'none (direct launch)', missing)


def build_command(game: SteamGame, extra_args: Optional[List[str]] = None,
                  exe_name: Optional[str] = None,
                  launch_command: Optional[str] = None,
                  runtime_override: Optional[str] = None) -> List[str]:
    """Build launch command with appropriate runtime wrapper.

    runtime_override (native games only; from a handler script's
    Game.runtime): 'scout' forces the scout-on-soldier stack (needed by
    scout-era games whose libs — e.g. libcrypto.so.1.0.0 — exist in no newer
    runtime, even 64-bit ones like Nuclear Throne), 'soldier'/'sniper' pin a
    specific runtime, 'none' launches bare.
    """
    launch_command = launch_command or getattr(game, 'launch_command', None)
    if launch_command:
        # The user said exactly what to run, so don't graft detect_game_args()
        # on top of it — their command already carries whatever the game needs.
        exe, args = resolve_launch_command(game.install_dir, launch_command)
        args = args + (extra_args or [])
    else:
        exe = find_game_executable(game, exe_name=exe_name)
        args = detect_game_args(game) + (extra_args or [])

    if game.is_proton:
        proton_path = game.proton_path
        if not proton_path:
            raise FileNotFoundError(
                f"Proton not found for app {game.appid}. "
                "Run the game through Steam once first to set up Proton."
            )
        prefix = [str(proton_path / 'proton'), 'run']
    else:
        prefix = []

    choice = select_runtime(game, runtime_override)
    for appid in choice.missing:
        print(f"[janus] Warning: {runtime_name(appid)} isn't installed "
              f"(steam://install/{appid}); using: {choice.label}", flush=True)
    return [*choice.wrapper, *prefix, str(exe), *args]


def launch_steam_game(
    appid: int,
    controller: Optional[int] = None,
    instance: int = 1,
    isolation: str = 'none',  # 'none', 'secondary', 'full'
    verbose: bool = False,
    extra_args: Optional[List[str]] = None,
    use_runtime: bool = True,
    gamescope_args: Optional[List[str]] = None,
    nvidia_prime: bool = False,
    copy_prefix: bool = False,
    copy_userdata: bool = False,
    goldberg_enabled: bool = False,
    goldberg_no_overlay: bool = True,
    goldberg_disable_lan: bool = True,
    goldberg_debug: bool = False,
    exe_name: Optional[str] = None,
    launch_command: Optional[str] = None,
    wmclass: Optional[str] = None,
    controller_vid: int = 0,
    controller_pid: int = 0,
    controller_name: Optional[str] = None,
    steam_input_filter: bool = False,
    script=None,
    script_ctx: Optional[Dict] = None,
    profile: Optional[str] = None,
    input_nodes: Optional[List[str]] = None,
    fps_limit: Optional[int] = None,
) -> subprocess.Popen:
    """
    Launch a Steam game with controller assignment and optional isolation.

    Args:
        appid: Steam app ID
        controller: Controller index to assign
        instance: Instance number (1 = primary, 2+ = secondary)
        isolation: 'none', 'secondary' (2+ isolated), 'full' (all isolated)
        verbose: Enable verbose logging
        extra_args: Extra arguments to pass to game
        use_runtime: Whether to use Steam Runtime (default True)
        gamescope_args: Arguments to pass to gamescope (wraps game in gamescope)
        nvidia_prime: Add NVIDIA PRIME offload environment variables
        copy_userdata: Copy user data (saves/config) from original prefix
        goldberg_enabled: Use Goldberg Steam Emulator for LAN multiplayer
        goldberg_no_overlay: Disable Goldberg overlay (prevents freezes in Proton)
        goldberg_disable_lan: Disable Goldberg LAN auto-discovery (prevents the
            Lethal-Company-class freeze where two same-host instances auto-join
            each other's LAN-broadcast game and deadlock the netcode handshake)
        exe_name: Filename of the executable to launch (searched for in the
            install dir); overridden by launch_command when both are given
        launch_command: Full command line relative to the game's install dir,
            e.g. "./jre/bin/java -jar Necesse.jar". Replaces all executable
            auto-detection and the auto-detected game args.
        script: the game's scripting.HandlerScript — required: Janus launches
            no game without a handler (raises HandlerError). Fires the per-instance hook
            sequence documented in SCRIPTING.md: onGameResolved ->
            buildEnvironment -> onMirrorReady (goldberg only) -> buildCommand
            -> play -> onInstanceStarted. The caller owns session-level hooks
            (onAllStarted / onInstanceExit / onStop) and script.finish().
        script_ctx: Per-instance metadata only the caller knows, merged into
            the script's ctx object: {'width','height','posX','posY',
            'numPlayers','players'}; players = [{'instance', 'profile'}]
            with profile = Profile.ctx().
        profile: id or name of the player profile (profiles.py); None = the
            instance's default (owner for instance 1, "Player N" otherwise).
        input_nodes: Janus input mode (controller_input.py): this instance's virtual
            pad nodes from the session's input daemon (padd.py). The instance
            runs in a private /dev holding only those (input_ns.py) and the
            hook gets them as its allowlist instead of a controller to filter
            for; *controller* & co. then only describe the physical pad.
            None = Steam Input mode.
        fps_limit: the game's frame limit (perf.py; 0 = none, None = the
            default 60). gamescope's -r/-o are the caller's.

    Returns:
        subprocess.Popen object for the launched process
    """
    if script is None:
        import scripting
        raise scripting.HandlerError(scripting.no_handler_message(appid))
    if script.non_steam:
        # Game.id handlers: no Steam, runtime, Proton or isolation.
        import nonsteam
        return nonsteam.launch(
            appid, controller=controller, instance=instance, verbose=verbose,
            extra_args=extra_args, gamescope_args=gamescope_args,
            nvidia_prime=nvidia_prime, wmclass=wmclass,
            controller_vid=controller_vid, controller_pid=controller_pid,
            controller_name=controller_name,
            steam_input_filter=steam_input_filter, script=script,
            script_ctx=script_ctx, profile=profile, input_nodes=input_nodes,
            fps_limit=fps_limit)
    game = SteamGame(appid, launch_command=launch_command)
    player = profiles.resolve(profile, instance)
    print(f"[janus] Instance {instance} plays as {player.name}"
          f"{' (Steam account)' if player.owner else ''}", flush=True)

    # --- handler script: onGameResolved (may override exe / launch command)
    sctx = None
    if script is not None:
        script.set_install_dir(game.install_dir)
        script.set_base_dir(game.install_dir)
        r = script.invoke('onGameResolved', ctx={
            'appid': appid, 'gameName': game.name,
            'installDir': str(game.install_dir),
            'gameDir': str(game.install_dir),
            'isNative': game.is_native, 'isProton': game.is_proton,
            'is32bit': game.is_32bit,
        })
        ov = (r or {}).get('ret')
        if isinstance(ov, dict):
            if ov.get('exeName'):
                exe_name = str(ov['exeName'])
            if ov.get('launchCommand'):
                launch_command = str(ov['launchCommand'])
                game = SteamGame(appid, launch_command=launch_command)

    janus_input = input_nodes is not None
    if janus_input and game.is_proton and isolation == 'none' \
            and int((script_ctx or {}).get('numPlayers') or 1) > 1:
        # Instances sharing a prefix share one wineserver and one winebus,
        # which reads the pads for all of them: the first instance's private
        # /dev would decide what every instance sees.
        print("[janus] Janus input: each instance gets its own Wine prefix "
              "(isolation 'prefix')", flush=True)
        isolation = 'prefix'
    env = build_environment(game, None if janus_input else controller, instance, isolation,
                            verbose, copy_prefix, copy_userdata,
                            goldberg_enabled, goldberg_no_overlay, goldberg_disable_lan,
                            goldberg_debug,
                            controller_vid=0 if janus_input else controller_vid,
                            controller_pid=0 if janus_input else controller_pid,
                            controller_name=None if janus_input else controller_name,
                            steam_input_filter=steam_input_filter and not janus_input,
                            goldberg_language=(script.get('goldbergLanguage') if script else None),
                            profile=player)
    import resize
    if resize.mode(script):
        # The game follows $JANUS_SIZE_FILE windowed (Game.resizeGame): the
        # IL2CPP UI-scale DLL reads this to switch to windowed mode.
        env['JANUS_RESIZE'] = '1'
    if janus_input:
        apply_janus_input_env(env, input_nodes)
    apply_perf_env(env, fps_limit, proton=game.is_proton,
                   library=game.library_path, appid=game.appid)

    # --- handler script: sheet env toggles + buildEnvironment hook
    if script is not None:
        script.apply_env(env)
        meta = script_ctx or {}
        compat = env.get('STEAM_COMPAT_DATA_PATH') or (
            str(game.compat_data_path) if game.is_proton else None)
        sctx = {
            'appid': appid, 'gameName': game.name,
            'instance': instance, 'rank': instance - 1,
            'numPlayers': meta.get('numPlayers'),
            'profile': player.ctx(),
            # Everyone in the session ({instance, profile}), when the
            # launcher knows them (GUI, Big Picture, steam-split).
            'players': meta.get('players') or [
                {'instance': instance, 'profile': player.ctx()}],
            'saveRoot': str(player.data_dir(appid)),
            'profilesDir': str(profiles.DATA_DIR),
            'controller': {'index': controller, 'vid': controller_vid,
                           'pid': controller_pid, 'name': controller_name},
            'inputMode': 'janus' if janus_input else 'steam',
            'width': meta.get('width'), 'height': meta.get('height'),
            'posX': meta.get('posX'), 'posY': meta.get('posY'),
            'installDir': str(game.install_dir),
            'gameDir': str(game.install_dir),
            'home': env.get('PRESSURE_VESSEL_HOME', str(Path.home())),
            'winePrefix': (compat + '/pfx') if compat else None,
            'isNative': game.is_native, 'isProton': game.is_proton,
            'is32bit': game.is_32bit,
            'options': script.options,
            'env': {}, 'gameArgs': [],
            'verbose': verbose, 'pid': None, 'lobbyId': None,
        }
        def _absorb_hook_result(r):
            """Fold a hook's ctx mutations back into host state: gameArgs
            accumulate across hooks (appended to the command once, later);
            ctx.env merges into the spawn env, where a JS null deletes the
            key (the natural 'unset' idiom)."""
            if not r:
                return
            if isinstance(r.get('gameArgs'), list):
                sctx['gameArgs'] = [str(a) for a in r['gameArgs']]
            if isinstance(r.get('ctxEnv'), dict):
                for k, v in r['ctxEnv'].items():
                    if v is None:
                        env.pop(str(k), None)
                    else:
                        env[str(k)] = str(v)
                sctx['env'] = {k: v for k, v in r['ctxEnv'].items()
                               if v is not None}

        r = script.invoke('buildEnvironment', ctx=sctx, env=env)
        if r and isinstance(r.get('env'), dict):
            env = {str(k): str(v) for k, v in r['env'].items()
                   if v is not None}
        _absorb_hook_result(r)

    # Create the mirror (Goldberg / BepInEx)
    cwd = game.install_dir
    real_tickets: List[Path] = []
    # A per-instance mirror for Goldberg, or for BepInEx (Unity mods must not
    # touch the real install). Plain mirror (real steam_api) without Goldberg.
    # Game.steamStubExes: the unpacked exe replaces the wrapped one there too.
    use_mirror = (goldberg_enabled or bepinex.needed(script)
                  or unreal.mod_needed(script)
                  or bool(script and script.get('steamStubExes')))
    if use_mirror:
        if goldberg_enabled:
            gb_dir = goldberg_dir(goldberg_debug)
            if not gb_dir.exists():
                print(f"[janus] Warning: Goldberg libraries not found at {gb_dir}", flush=True)
                if goldberg_debug:
                    print(f"[janus] Place debug build (steam_api.dll, steam_api64.dll, libsteam_api.so, libsteam_api32.so) there", flush=True)
                else:
                    print(f"[janus] Place steam_api.dll, steam_api64.dll, and/or libsteam_api.so there", flush=True)
        mirror_dir = create_goldberg_mirror(
            game, instance, goldberg_debug,
            copy_instead=script.get('mirrorCopyInstead') if script else None,
            exclude=script.get('mirrorExclude') if script else None,
            replace_files=steamstub.replacements(
                game, script.get('steamStubExes') if script else None),
            goldberg=goldberg_enabled)
        cwd = mirror_dir
        # The container must see the mirror and the install its symlinks point
        # into (the Goldberg branch of build_environment shares them too).
        shared = [p for p in env.get('PRESSURE_VESSEL_FILESYSTEMS_RW', '').split(':') if p]
        for p in (str(mirror_dir), str(game.install_dir)):
            if p not in shared:
                shared.append(p)
        env['PRESSURE_VESSEL_FILESYSTEMS_RW'] = ':'.join(shared)
        if goldberg_enabled:
            # Make sure GseAppPath is set AFTER mirror exists (build_environment runs
            # before mirror creation, so it could skip the setting in some races).
            if goldberg_debug:
                log_dir = mirror_dir / '_janus_gbe_logs'
                log_dir.mkdir(parents=True, exist_ok=True)
                env['GseAppPath'] = str(log_dir)
                # Also share with pressure-vessel
                existing_fs = env.get('PRESSURE_VESSEL_FILESYSTEMS_RW', '')
                if str(log_dir) not in existing_fs:
                    env['PRESSURE_VESSEL_FILESYSTEMS_RW'] = (
                        f"{existing_fs}:{log_dir}" if existing_fs else str(log_dir)
                    )
            if script is not None and script.get('goldbergInvites'):
                if goldberg_disable_lan:
                    print("[janus] Warning: Game.goldbergInvites needs LAN discovery "
                          "(goldbergDisableLan = false) — instances won't see each "
                          "other as friends, so invites go nowhere", flush=True)
                write_goldberg_invite_settings(
                    mirror_dir, mirror_dir / '_janus_gbe_logs' if goldberg_debug else None)
            if script is not None and script.get('goldbergRealTicket'):
                real_tickets = install_ticket_proxy(mirror_dir)
            # Write overlay disable into game directory (gbe_fork checks here first)
            mirror_marker = mirror_dir / 'disable_overlay.txt'
            if goldberg_no_overlay:
                if mirror_marker.is_symlink():
                    mirror_marker.unlink()
                mirror_marker.write_text('')
            else:
                if mirror_marker.is_symlink() or mirror_marker.exists():
                    mirror_marker.unlink()

        # --- handler script: onMirrorReady (the mirror is wiped+rebuilt
        # every launch, so payload/mod installs MUST happen here, not earlier)
        if script is not None:
            sctx['gameDir'] = str(cwd)
            script.set_base_dir(cwd)
            _absorb_hook_result(script.invoke('onMirrorReady', ctx=sctx))

    # --- handler script: buildCommand (full argv replacement)
    script_cmd = None
    if script is not None:
        r = script.invoke('buildCommand', ctx=sctx)
        _absorb_hook_result(r)
        ret = (r or {}).get('ret')
        if isinstance(ret, list) and ret:
            # Treat the returned argv like an explicit launch command: first
            # token resolved relative to the install dir, runtime-wrapped.
            script_cmd = ' '.join(shlex.quote(str(t)) for t in ret)

    runtime_override = script.get('runtime') if script is not None else None
    if script_cmd is not None:
        if use_runtime:
            cmd = build_command(game, extra_args, launch_command=script_cmd,
                                runtime_override=runtime_override)
        else:
            exe, pre_args = resolve_launch_command(game.install_dir, script_cmd)
            cmd = [str(exe), *pre_args, *(extra_args or [])]
    elif use_runtime:
        cmd = build_command(game, extra_args, exe_name=exe_name,
                            runtime_override=runtime_override)
    elif game.launch_command:
        exe, pre_args = resolve_launch_command(game.install_dir, game.launch_command)
        cmd = [str(exe), *pre_args, *(extra_args or [])]
    else:
        exe = find_game_executable(game, exe_name=exe_name)
        cmd = [str(exe)] + (extra_args or [])

    # Rewrite command paths from original game dir to the mirror
    if use_mirror:
        install_str = str(game.install_dir)
        mirror_str = str(cwd)
        cmd = [arg.replace(install_str, mirror_str) if install_str in arg else arg for arg in cmd]
        # Unity mods (Game.unityUiScale / bepinexPlugins) go next to the exe.
        if script is not None and bepinex.needed(script):
            exe_arg = next((a for a in cmd if a.startswith(mirror_str)
                            and a.lower().endswith('.exe')), None)
            if exe_arg is None:
                script._user_message('warning', 'Unity mods not installed: '
                                     'no .exe in the launch command.')
            else:
                bepinex.deploy(script, cwd, Path(exe_arg), env, game.is_proton)
        # Unreal per-screen UI fit (Game.unrealUiFit): UE4SS next to the exe.
        if script is not None and unreal.mod_needed(script):
            exe_arg = next((a for a in cmd if a.startswith(mirror_str)
                            and a.lower().endswith('.exe')), None)
            if exe_arg is None:
                script._user_message('warning', 'UI scale not applied: '
                                     'no .exe in the launch command.')
            else:
                unreal.deploy(script, cwd, Path(exe_arg), env, game.is_proton)

    # --- handler script: play (the per-instance workhorse). Mutations to
    # ctx.gameArgs are appended to the game's command line; ctx.env merges
    # into the spawn environment. Runs before the gamescope wrap, but the
    # appended args stay at the end of the final command (game args).
    if script is not None:
        _absorb_hook_result(script.invoke('play', ctx=sctx))
        if sctx['gameArgs']:
            extra = list(sctx['gameArgs'])
            if use_mirror:
                # Same install->mirror rewrite the main command gets, so a
                # script arg built from ctx.installDir doesn't point the
                # goldberg'd game back at the un-mirrored tree.
                install_str = str(game.install_dir)
                mirror_str = str(cwd)
                extra = [a.replace(install_str, mirror_str)
                         if install_str in a else a for a in extra]
            cmd = cmd + extra
        # Game.unrealUiScale: the UI scale goes into the instance's Engine.ini
        # (after play, so a -UserDir= it added is honoured).
        unreal.apply(script, cmd, game.install_dir, sctx.get('winePrefix'),
                     sctx.get('home') or str(Path.home()))

    if janus_input:
        cmd = wrap_input_namespace(cmd, input_nodes, instance)

    # Wrap in gamescope if requested
    if gamescope_args is not None:
        cmd = wrap_in_gamescope(cmd, env, gamescope_args, nvidia_prime, wmclass)

    proc = subprocess.Popen(cmd, env=env, cwd=cwd)
    if real_tickets:
        # Started after the game so it can watch its pid; the proxy waits for
        # the ticket file, and the game needs far longer to reach its login.
        proc.janus_ticket_helper = spawn_ticket_helper(
            game.appid, real_tickets, proc.pid, env)
    if script is not None:
        sctx['pid'] = proc.pid
        # Stash the ctx on the Popen so orchestrators can pass it to
        # onInstanceExit without rebuilding it.
        proc.janus_script_ctx = sctx
        script.invoke('onInstanceStarted', ctx=sctx, soft=True)
    return proc


def library_appids(steam_root=None) -> set:
    """Appids in the Steam library, installed or not: Steam caches library
    art (appcache/librarycache/<appid>/) for every game the account has,
    which is the only local record of the whole library (the Web API needs
    a key). A heuristic: a few apps the account doesn't own (tools, a game
    once on a free weekend) may be included. Empty without Steam."""
    roots = [steam_root] if steam_root is not None else find_all_steam_roots()
    found = set()
    for root in roots:
        d = Path(root) / 'appcache' / 'librarycache'
        try:
            found.update(int(e.name) for e in d.iterdir()
                         if e.name.isdigit() and e.is_dir())
        except OSError:
            continue
    return found


def get_installed_games(steam_root=None) -> List[Dict]:
    """
    Scan all Steam library folders for installed games.

    If ``steam_root`` is None, every discovered Steam root (native, Flatpak,
    Snap) is searched; duplicates by appid are dropped (first-seen wins).

    Returns:
        List of dicts with keys: appid (int), name (str), library_path (str)
    """
    if steam_root is not None:
        roots = [steam_root]
    else:
        roots = find_all_steam_roots()
        if not roots:
            raise FileNotFoundError("Steam installation not found")

    # Collect all steamapps folders across all roots (main + library folders).
    steamapps_dirs: List[Path] = []
    for root in roots:
        main_steamapps = root / 'steamapps'
        if main_steamapps.is_dir():
            steamapps_dirs.append(main_steamapps)
        library_folders = parse_vdf(main_steamapps / 'libraryfolders.vdf')
        if not library_folders:
            continue
        for key, folder in library_folders.get('libraryfolders', {}).items():
            if not key.isdigit():
                continue
            folder_path = folder.get('path') if isinstance(folder, dict) else folder
            if folder_path:
                steamapps = Path(folder_path) / 'steamapps'
                if steamapps.is_dir():
                    steamapps_dirs.append(steamapps)

    # Dedupe steamapps dirs (a library may be referenced from multiple roots).
    deduped: List[Path] = []
    seen_dirs: set = set()
    for d in steamapps_dirs:
        key = str(d.resolve()) if d.exists() else str(d)
        if key in seen_dirs:
            continue
        seen_dirs.add(key)
        deduped.append(d)
    steamapps_dirs = deduped

    games = []
    seen = set()
    for steamapps in steamapps_dirs:
        for manifest_path in steamapps.glob('appmanifest_*.acf'):
            manifest = parse_vdf(manifest_path)
            app_state = manifest.get('AppState', {})
            appid_str = app_state.get('appid', '')
            name = app_state.get('name', '')
            if not appid_str or not name:
                continue
            appid = int(appid_str)
            if appid in seen:
                continue
            seen.add(appid)
            games.append({
                'appid': appid,
                'name': name,
                'library_path': str(steamapps),
            })

    games.sort(key=lambda g: g['name'].lower())
    return games


if __name__ == '__main__':
    # Game info only. Launching goes through main.py (`steam` / `steam-split`),
    # which loads the game's handler: there is no launch without one.
    import argparse

    parser = argparse.ArgumentParser(description='Steam game info (launch with main.py steam)')
    parser.add_argument('appid', type=int, help='Steam App ID')
    args = parser.parse_args()

    try:
        game = SteamGame(args.appid)
        print(f"App ID: {game.appid}")
        print(f"Name: {game.name}")
        print(f"Install dir: {game.install_dir}")
        print(f"Native: {game.is_native}")
        print(f"32-bit: {game.is_32bit}")
        try:
            print(f"Executable: {find_game_executable(game)}")
        except FileNotFoundError as e:
            print(f"Executable: {e}")
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

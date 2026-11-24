#!/usr/bin/env python3
"""
Per-game handler scripting for Janus.

Each game may ship a *handler script* — a JavaScript file executed by an
embedded QuickJS engine (quickjs-ng, vendored under vendor/quickjs/) — that
encodes everything Janus needs to know about that game: a property sheet of
launch options (``Game.*`` globals) plus optional lifecycle callbacks
(``Game.play = function (ctx) {...}``) invoked at fixed points in the launch
pipeline. Modeled on Nucleus Co-Op's Jint-based handlers. See SCRIPTING.md
for the authoritative environment reference.

Design constraints:
- Scripts cross the Python<->JS boundary as *plain data only* (JSON). Host
  functionality is exposed through a single dispatcher callable; the JS
  prelude wraps it in the ``Janus.*`` API.
- One ``HandlerScript`` per session; hook invocations are serialized with a
  lock (QuickJS contexts are not thread-safe).
- No launch without a handler: ``load_handler`` raises when the game has
  none (or the quickjs module is missing). There is no default behaviour.
"""

import atexit
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
from glob import glob as _glob
from pathlib import Path
from typing import Any, Dict, List, Optional

import version

# --- engine import (vendored fallback) -------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
# An installed copy (zip or .deb, scripts/package.sh) carries a VERSION file;
# a git checkout doesn't.
INSTALLED = (_REPO_ROOT / 'VERSION').is_file()
_VENDOR_QJS = _REPO_ROOT / 'vendor' / 'quickjs'

try:
    import quickjs
except ImportError:
    if _VENDOR_QJS.is_dir():
        sys.path.insert(0, str(_VENDOR_QJS))
    try:
        import quickjs
    except ImportError:
        quickjs = None


class HandlerError(Exception):
    """A handler script failed (syntax error, hook exception, bad API use)."""


# --- handler discovery ------------------------------------------------------

# A handler names its game in its file name (handlers/content-warning.js) and
# declares the appid it handles with a literal on its own line, which is
# read here WITHOUT evaluating the script:   Game.appid = 2881650;
_APPID_DECL = re.compile(r'^[ \t]*Game\.appid[ \t]*=[ \t]*(\d+)[ \t]*;?', re.M)
# A non-Steam game (nonsteam.py) declares a string id instead:
#   Game.id = "minecraft";
_ID_DECL = re.compile(
    r'^[ \t]*Game\.id[ \t]*=[ \t]*["\']([a-z0-9][a-z0-9_-]*)["\'][ \t]*;?', re.M)
_NAME_DECL = re.compile(r'^[ \t]*Game\.name[ \t]*=[ \t]*"([^"\n]+)"', re.M)

# A game key is a Steam appid (int) or a non-Steam game's id (str).
GAME_ID_RE = re.compile(r'^[a-z0-9][a-z0-9_-]*$')


def parse_game_key(text):
    """CLI/config text -> game key: digits are a Steam appid, anything else a
    non-Steam game id. Raises ValueError for neither."""
    text = str(text).strip()
    if text.isdigit():
        return int(text)
    if GAME_ID_RE.match(text):
        return text
    raise ValueError(f"'{text}' is neither a Steam appid nor a game id "
                     f"(lowercase letters, digits, - and _)")


def is_steam_key(key) -> bool:
    """Is *key* a Steam appid (not a non-Steam game id)?"""
    return isinstance(key, int) or (isinstance(key, str) and key.isdigit())


def user_handler_dir() -> Path:
    """Where hub installs and hand-written handlers live."""
    return Path.home() / '.config' / 'janus' / 'games'


def _handler_dirs() -> List[Path]:
    """Handler directories, highest priority first. In a git checkout the
    repo's handlers/ (development copies) win over user-local ones (hub
    installs, hand-written handlers). An installed copy ships no handlers:
    only the user's (from the hub) count."""
    if INSTALLED:
        return [user_handler_dir()]
    return [_REPO_ROOT / 'handlers', user_handler_dir()]


def _decl(rx, path: Path) -> Optional[str]:
    try:
        m = rx.search(path.read_text(errors='replace'))
    except OSError:
        return None
    return m.group(1) if m else None


def handler_appid(path: Path) -> Optional[int]:
    """The appid a handler script declares (`Game.appid = N;`), or None."""
    a = _decl(_APPID_DECL, path)
    return int(a) if a else None


def handler_id(path: Path) -> Optional[str]:
    """The non-Steam game id a handler declares (`Game.id = "...";`), or
    None. Only counts without a `Game.appid`: that makes it a Steam game."""
    if handler_appid(path) is not None:
        return None
    return _decl(_ID_DECL, path)


def handler_name(path: Path) -> str:
    """The `Game.name = "...";` a handler declares, or ''."""
    return _decl(_NAME_DECL, path) or ''


def _handler_files(d: Path) -> List[Path]:
    """Handler scripts in *d*: flat <name>.js and directory <name>/handler.js."""
    if not d.is_dir():
        return []
    return sorted(list(d.glob('*.js')) + list(d.glob('*/handler.js')))


def _handler_matches(d: Path, key) -> List[Path]:
    """Scripts in *d* for the game *key*. A Steam appid: `Game.appid`
    matches, or a legacy file named <appid>.js (or <appid>/handler.js)
    without the declaration. A non-Steam id: `Game.id` matches."""
    matches = []
    for p in _handler_files(d):
        if not is_steam_key(key):
            if handler_id(p) == key:
                matches.append(p)
            continue
        declared = handler_appid(p)
        legacy = (p.stem if p.name != 'handler.js' else p.parent.name) == str(key)
        if declared == int(key) or (declared is None and legacy
                                    and handler_id(p) is None):
            matches.append(p)
    return matches


def non_steam_handlers() -> Dict[str, Path]:
    """Every non-Steam game with a handler: {id: handler path}, the path
    find_handler(id) would use."""
    found: Dict[str, Path] = {}
    for d in _handler_dirs():
        for p in _handler_files(d):
            gid = handler_id(p)
            if gid:
                found.setdefault(gid, p)
    return found


def _short_path(p: Path) -> str:
    """A handler path for messages: repo-relative, or ~-relative."""
    for base, prefix in ((_REPO_ROOT, ''), (Path.home(), '~/')):
        try:
            return prefix + str(p.relative_to(base))
        except ValueError:
            pass
    return str(p)


def find_handler(appid: int) -> Optional[Path]:
    """The handler script for *appid*: the first match in _handler_dirs()
    order (see there and handler_conflict)."""
    for d in _handler_dirs():
        matches = _handler_matches(d, appid)
        if len(matches) > 1:
            print(f"[script] Warning: several handlers for app {appid} in {d}: "
                  f"{', '.join(m.name if m.name != 'handler.js' else m.parent.name + '/handler.js' for m in matches)}"
                  f" — using {matches[0].name}", flush=True)
        if matches:
            return matches[0]
    return None


def handler_conflict(appid: int) -> Optional[str]:
    """A user-facing warning when *appid* has a handler in more than one
    handler directory (typically the repo's development copy and a hub
    install), naming the one used; None when there is no conflict."""
    found = [m[0] for m in (_handler_matches(d, appid) for d in _handler_dirs()) if m]
    if len(found) < 2:
        return None
    others = ', '.join(_short_path(p) for p in found[1:])
    return (f"This game has more than one handler. Using {_short_path(found[0])} "
            f"(the development copy in the repo); ignoring {others}.")


def payload_dir_for(script_path: Path) -> Path:
    """The handler's bundled payload directory.

    handlers/<name>/handler.js -> handlers/<name>/payload/
    <anything>/<name>.js       -> <anything>/<name>.payload/
    """
    if script_path.name == 'handler.js':
        return script_path.parent / 'payload'
    return script_path.parent / f'{script_path.stem}.payload'


# --- property sheet field registry ------------------------------------------

# camelCase sheet fields recognized by the pipeline. Anything else assigned to
# Game.* (that isn't a function) gets a typo warning but is preserved —
# handlers may stash their own state on Game.
KNOWN_SHEET_FIELDS = {
    # informational
    'appid', 'name', 'maxPlayers', 'launchMessage', 'minimumJanusVersion',
    # non-Steam games (nonsteam.py)
    'id', 'launcherStartsGame',
    # SessionConfig equivalents
    'isolation', 'borderless', 'nvidia', 'verbose',
    'copyPrefix', 'copyUserdata', 'forceGrabCursor', 'grab', 'hidePanel',
    'extraGamescopeArgs', 'gameArgs', 'extraEnv',
    'goldberg', 'goldbergNoOverlay', 'goldbergDisableLan',
    'goldbergDebug', 'goldbergAutoJoin',
    'goldbergHostInstance',
    'steamInputFilter', 'requireSteamInput', 'requireGamepadLayout',
    'janusInput', 'steamInput',
    'kwinKeepAbove', 'kwinAcceptFocus', 'kwinDisableFsp',
    'oskEnabled', 'exeName', 'launchCommand',
    # script-only
    'useRuntime', 'runtime', 'staggerSeconds',
    'mirrorCopyInstead', 'mirrorExclude',
    'goldbergLanguage',
    'goldbergInvites', 'goldbergRealTicket', 'steamStubExes',
    'lobbyConnectArg', 'killOnClose', 'profileSaves',
    'unityUiScale', 'unityUiScaleGroups', 'unityUiScaleDisable', 'unityUiScaleIgnore',
    'bepinexPlugins',
    'unrealUiScale', 'unrealUiFit', 'sideBySideFirst',
    'reflow', 'lateJoin', 'resizeGame', 'keyboardMouse', 'unityRunInBackground',
}

HOOK_NAMES = {
    'onGameResolved', 'buildEnvironment', 'onMirrorReady', 'buildCommand',
    'play', 'onInstanceStarted', 'onAllStarted', 'onLobbyDetected',
    'onInstanceExit', 'onStop',
    # dynamic players: a game's tile changed size (resize.py)
    'onResize',
    # non-Steam games (nonsteam.py)
    'setupCheck', 'owner', 'art',
    # profile menus (save_info.py)
    'profileInfo',
}

# --- JS prelude -------------------------------------------------------------

_PRELUDE = r"""
"use strict";
globalThis.Game = {};

// Single JSON-boundary dispatcher into the Python host. Host returns
// {ok:true, value:...} or {ok:false, error:"..."} as a JSON string.
function __h(name, args) {
    const raw = __janus_host(name, JSON.stringify(args === undefined ? [] : args));
    const r = JSON.parse(raw);
    if (!r.ok) throw new Error("Janus." + name + ": " + r.error);
    return r.value;
}

function __fmt(a) {
    return a.map(x => typeof x === 'string' ? x : JSON.stringify(x)).join(' ');
}

globalThis.Janus = {
    log: (...a) => __h('log', [__fmt(a)]),
    // User-facing problems: logged AND shown to the user (GUI dialog).
    // warn() carries on; error() also throws, aborting the current hook
    // (and so the launch, unless the hook is a post-spawn notification).
    warn: (...a) => __h('warn', [__fmt(a)]),
    error: (...a) => { const m = __fmt(a); __h('error', [m]); throw new Error(m); },
    addOption: (key, spec) => __h('addOption', [key, spec || {}]),
    getOption: (key) => __h('getOption', [key]),

    readFile: (p) => __h('readFile', [p]),
    writeFile: (p, text) => __h('writeFile', [p, String(text)]),
    readLines: (p) => __h('readLines', [p]),
    writeLines: (p, lines) => __h('writeLines', [p, lines]),
    findLine: (p, pattern) => __h('findLine', [p, pattern]),
    replaceLine: (p, pattern, newLine) => __h('replaceLine', [p, pattern, String(newLine)]),

    iniGet: (p, section, key) => __h('iniGet', [p, section, key]),
    iniSet: (p, section, key, value) => __h('iniSet', [p, section, key, String(value)]),

    readJson: (p) => __h('readJson', [p]),
    writeJson: (p, obj) => __h('writeJson', [p, obj]),
    jsonSet: (p, dottedKey, value) => __h('jsonSet', [p, dottedKey, value]),

    exists: (p) => __h('exists', [p]),
    appInstallDir: (appid) => __h('appInstallDir', [appid]),
    glob: (pattern) => __h('glob', [pattern]),
    mkdir: (p) => __h('mkdir', [p]),
    copy: (src, dst) => __h('copy', [src, dst]),
    move: (src, dst) => __h('move', [src, dst]),
    remove: (p, opts) => __h('remove', [p, opts || {}]),
    symlink: (target, link) => __h('symlink', [target, link]),
    deployPayload: (dst) => __h('deployPayload', [dst]),
    binaryPatch: (p, findHex, replaceHex) => __h('binaryPatch', [p, findHex, replaceHex]),
    backup: (p) => __h('backup', [p]),

    run: (argv, opts) => __h('run', [argv, opts || {}]),
    spawn: (argv, opts) => __h('spawn', [argv, opts || {}]),

    // A button in the game's options (and a setup step's fix): spec =
    // {label, description?, menu?: true, run: function (ctx) {...}}. The
    // function stays here; the host gets the rest and calls __janus_action.
    addAction: (key, spec) => {
        if (!spec || typeof spec.run !== 'function')
            throw new Error("Janus.addAction: spec.run must be a function");
        const s = Object.assign({}, spec);
        delete s.run;
        __h('addAction', [String(key), s]);
        __janus_actions[String(key)] = spec.run;
    },
    progress: (text) => __h('progress', [String(text)]),
    fetchJson: (url, opts) => __h('fetchJson', [String(url), opts || {}]),
    download: (url, dst, opts) => __h('download', [String(url), dst, opts || {}]),
    extract: (archive, dst) => __h('extract', [archive, dst]),
};

const __janus_actions = {};

function __janus_action(key, ctxJson) {
    const fn = __janus_actions[key];
    if (typeof fn !== 'function') throw new Error("no action '" + key + "'");
    const ret = fn(JSON.parse(ctxJson));
    return JSON.stringify({ret: ret === undefined ? null : ret});
}

globalThis.console = { log: Janus.log, error: Janus.warn, warn: Janus.warn };

function __janus_sheet() {
    const out = {};
    for (const k of Object.keys(Game)) {
        if (typeof Game[k] !== 'function') out[k] = Game[k];
    }
    return JSON.stringify(out);
}

function __janus_hooks() {
    return JSON.stringify(Object.keys(Game).filter(k => typeof Game[k] === 'function'));
}

function __janus_invoke(name, ctxJson, envJson) {
    const fn = Game[name];
    if (typeof fn !== 'function') return null;
    const ctx = ctxJson === null ? null : JSON.parse(ctxJson);
    const env = envJson === null ? null : JSON.parse(envJson);
    const ret = (env !== null) ? fn(ctx, env) : fn(ctx);
    return JSON.stringify({
        ret: ret === undefined ? null : ret,
        gameArgs: (ctx && Array.isArray(ctx.gameArgs)) ? ctx.gameArgs : null,
        ctxEnv: (ctx && ctx.env && typeof ctx.env === 'object') ? ctx.env : null,
        env: env,
    });
}
"""


def _hexbytes(s: str) -> bytes:
    return bytes.fromhex(re.sub(r'[\s,]', '', s))


class HandlerScript:
    """A loaded per-game handler script.

    Lifecycle: construct (evaluates the script, captures the property sheet
    and declared options), then the launch pipeline calls :meth:`invoke` at
    fixed hook points, then :meth:`finish` at session end (restores backups,
    kills spawned helpers).
    """

    def __init__(self, script_path: Path, appid: int,
                 option_values: Optional[Dict[str, str]] = None,
                 verbose: bool = False):
        if quickjs is None:
            raise HandlerError(
                "quickjs module not available (expected vendored copy in "
                f"{_VENDOR_QJS} or pip package 'quickjs-ng')")
        self.script_path = Path(script_path)
        self.appid = appid
        self.verbose = verbose
        self._lock = threading.RLock()
        self._loading = True
        self._read_only_hook = ''    # hook name while a read-only hook runs
        self._base_dir: Optional[Path] = None      # resolves relative paths
        self._install_dir: Optional[Path] = None
        self._option_specs: Dict[str, Dict[str, Any]] = {}
        self._option_order: List[str] = []
        self._action_specs: Dict[str, Dict[str, Any]] = {}   # Janus.addAction
        # Janus.progress(text) -> progress(text): the GUIs show it while a
        # setup step or action runs.
        self.progress = None
        self._raw_option_values = dict(option_values or {})
        self._backups: List[tuple] = []            # (path, saved_bytes|None)
        self._spawned: List[subprocess.Popen] = []
        self._finished = False
        self._atexit_registered = False
        # User-facing Janus.warn/error messages: (level, text), deduplicated.
        # notify(level, text, done) is called for each new one (the GUI sets
        # it to a dialog; the CLI just has the log line). Off the UI thread
        # the calling hook BLOCKS until done() is called (the user closed the
        # dialog) or release_waiters() (Stop). Messages raised before notify
        # is set (e.g. at load time) are replayed by the setter.
        self.messages: List[tuple] = []
        self._notify = None
        self._ui_thread: Optional[int] = None
        self._waiters: set = set()                 # threading.Event per blocked hook
        self._cancelled = False

        self._ctx = quickjs.Context()
        mem_mb = int(os.environ.get('JANUS_SCRIPT_MEMORY_MB', '256'))
        self._ctx.set_memory_limit(mem_mb * 1024 * 1024)
        # NOTE: no set_time_limit() — the quickjs binding refuses to call
        # Python callables while a time limit is armed ("Can not call into
        # Python with a time limit set"), and host callables are the whole
        # point. A handler stuck in an infinite loop therefore hangs the
        # launch; there is no in-engine CPU watchdog. Documented in
        # SCRIPTING.md.
        self._ctx.add_callable('__janus_host', self._host)
        self._ctx.eval(_PRELUDE)

        try:
            source = self.script_path.read_text()
        except OSError as e:
            raise HandlerError(f"cannot read handler {self.script_path}: {e}") \
                from None
        # Game.minimumJanusVersion: refuse before running anything newer
        # than this Janus understands.
        need = version.declared_minimum(source)
        if not version.satisfies(need):
            raise HandlerError(
                f"{self.script_path.name} needs Janus {need} or newer (this is "
                f"{version.janus_version()}). Update Janus, or get an older version "
                "of the handler from the handler hub.")
        try:
            self._ctx.eval(source)
        except quickjs.JSException as e:
            raise HandlerError(f"{self.script_path}: {e}") from None
        finally:
            self._loading = False

        self.sheet: Dict[str, Any] = json.loads(self._ctx.eval('__janus_sheet()'))
        self.hooks = set(json.loads(self._ctx.eval('__janus_hooks()')))
        for key in self.sheet:
            if key not in KNOWN_SHEET_FIELDS:
                self._log(f"warning: Game.{key} is not a recognized option "
                          f"(typo?) — ignored by the pipeline")
        for key in self.hooks:
            if key not in HOOK_NAMES:
                self._log(f"warning: Game.{key} is not a recognized hook "
                          f"(known: {', '.join(sorted(HOOK_NAMES))})")
        # Game.unityUiScale / unrealUiScale: Janus's own "UI scale" option
        # (bepinex.py / unreal.py).
        if self.sheet.get('unityUiScale') or self.sheet.get('unrealUiScale'):
            import bepinex
            import unreal
            key = bepinex.UI_SCALE_OPTION
            if key in self._option_specs:
                self._log(f"warning: Janus.addOption('{key}') is replaced by "
                          f"Janus's own UI scale option")
            else:
                self._option_order.append(key)
            self._option_specs[key] = (bepinex.ui_scale_option_spec()
                                       if self.sheet.get('unityUiScale')
                                       else unreal.option_spec(
                                           bool(self.sheet.get('unrealUiFit'))))
            if self.sheet.get('unrealUiScale') and unreal.BY_HEIGHT_OPTION not in self._option_specs:
                self._option_order.insert(self._option_order.index(key), unreal.BY_HEIGHT_OPTION)
                self._option_specs[unreal.BY_HEIGHT_OPTION] = unreal.by_height_option_spec()
        self.options = self._resolve_options()

    # -- logging ------------------------------------------------------------

    def _log(self, msg: str):
        print(f"[script] {msg}", flush=True)

    def _vlog(self, msg: str):
        if self.verbose:
            self._log(msg)

    def set_notify(self, callback, ui_thread: Optional[int] = None):
        """Route Janus.warn/error to *callback(level, text, done)*.

        *ui_thread* (a threading ident) is the thread that displays the
        messages: there, callback must itself block until the user is done
        (modal dialog) — it can't wait for its own event loop. Anywhere else
        the hook waits for done(). Replays the messages already raised."""
        self._notify = callback
        self._ui_thread = ui_thread
        if callback:
            for level, text in list(self.messages):
                self._display(level, text)

    def release_waiters(self):
        """Unblock every hook waiting on a message (session stopping). The
        resumed hooks abort: Janus.warn/error throws, so a stopped session
        doesn't carry on launching behind the user's back."""
        self._cancelled = True
        for ev in list(self._waiters):
            ev.set()

    def _user_message(self, level: str, text: str):
        self._log(f"{level.upper()}: {text}")
        if (level, text) in self.messages:
            return  # e.g. the same warning from every instance's hook
        self.messages.append((level, text))
        self._display(level, text)

    def _display(self, level: str, text: str):
        if not self._notify:
            return
        if self._ui_thread is None or threading.get_ident() == self._ui_thread:
            try:
                self._notify(level, text, lambda: None)
            except Exception as e:
                self._log(f"failed to display {level}: {e}")
            return
        if self._cancelled:
            raise HandlerError("session stopped")
        ev = threading.Event()
        self._waiters.add(ev)
        try:
            self._notify(level, text, ev.set)
            ev.wait()
        except HandlerError:
            raise
        except Exception as e:
            self._log(f"failed to display {level}: {e}")
        finally:
            self._waiters.discard(ev)
        if self._cancelled:
            raise HandlerError("session stopped while the message was shown")

    # -- sheet access --------------------------------------------------------

    def get(self, field: str, default: Any = None) -> Any:
        """Sheet value if the script set it, else *default*."""
        return self.sheet.get(field, default)

    def has(self, field: str) -> bool:
        return field in self.sheet

    def apply_env(self, env: Dict[str, str], user_env=None):
        """Apply sheet-implied env vars to a launch env in place.

        Nothing here overrides a var already present in the user's real
        environment (user env / GUI-injected env > script).
        """
        user_env = os.environ if user_env is None else user_env
        extra = self.sheet.get('extraEnv')
        if isinstance(extra, dict):
            for k, v in extra.items():
                if str(k) not in user_env:
                    env[str(k)] = str(v)

    # -- options -------------------------------------------------------------

    def _resolve_options(self) -> Dict[str, Any]:
        resolved: Dict[str, Any] = {}
        for key, spec in self._option_specs.items():
            resolved[key] = spec.get('default')
        for key, raw in self._raw_option_values.items():
            if key not in self._option_specs:
                self._log(f"warning: -o {key}=... does not match any "
                          f"Janus.addOption key (declared: "
                          f"{', '.join(self._option_order) or 'none'})")
                resolved[key] = raw
                continue
            resolved[key] = self._coerce_option(self._option_specs[key], raw)
        return resolved

    @staticmethod
    def _coerce_option(spec: Dict[str, Any], raw: str) -> Any:
        typ = spec.get('type', 'string')
        if typ == 'bool':
            return str(raw).strip().lower() in ('1', 'true', 'yes', 'on')
        if typ == 'int':
            return int(raw)
        if typ == 'choice':
            # The GUI hands values back as strings; a handler may declare
            # numeric choices. Match by string form and return the ORIGINAL
            # choice element so the script sees its declared type.
            choices = spec.get('choices') or []
            if choices:
                for c in choices:
                    if raw == c or str(raw) == str(c):
                        return c
                raise HandlerError(
                    f"option value '{raw}' not in choices {choices}")
        return raw

    # -- path helpers / guards ----------------------------------------------

    def set_base_dir(self, path):
        """Directory that relative script paths resolve against (usually the
        effective game dir: goldberg mirror if enabled, else install dir)."""
        self._base_dir = Path(path)

    def set_install_dir(self, path):
        self._install_dir = Path(path)

    def _resolve(self, p: str) -> Path:
        path = Path(os.path.expanduser(str(p)))
        if not path.is_absolute():
            base = self._base_dir or self._install_dir or Path.cwd()
            path = base / path
        return path

    def _managed_roots(self) -> List[Path]:
        roots = [
            Path.home() / '.local' / 'share' / 'janus',
            Path.home() / '.config' / 'janus',
            Path('/tmp'),
        ]
        for d in (self._base_dir, self._install_dir):
            if d is not None:
                roots.append(d)
        return roots

    def _is_managed(self, path: Path) -> bool:
        # Judge by where the entry LIVES, not where a symlink points:
        # unlinking a symlink inside a managed dir is safe regardless of its
        # target (goldberg mirrors are full of links into the install dir).
        rp = path.parent.resolve() / path.name
        for root in self._managed_roots():
            try:
                rp.relative_to(root.resolve())
                return True
            except ValueError:
                continue
        return False

    # -- host dispatcher -----------------------------------------------------

    # Janus.* calls that change system state. Forbidden at script load time:
    # top level is declarations only (sheet + addOption + read-only probes).
    # This matters because the GUI evaluates handlers merely to *display*
    # their sheet/options — selecting a game must never modify files.
    _MUTATING_APIS = {
        'writeFile', 'writeLines', 'replaceLine', 'iniSet', 'writeJson',
        'jsonSet', 'mkdir', 'copy', 'move', 'remove', 'symlink',
        'deployPayload', 'binaryPatch', 'backup', 'run', 'spawn',
        # network: slow, and selecting a game must not touch the web
        'fetchJson', 'download', 'extract',
    }

    def _host(self, name, args_json):
        try:
            args = json.loads(str(args_json))
            fn = getattr(self, f'_api_{name}', None)
            if fn is None:
                raise HandlerError(f"unknown host function '{name}'")
            if self._read_only_hook and name in self._MUTATING_APIS:
                raise HandlerError(
                    f"Janus.{name} is not allowed in Game.{self._read_only_hook} "
                    f"(read-only: it only describes, the menus call it often)")
            if self._loading and name in self._MUTATING_APIS:
                raise HandlerError(
                    f"Janus.{name} is not allowed at script load time — "
                    f"top level is declarations only; do this inside a hook "
                    f"(e.g. Game.play)")
            value = fn(*args)
            return json.dumps({'ok': True, 'value': value})
        except Exception as e:  # surfaces in JS as a catchable Error
            return json.dumps({'ok': False, 'error': f'{type(e).__name__}: {e}'})

    # Each _api_* method backs one Janus.* function.

    def _api_log(self, msg):
        self._log(str(msg))
        return None

    def _api_warn(self, msg):
        self._user_message('warning', str(msg))
        return None

    def _api_error(self, msg):
        self._user_message('error', str(msg))
        return None

    def _api_addOption(self, key, spec):
        if not self._loading:
            raise HandlerError("Janus.addOption is only allowed at script "
                               "load time (top level), not inside hooks")
        if not isinstance(spec, dict):
            raise HandlerError("addOption spec must be an object")
        spec.setdefault('type', 'choice' if spec.get('choices') else 'string')
        self._option_specs[str(key)] = spec
        self._option_order.append(str(key))
        return None

    def _api_getOption(self, key):
        key = str(key)
        if hasattr(self, 'options'):
            return self.options.get(key)
        # During load, self.options isn't built yet — resolve from the raw
        # -o values so top-level `Janus.getOption(...)` sees user overrides.
        if key in self._raw_option_values:
            raw = self._raw_option_values[key]
            spec = self._option_specs.get(key)
            return self._coerce_option(spec, raw) if spec else raw
        return self._option_specs.get(key, {}).get('default')

    def _prepare_write(self, path: Path) -> Path:
        """Make *path* safe to write: goldberg mirrors are symlink farms into
        the REAL install dir, so writing through a symlink would silently
        modify the original game files. Break symlinks first: a file symlink
        becomes a real copy of its target; a symlinked ancestor directory is
        materialized mirror-style (real dir, children symlinked) so only the
        touched entry becomes real."""
        ancestors = []
        cur = path.parent
        while cur != cur.parent:
            ancestors.append(cur)
            cur = cur.parent
        for d in reversed(ancestors):          # root-first
            if d.is_symlink() and Path(os.path.realpath(d)).is_dir():
                target = Path(os.path.realpath(d))
                d.unlink()
                d.mkdir()
                for child in target.iterdir():
                    (d / child.name).symlink_to(child)
        if path.is_symlink():
            target = Path(os.path.realpath(path))
            path.unlink()
            if target.is_dir():
                path.mkdir()
                for child in target.iterdir():
                    (path / child.name).symlink_to(child)
            elif target.exists():
                shutil.copy2(target, path)
        return path

    def _api_readFile(self, p):
        return self._resolve(p).read_text()

    def _api_writeFile(self, p, text):
        path = self._prepare_write(self._resolve(p))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return None

    def _api_readLines(self, p):
        return self._resolve(p).read_text().splitlines()

    def _api_writeLines(self, p, lines):
        if not isinstance(lines, list):
            raise HandlerError("writeLines expects an array of strings")
        path = self._prepare_write(self._resolve(p))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('\n'.join(str(l) for l in lines) + '\n')
        return None

    def _api_findLine(self, p, pattern):
        rx = re.compile(pattern)
        for i, line in enumerate(self._api_readLines(p), start=1):
            if rx.search(line):
                return i
        return -1

    def _api_replaceLine(self, p, pattern, new_line):
        path = self._resolve(p)
        rx = re.compile(pattern)
        lines = path.read_text().splitlines()
        for i, line in enumerate(lines):
            if rx.search(line):
                lines[i] = new_line
                self._prepare_write(path).write_text('\n'.join(lines) + '\n')
                return i + 1
        return -1

    # Line-preserving INI editing. Section '' or null = keys before any
    # [section] header (common in game cfgs). Handles 'key=value' with
    # optional spaces; creates missing sections/keys on set.
    def _ini_parse_target(self, lines, section, key):
        """(key_line_index, section_insert_point) for *key* in *section*.
        With duplicate section headers, the LAST occurrence of key/section
        wins (matching typical last-wins INI parsers) — editing an earlier
        one would leave a shadowing later value."""
        top_level = section in ('', None)
        in_target = top_level
        found_idx = None
        sec_end = 0 if top_level else None
        key_rx = re.compile(r'^\s*' + re.escape(key) + r'\s*=')
        for i, line in enumerate(lines):
            m = re.match(r'^\s*\[(.+?)\]\s*$', line)
            if m:
                if top_level:
                    break
                in_target = (m.group(1) == section)
                if in_target:
                    sec_end = i + 1
                continue
            if in_target:
                if key_rx.match(line):
                    found_idx = i
                if line.strip():
                    sec_end = i + 1
        return found_idx, sec_end

    def _api_iniGet(self, p, section, key):
        path = self._resolve(p)
        if not path.exists():
            return None
        lines = path.read_text().splitlines()
        idx, _ = self._ini_parse_target(lines, section, key)
        if idx is None:
            return None
        return lines[idx].split('=', 1)[1].strip()

    def _api_iniSet(self, p, section, key, value):
        path = self._resolve(p)
        lines = path.read_text().splitlines() if path.exists() else []
        idx, sec_end = self._ini_parse_target(lines, section, key)
        entry = f'{key}={value}'
        if idx is not None:
            lines[idx] = entry
        elif sec_end is not None:
            lines.insert(sec_end, entry)
        else:
            if section not in ('', None):
                lines.extend(['', f'[{section}]'])
            lines.append(entry)
        path = self._prepare_write(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('\n'.join(lines) + '\n')
        return None

    def _api_readJson(self, p):
        return json.loads(self._resolve(p).read_text())

    def _api_writeJson(self, p, obj):
        path = self._prepare_write(self._resolve(p))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj, indent=2) + '\n')
        return None

    def _api_jsonSet(self, p, dotted_key, value):
        path = self._resolve(p)
        data = json.loads(path.read_text()) if path.exists() else {}
        node = data
        keys = str(dotted_key).split('.')
        for k in keys[:-1]:
            node = node.setdefault(k, {})
        node[keys[-1]] = value
        self._prepare_write(path).write_text(json.dumps(data, indent=2) + '\n')
        return None

    def _api_exists(self, p):
        return self._resolve(p).exists()

    def _api_appInstallDir(self, appid):
        try:
            from steam import find_app_install_dir
        except ImportError:
            from .steam import find_app_install_dir
        d = find_app_install_dir(int(appid))
        return str(d) if d else None

    def _api_glob(self, pattern):
        pat = str(self._resolve(pattern))
        return sorted(_glob(pat, recursive=True))

    def _api_mkdir(self, p):
        self._resolve(p).mkdir(parents=True, exist_ok=True)
        return None

    def _copy_entry(self, s: Path, d: Path):
        """Copy one file over *d*, breaking a pre-existing symlink at *d*
        instead of writing through it."""
        if d.is_symlink():
            d.unlink()
        shutil.copy2(s, d)

    def _copy_tree_safe(self, s: Path, d: Path):
        """copytree that never writes through destination symlinks (file OR
        directory) — a symlinked destination dir is materialized first."""
        if d.is_symlink():
            self._prepare_write(d)
        d.mkdir(parents=True, exist_ok=True)
        for item in s.iterdir():
            di = d / item.name
            if item.is_dir() and not item.is_symlink():
                self._copy_tree_safe(item, di)
            else:
                self._copy_entry(item, di)

    def _api_copy(self, src, dst):
        s, d = self._resolve(src), self._resolve(dst)
        if s.is_dir():
            self._copy_tree_safe(s, d)
        else:
            d.parent.mkdir(parents=True, exist_ok=True)
            self._copy_entry(s, d)
        return None

    def _api_move(self, src, dst):
        s, d = self._resolve(src), self._resolve(dst)
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(s), str(d))
        return None

    def _api_remove(self, p, opts):
        path = self._resolve(p)
        if not self._is_managed(path) and not opts.get('force'):
            raise HandlerError(
                f"refusing to remove {path}: outside Janus-managed dirs "
                f"(pass {{force: true}} if intentional)")
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
        return None

    def _api_symlink(self, target, link):
        lnk = self._resolve(link)
        lnk.parent.mkdir(parents=True, exist_ok=True)
        if lnk.is_symlink() or lnk.exists():
            lnk.unlink()
        lnk.symlink_to(self._resolve(target))
        return None

    def _api_deployPayload(self, dst):
        payload = payload_dir_for(self.script_path)
        if not payload.is_dir():
            raise HandlerError(f"handler has no payload directory ({payload})")
        dest = self._resolve(dst)
        self._copy_tree_safe(payload, dest)
        self._vlog(f"deployed payload {payload} -> {dest}")
        return None

    def _api_binaryPatch(self, p, find_hex, replace_hex):
        path = self._resolve(p)
        find_b, repl_b = _hexbytes(find_hex), _hexbytes(replace_hex)
        if len(find_b) != len(repl_b):
            raise HandlerError("binaryPatch find/replace must be equal length")
        data = path.read_bytes()
        count = data.count(find_b)
        if count:
            self._prepare_write(path).write_bytes(data.replace(find_b, repl_b))
        return count

    def _register_atexit_cleanup(self):
        """Safety net: restore backups / kill spawned helpers even when the
        launcher exits without reaching finish() (e.g. cmd_steam sys.exit(1)
        after play() already edited files). Deliberately does NOT run
        Game.killOnClose — no session ever ran to completion here."""
        if not self._atexit_registered:
            self._atexit_registered = True
            atexit.register(self._cleanup_resources)

    def _cleanup_resources(self):
        if self._finished:
            return
        # Serialize against an in-flight invoke() that may still be adding
        # backups/spawns from a worker thread.
        with self._lock:
            self._cleanup_locked()

    def _cleanup_locked(self):
        for path, saved in reversed(self._backups):
            try:
                if saved is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(saved)
                self._vlog(f"restored {path}")
            except OSError as e:
                self._log(f"failed to restore {path}: {e}")
        self._backups.clear()
        for proc in self._spawned:
            if proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass
        self._spawned.clear()

    def _api_backup(self, p):
        self._register_atexit_cleanup()
        path = self._resolve(p)
        saved = path.read_bytes() if path.exists() else None
        self._backups.append((path, saved))
        self._vlog(f"backed up {path} (restored at session end)")
        return None

    @staticmethod
    def _check_argv(argv):
        if not isinstance(argv, list) or not argv or \
                not all(isinstance(a, str) for a in argv):
            raise HandlerError("expected a non-empty argv array of strings "
                               "(no shell strings)")

    def _run_kwargs(self, opts):
        kw: Dict[str, Any] = {}
        if opts.get('cwd'):
            kw['cwd'] = str(self._resolve(opts['cwd']))
        if opts.get('env'):
            env = os.environ.copy()
            env.update({str(k): str(v) for k, v in opts['env'].items()})
            kw['env'] = env
        return kw

    def _api_run(self, argv, opts):
        self._check_argv(argv)
        kw = self._run_kwargs(opts)
        timeout = float(opts.get('timeout', 20))
        r = subprocess.run(argv, capture_output=True, text=True,
                           timeout=timeout, **kw)
        return {'code': r.returncode, 'stdout': r.stdout, 'stderr': r.stderr}

    def _api_spawn(self, argv, opts):
        self._register_atexit_cleanup()
        self._check_argv(argv)
        kw = self._run_kwargs(opts)
        proc = subprocess.Popen(argv, **kw)
        self._spawned.append(proc)
        return proc.pid

    # -- actions, progress, web ----------------------------------------------

    def _api_addAction(self, key, spec):
        if not self._loading:
            raise HandlerError("Janus.addAction is only allowed at script "
                               "load time (top level), not inside hooks")
        if not isinstance(spec, dict) or not spec.get('label'):
            raise HandlerError("addAction spec needs a label")
        spec.setdefault('menu', True)
        self._action_specs[str(key)] = spec
        return None

    def _api_progress(self, text):
        self._log(str(text))
        if self.progress:
            try:
                self.progress(str(text))
            except Exception as e:
                self._log(f"progress display failed: {e}")
        return None

    _USER_AGENT = 'janus-split-screen (handler script)'

    def _open_url(self, url, timeout, headers=None):
        import urllib.request
        if not str(url).startswith(('https://', 'http://')):
            raise HandlerError(f"not a web URL: {url}")
        req = urllib.request.Request(str(url), headers={'User-Agent': self._USER_AGENT,
                                                        **(headers or {})})
        return urllib.request.urlopen(req, timeout=timeout)

    def _api_fetchJson(self, url, opts):
        with self._open_url(url, float(opts.get('timeout', 30))) as r:
            return json.loads(r.read().decode('utf-8'))

    def _api_download(self, url, dst, opts):
        """Download *url* to *dst* (a file path), via a temp file renamed on
        success. opts: sha1/sha256/sha512 (hex) to verify, label for the
        progress lines, timeout, retries (default 4).

        Robust to flaky connections: Python's http.client returns a body cut
        short by a dropped connection as a normal end of file (no
        IncompleteRead), which used to surface as a hash mismatch. Each
        attempt checks the length against Content-Length, resumes with a
        Range request when the server supports it, and starts over after a
        mismatch. Returns the path."""
        import hashlib
        import http.client
        import time as _time
        import urllib.error
        path = self._resolve(dst)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + '.part')
        want = {a: str(opts[a]).lower() for a in ('sha1', 'sha256', 'sha512') if opts.get(a)}
        label = str(opts.get('label') or path.name)
        timeout = float(opts.get('timeout', 60))
        attempts = max(1, int(opts.get('retries', 4)) + 1)
        step = [-1]

        def progress(done, total):
            # A line every 5 % (every 8 MB without a known size).
            now = done * 20 // total if total else done >> 23
            if now != step[0]:
                step[0] = now
                self._api_progress(f"Downloading {label}: " +
                                   (f"{done * 100 // total}%" if total else f"{done >> 20} MB"))

        def fetch():
            """One attempt, resuming *tmp* if it has data. Returns True when
            the body is complete (as far as the server said how long)."""
            have = tmp.stat().st_size if tmp.exists() else 0
            headers = {'Range': f'bytes={have}-'} if have else None
            try:
                r = self._open_url(url, timeout, headers)
            except urllib.error.HTTPError as e:
                if e.code == 416 and have:           # nothing left to send, or a bad range
                    tmp.unlink()
                    return False
                raise
            with r:
                total = 0
                if have and r.status == 206:
                    m = re.match(r'bytes \d+-\d+/(\d+)', r.headers.get('Content-Range', ''))
                    total = int(m.group(1)) if m else 0
                    mode = 'ab'
                else:                               # no resume: start over
                    have = 0
                    total = int(r.headers.get('Content-Length') or 0)
                    mode = 'wb'
                done = have
                with open(tmp, mode) as out:
                    while True:
                        chunk = r.read(1 << 16)
                        if not chunk:
                            break
                        out.write(chunk)
                        done += len(chunk)
                        progress(done, total)
            if total and done < total:
                self._log(f"{label}: connection dropped at {done}/{total} bytes")
                return False
            return True

        def verify():
            if not want:
                return True
            hs = {a: hashlib.new(a) for a in want}
            with open(tmp, 'rb') as f:
                for chunk in iter(lambda: f.read(1 << 20), b''):
                    for h in hs.values():
                        h.update(chunk)
            bad = [a for a, h in hs.items() if h.hexdigest() != want[a]]
            if bad:
                self._log(f"{label}: {bad[0]} mismatch, downloading it again")
            return not bad

        last = None
        try:
            tmp.unlink(missing_ok=True)
            for attempt in range(attempts):
                if attempt:
                    _time.sleep(min(8, 2 ** (attempt - 1)))
                    self._api_progress(f"Downloading {label}: retrying ({attempt}/{attempts - 1})")
                try:
                    if not fetch():
                        last = "the connection kept dropping"
                        continue
                except urllib.error.HTTPError as e:
                    if e.code < 500 and e.code not in (408, 429):
                        raise HandlerError(f"{label}: download failed (HTTP {e.code})") from None
                    last = f"HTTP {e.code}"
                    continue
                except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
                    last = str(getattr(e, 'reason', None) or e)
                    self._log(f"{label}: {last}")
                    continue
                if verify():
                    tmp.replace(path)
                    return str(path)
                tmp.unlink()
                last = "the file kept arriving corrupted (checksum mismatch)"
            raise HandlerError(f"{label}: download failed after {attempts} attempts: {last}. "
                               "Check the internet connection and try again.")
        finally:
            tmp.unlink(missing_ok=True)

    def _api_extract(self, archive, dst):
        """Unpack a .tar.gz/.tar.xz/.tgz or .zip into *dst*. Refuses entries
        (and symlinks) that would land outside *dst*."""
        import tarfile
        import zipfile
        src, dest = self._resolve(archive), self._resolve(dst)
        dest.mkdir(parents=True, exist_ok=True)
        root = dest.resolve()

        def inside(p: Path) -> bool:
            try:
                p.resolve().relative_to(root)
                return True
            except ValueError:
                return False

        if zipfile.is_zipfile(src):
            with zipfile.ZipFile(src) as z:
                for name in z.namelist():
                    if name.startswith('/') or not inside(dest / name):
                        raise HandlerError(f"unsafe path in {src.name}: {name}")
                z.extractall(dest)
        else:
            with tarfile.open(src) as t:
                for m in t.getmembers():
                    target = dest / m.name
                    if m.name.startswith('/') or not inside(target):
                        raise HandlerError(f"unsafe path in {src.name}: {m.name}")
                    if m.issym() and not inside(target.parent / m.linkname):
                        raise HandlerError(f"unsafe link in {src.name}: {m.name}")
                    if m.islnk() and not inside(dest / m.linkname):
                        raise HandlerError(f"unsafe link in {src.name}: {m.name}")
                    if m.isdev():
                        raise HandlerError(f"device file in {src.name}: {m.name}")
                # Python >= 3.12 (and security backports) also check it themselves.
                t.extractall(dest, **({'filter': 'data'}
                                      if hasattr(tarfile, 'data_filter') else {}))
        self._vlog(f"extracted {src} -> {dest}")
        return None

    # -- hook invocation ------------------------------------------------------

    @property
    def option_specs(self):
        """Declared Janus.addOption specs, in declaration order:
        [(key, spec_dict), ...] — for GUI rendering."""
        return [(k, self._option_specs[k]) for k in self._option_order]

    @property
    def action_specs(self):
        """Janus.addAction specs, in declaration order: [(key, spec)] —
        spec = {label, description?, menu}."""
        return list(self._action_specs.items())

    @property
    def non_steam(self) -> bool:
        """A non-Steam game's handler (`Game.id`, no `Game.appid`)."""
        return bool(self.sheet.get('id')) and 'appid' not in self.sheet

    def run_action(self, key: str, ctx: Dict[str, Any]):
        """Run the Janus.addAction function *key* with *ctx*; returns what
        it returned. Raises HandlerError when it throws (Janus.error)."""
        if key not in self._action_specs:
            raise HandlerError(f"no action '{key}'")
        with self._lock:
            if ctx.get('gameDir'):
                self._base_dir = Path(ctx['gameDir'])
            self._vlog(f"action {key} running")
            try:
                raw = self._ctx.get('__janus_action')(key, json.dumps(ctx))
            except (quickjs.JSException, quickjs.StackOverflow) as e:
                # Shown to the user as is: no JS stack (it goes to the log).
                # Janus.error / a thrown Error = the handler's own words; any
                # other exception (a TypeError: a handler bug) keeps its type.
                text = str(e)
                self._log(f"action {key} failed: {text}")
                text = text.split('\n    at ')[0].strip()
                if text.startswith('Error: '):
                    raise HandlerError(text[len('Error: '):]) from None
                raise HandlerError(f"{self._action_specs[key]['label']}: {text}") from None
        return json.loads(str(raw)).get('ret')

    def has_hook(self, name: str) -> bool:
        return name in self.hooks

    def invoke(self, name: str, ctx: Optional[Dict[str, Any]] = None,
               env: Optional[Dict[str, str]] = None, soft: bool = False):
        """Invoke Game.<name>(ctx[, env]) if defined.

        Returns None when the hook isn't defined, else a dict:
          {'ret': ..., 'gameArgs': [...]|None, 'ctxEnv': {...}|None,
           'env': {...}|None (post-mutation copy of *env*)}

        soft=True: log exceptions and return None instead of raising —
        used for post-spawn notification hooks that must not kill a
        running session.
        """
        if not self.has_hook(name):
            return None
        if self._finished:
            # A worker still mid-launch after stop_all()/finish() must not
            # run hooks: backups/spawns registered now would never be cleaned
            # up (finish is idempotent and already ran).
            self._log(f"hook {name} skipped: session already finished")
            return None
        if not self._acquire_for_hook(name):
            return None
        try:
            try:
                # Re-anchor relative-path resolution from the ctx itself so
                # concurrent per-instance callers (GUI InstanceWorker threads
                # with different goldberg mirrors) can share one script: the
                # base dir is taken under the lock, per invocation.
                if ctx:
                    if ctx.get('gameDir'):
                        self._base_dir = Path(ctx['gameDir'])
                    if ctx.get('installDir'):
                        self._install_dir = Path(ctx['installDir'])
                self._vlog(f"hook {name} firing")
                ctx_json = json.dumps(ctx) if ctx is not None else None
                env_json = json.dumps(env) if env is not None else None
                fn = self._ctx.get('__janus_invoke')
                raw = fn(name, ctx_json, env_json)
                return json.loads(str(raw)) if raw is not None else None
            except (quickjs.JSException, quickjs.StackOverflow) as e:
                if soft:
                    self._log(f"error in Game.{name} (ignored): {e}")
                    return None
                raise HandlerError(f"Game.{name}: {e}") from None
        finally:
            self._lock.release()

    def _acquire_for_hook(self, name: str) -> bool:
        """Take the script lock. On the UI thread, give up (skip the hook)
        while another thread's hook holds it waiting for the user to close a
        message: that dialog needs the UI thread, so blocking here would
        deadlock."""
        if threading.get_ident() != self._ui_thread:
            self._lock.acquire()
            return True
        while not self._lock.acquire(timeout=0.05):
            if self._waiters:
                self._log(f"hook {name} skipped: a handler message is "
                          f"waiting to be closed")
                return False
        return True

    # -- session teardown -----------------------------------------------------

    def finish(self):
        """Session-end cleanup: restore Janus.backup files, kill spawned
        helpers, kill Game.killOnClose process names. Idempotent."""
        if self._finished:
            return
        self._cleanup_resources()
        self._finished = True
        for name in self.get('killOnClose', []) or []:
            subprocess.run(['pkill', '-f', str(name)],
                           capture_output=True)


# --- loading + CLI merge helpers -------------------------------------------

def parse_option_args(pairs: Optional[List[str]]) -> Dict[str, str]:
    """Parse repeated CLI ``-o key=value`` args into a dict."""
    out: Dict[str, str] = {}
    for pair in pairs or []:
        if '=' not in pair:
            raise ValueError(f"invalid option '{pair}', expected key=value")
        k, v = pair.split('=', 1)
        out[k.strip()] = v
    return out


def no_handler_message(appid) -> str:
    what = f"app {appid}" if is_steam_key(appid) else f'"{appid}"'
    return (f"No handler for {what}: Janus only launches games that have "
            f"one. Get it from the handler hub (Big Picture: Y, or "
            f"`main.py hub get {appid}`).")


def load_handler(appid: int, option_values: Optional[Dict[str, str]] = None,
                 verbose: bool = False,
                 explicit_path: Optional[Path] = None) -> HandlerScript:
    """Load the handler script for *appid*. Raises HandlerError when there
    is none (no launch without a handler), when quickjs is missing, or when
    the script is broken — a launch must fail loudly, never degrade."""
    path = Path(explicit_path) if explicit_path else find_handler(appid)
    if path is None:
        raise HandlerError(no_handler_message(appid))
    if quickjs is None:
        raise HandlerError(f"handler {path} exists but the quickjs module is "
                           f"missing (vendor/quickjs/ or pip quickjs-ng)")
    if not explicit_path:
        conflict = handler_conflict(appid)
        if conflict:
            print(f"[script] Warning: {conflict}", flush=True)
    script = HandlerScript(path, appid, option_values=option_values,
                           verbose=verbose)
    print(f"[script] Loaded handler {path}"
          + (f" ({script.get('name')})" if script.get('name') else ''),
          flush=True)
    field = 'appid' if is_steam_key(appid) else 'id'
    declared = script.get(field)
    if declared is not None and str(declared) != str(appid):
        print(f"[script] Warning: {path.name} declares Game.{field} = {declared} "
              f"but is running for {appid}", flush=True)
    return script


def launch_message(script: Optional[HandlerScript]) -> str:
    """Game.launchMessage: what the players must know before the game
    starts (shown by both GUIs, which wait for an OK; printed by the CLI).
    '' when the handler sets none."""
    if script is None:
        return ''
    msg = script.get('launchMessage')
    return str(msg).strip() if msg else ''


def effective(script: Optional[HandlerScript], field: str,
              cli_value, cli_default, builtin):
    """3-way precedence merge for one launch option.

    CLI beats script beats built-in. 'CLI set it' is detected by comparing
    with *cli_default* — flags that pass their argparse default fall through
    to the script (limitation: an explicit flag equal to its default can't
    override the script; use None-sentinel defaults where that matters).
    """
    if cli_value != cli_default:
        return cli_value
    if script is not None and script.has(field):
        return script.get(field)
    return builtin

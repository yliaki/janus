"""QML-facing backend for Big Picture mode.

Game list, the players who joined (by pressing A on their pad), the split
layout, and launch/stop — on top of the same LauncherService, PresetManager
and handler scripts as the desktop GUI. Advanced settings stay in the
desktop GUI: a launch here uses the game's saved config (or the handler's
defaults) and only replaces its instances. That config is never written
back, so a Big Picture session can't clobber the desktop layout.
"""

import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import (
    QAbstractListModel, QModelIndex, QObject, QTimer, QUrl, Qt,
    Property, Signal, Slot,
)

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / 'src' / 'host'))

from src.gui.models.config import (  # noqa: E402
    InstanceConfig, SessionConfig, apply_script_defaults,
)
from src.gui.models.presets import PresetManager  # noqa: E402
from src.gui.services.launcher_service import LauncherService, session_pads  # noqa: E402
from src.gui.services.screen_info import get_all_monitors  # noqa: E402
from src.bigpicture.hub_browser import HubBrowser  # noqa: E402
from controllers import list_controllers, sysfs_device  # noqa: E402
from padd import NINTENDO_VID  # noqa: E402
from steam import get_installed_games, find_all_steam_roots, SteamGame  # noqa: E402
import about  # noqa: E402
import hub  # noqa: E402
import nonsteam  # noqa: E402
import prereqs  # noqa: E402
import profiles  # noqa: E402
import save_info  # noqa: E402
import scripting  # noqa: E402
import steam_input  # noqa: E402
import controller_input as cinput  # noqa: E402
import pad_mappings  # noqa: E402
import wayland_session as ws  # noqa: E402
from split_session import UPSCALE_FILTERS  # noqa: E402
from layouts import (  # noqa: E402,F401  (re-exported: tests, QML-side names)
    LAYOUTS, MAX_PLAYERS, SIDE_BY_SIDE, layout_options, tile_rects,
)


# Options panel "Display" rows (SessionConfig fields, shared with the desktop GUI).
DISPLAY_FIELDS = ('render_scale', 'upscale_filter', 'upscale_sharpness', 'fps_limit')
RENDER_SCALES = list(range(25, 101, 5))
import perf  # noqa: E402
UPSCALE_LABELS = {'linear': 'Smooth', 'fsr': 'FSR', 'nis': 'NIS',
                  'nearest': 'Nearest', 'pixel': 'Pixel'}
assert set(UPSCALE_LABELS) == set(UPSCALE_FILTERS)

# A player who set their own number this recently is "pinned": when someone
# else changes number, the pinned keep theirs and the others shift instead.
PIN_SECONDS = 10.0

# A joined pad that disappears keeps its player this long, waiting to be
# re-attached by hardware identity (wireless pads drop out and come back,
# often at a different js index).
RECONNECT_SECONDS = 15.0

def _library_file(roots, appid: int, *names: str) -> str:
    """File URL of the first of *names* in Steam's library-art cache for
    *appid* (appcache/librarycache/<appid>/, flat or in a hashed
    subdirectory, depending on the client version), or ''."""
    for root in roots:
        d = root / 'appcache' / 'librarycache' / str(appid)
        if not d.is_dir():
            continue
        for name in names:
            hit = next(d.glob(name), None) or next(d.glob('*/' + name), None)
            if hit:
                return QUrl.fromLocalFile(str(hit)).toString()
    return ''


def _cover_art(roots, appid: int) -> tuple[str, bool]:
    """(file URL, is_portrait) of the game's Steam library artwork, or ('', False).

    Older clients name the portrait art library_600x900.jpg and the banner
    header.jpg; newer ones library_capsule.jpg (300x450) and
    library_header.jpg."""
    url = _library_file(roots, appid, 'library_600x900*.jpg', 'library_capsule.jpg')
    if url:
        return url, True
    return _library_file(roots, appid, 'header.jpg', 'library_header.jpg'), False


# Friendly names for common pads (by VID:PID); others get cleaned up.
_PAD_NAMES = {
    (0x045e, 0x028e): 'Xbox 360', (0x045e, 0x0719): 'Xbox 360 Wireless',
    (0x045e, 0x02d1): 'Xbox One', (0x045e, 0x02dd): 'Xbox One',
    (0x045e, 0x02ea): 'Xbox One S', (0x045e, 0x02e0): 'Xbox One S',
    (0x045e, 0x0b00): 'Xbox Elite 2', (0x045e, 0x0b05): 'Xbox Elite 2',
    (0x045e, 0x0b12): 'Xbox Series', (0x045e, 0x0b13): 'Xbox Series',
    (0x045e, 0x0b20): 'Xbox Series', (0x045e, 0x0b22): 'Xbox Series',
    (0x054c, 0x05c4): 'DualShock 4', (0x054c, 0x09cc): 'DualShock 4',
    (0x054c, 0x0ce6): 'DualSense', (0x054c, 0x0df2): 'DualSense Edge',
    (0x057e, 0x2009): 'Switch Pro', (0x28de, 0x1102): 'Steam Controller',
    (0x28de, 0x1142): 'Steam Controller', (0x28de, 0x1205): 'Steam Deck',
}
_NAME_NOISE = re.compile(
    r'\b(Gamepad|Controller|Microsoft|Sony Interactive Entertainment|Nintendo|'
    r'Generic|Corporation|Inc\.?|Co\.,?\s*Ltd\.?)\b', re.I)


def short_pad_name(name: str, vid: int, pid: int) -> str:
    """'Microsoft Xbox Series S|X Controller' -> 'Xbox Series',
    'Gamepad MACHENIKE G3V2' -> 'MACHENIKE G3V2'. Display only: the full
    kernel name stays the controller's identity."""
    if (vid, pid) in _PAD_NAMES:
        return _PAD_NAMES[(vid, pid)]
    short = re.sub(r'\s+', ' ', _NAME_NOISE.sub('', name)).strip(' -_,')
    # "USB Gamepad" -> "USB" says less than the original.
    if len(short) < 4 or short.lower() in ('usb', 'wireless', 'pad', 'joystick'):
        return name
    return short


def _battery_supply(js_index: int):
    """The power_supply dir of a wireless pad, or None. The driver (xpad,
    hid-playstation, hid-nintendo, xpadneo, ...) hangs it off the HID/USB
    device above the input device."""
    try:
        node = Path(sysfs_device(js_index)).resolve()
    except OSError:
        return None
    for anc in list(node.parents)[:4]:
        ps_dir = anc / 'power_supply'
        if ps_dir.is_dir():
            for ps in ps_dir.iterdir():
                return ps
    return None


def _read_battery(ps: Path) -> int:
    levels = {'Critical': 5, 'Low': 20, 'Normal': 60, 'High': 85, 'Full': 100}
    try:
        return int((ps / 'capacity').read_text().strip())
    except (OSError, ValueError):
        pass
    try:
        return levels.get((ps / 'capacity_level').read_text().strip(), -1)
    except OSError:
        return -1


# Reading a battery can block for seconds: the generic HID battery of a
# Bluetooth pad (hid-<mac>-battery, e.g. an Xbox Wireless pad over BT) asks
# the device for a report through uhid on every read of `capacity`, and
# waits ~5 s when it doesn't answer ("No data available"). Done on the UI
# thread for each pad on every refresh, Big Picture froze (2026-10-27).
# So batteries are read by one background thread; the UI only sees the cache.
_BATTERY_TTL = 60.0          # seconds between reads of a pad that answered
_BATTERY_RETRY = 300.0       # ... and of one that didn't
_battery_cache = {}          # power_supply path -> (value, time read)
_battery_pending = set()
_battery_lock = threading.Lock()
_battery_wake = threading.Event()


def _battery_worker():
    while True:
        _battery_wake.wait()
        _battery_wake.clear()
        while True:
            with _battery_lock:
                if not _battery_pending:
                    break
                ps = _battery_pending.pop()
            value = _read_battery(ps)
            with _battery_lock:
                _battery_cache[ps] = (value, time.monotonic())


_battery_thread = None


def pad_battery(js_index: int) -> int:
    """Battery percent of a wireless pad, -1 when it reports none (or hasn't
    been read yet). Never blocks: reads happen in a background thread."""
    global _battery_thread
    ps = _battery_supply(js_index)
    if ps is None:
        return -1
    now = time.monotonic()
    with _battery_lock:
        value, when = _battery_cache.get(ps, (-1, None))
        stale = when is None or now - when > (_BATTERY_TTL if value >= 0 else _BATTERY_RETRY)
        if stale and ps not in _battery_pending:
            _battery_pending.add(ps)
            if when is None:   # not yet tried: don't ask again until this read is done
                _battery_cache[ps] = (-1, now)
            if _battery_thread is None:
                _battery_thread = threading.Thread(
                    target=_battery_worker, name='janus-battery', daemon=True)
                _battery_thread.start()
            _battery_wake.set()
    return value


def pad_key(pad) -> str:
    """A pad's hardware identity for remembering its player's profile:
    uniq (serial/MAC), else VID:PID + USB port, else VID:PID + name."""
    ident = pad.get('uniq') or pad.get('phys') or pad.get('name') or ''
    return f"{pad['vid']:04x}:{pad['pid']:04x}:{ident}"


def _non_steam_art(gid: str, handler: Path, kind: str) -> str:
    """File URL of a non-Steam game's *kind* art (cover/hero/hero_blur/logo),
    which Steam can't provide: downloaded by Game.art (nonsteam.fetch_art),
    else <kind>.jpg/.png in the handler's payload dir, else ''."""
    hit = nonsteam.art_file(gid, kind)
    if hit is None:
        d = scripting.payload_dir_for(handler)
        hit = next((d / f'{kind}.{ext}' for ext in ('jpg', 'png')
                    if (d / f'{kind}.{ext}').is_file()), None)
    return QUrl.fromLocalFile(str(hit)).toString() if hit else ''


class GameListModel(QAbstractListModel):
    """Games Janus can play: installed ones with a handler first, then
    installed ones whose handler is on the hub ("Get handler"), then games
    with a handler that aren't installed (shown so the player knows to
    install them). A game with no handler here or on the hub (usually a
    single-player game) isn't listed at all."""

    AppidRole = Qt.UserRole + 1
    NameRole = Qt.UserRole + 2
    CoverRole = Qt.UserRole + 3
    PortraitRole = Qt.UserRole + 4
    KnownRole = Qt.UserRole + 5
    HeroRole = Qt.UserRole + 6
    InstalledRole = Qt.UserRole + 7
    HandlerRole = Qt.UserRole + 8

    _artFetched = Signal()        # a non-Steam game's art arrived (worker thread)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._all: list[dict] = []
        self._rows: list[dict] = []
        self._hub_keys: set = set()     # games with a handler on the hub (set_hub_keys)
        self._art_tried: set = set()    # non-Steam ids whose art was fetched this run
        self._artFetched.connect(self.refresh)

    def _fetch_art(self, gid, path):
        """Download a non-Steam game's Game.art in the background, once per
        run (and only when missing or a month old)."""
        if gid in self._art_tried or not nonsteam.art_wanted(gid):
            return
        self._art_tried.add(gid)

        def work():
            try:
                if nonsteam.fetch_art(scripting.HandlerScript(path, gid)):
                    self._artFetched.emit()
            except scripting.HandlerError as e:
                print(f'[bigpicture] {gid} art: {e}', flush=True)
        threading.Thread(target=work, name='janus-art', daemon=True).start()

    def roleNames(self):
        return {self.AppidRole: b'appid', self.NameRole: b'name',
                self.CoverRole: b'cover', self.PortraitRole: b'portrait',
                self.KnownRole: b'known', self.HeroRole: b'hero',
                self.InstalledRole: b'installed', self.HandlerRole: b'handler'}

    def rowCount(self, parent=QModelIndex()):
        return len(self._rows)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        g = self._rows[index.row()]
        return {self.AppidRole: g['appid'], self.NameRole: g['name'],
                self.CoverRole: g['cover'], self.PortraitRole: g['portrait'],
                self.KnownRole: g['known'], self.HeroRole: g['hero'],
                self.InstalledRole: g['installed'], self.HandlerRole: g['handler'],
                Qt.DisplayRole: g['name']}.get(role)

    @Slot('QVariant', result=int)
    def indexOf(self, appid):
        return next((i for i, g in enumerate(self._rows) if g['appid'] == appid), -1)

    def refresh(self):
        try:
            installed = get_installed_games()
        except FileNotFoundError:
            installed = []
        handled: dict[int, Path] = {}
        for d in scripting._handler_dirs():
            for p in scripting._handler_files(d):
                a = scripting.handler_appid(p)
                if a is not None:
                    handled.setdefault(a, p)
        roots = find_all_steam_roots()
        games = []
        for g in installed:
            # Proton/runtime/redistributable entries aren't games.
            if any(s in g['name'] for s in ('Proton', 'Steam Linux Runtime',
                                            'Steamworks Common')):
                continue
            # No handler here or on the hub: not a game Janus can play.
            if g['appid'] not in handled and g['appid'] not in self._hub_keys:
                continue
            cover, portrait = _cover_art(roots, g['appid'])
            games.append({'appid': g['appid'], 'name': g['name'],
                          'cover': cover, 'portrait': portrait,
                          'hero': _library_file(roots, g['appid'], 'library_hero.jpg'),
                          'known': g['appid'] in handled,
                          'installed': True, 'handler': g['appid'] in handled})
        have = {g['appid'] for g in games} | {g['appid'] for g in installed}
        from_hub = hub.installed()
        for appid, path in handled.items():
            if appid in have:
                continue
            rec = from_hub.get(appid, {})
            # A game in the library has Steam's art cached even when it isn't
            # installed; else the hub's header image.
            cover, portrait = _cover_art(roots, appid)
            games.append({'appid': appid,
                          'name': rec.get('name') or scripting.handler_name(path) or f'App {appid}',
                          'cover': cover or rec.get('cover_url', ''), 'portrait': portrait,
                          'hero': _library_file(roots, appid, 'library_hero.jpg'),
                          'known': True, 'installed': False,
                          'handler': True})
        # Non-Steam games (Game.id handlers): Janus sets them up itself, so
        # they count as installed; their art comes from the handler's payload.
        for gid, path in scripting.non_steam_handlers().items():
            self._fetch_art(gid, path)
            cover = _non_steam_art(gid, path, 'cover')
            games.append({'appid': gid, 'name': scripting.handler_name(path) or gid,
                          'cover': cover, 'portrait': bool(cover),
                          'hero': _non_steam_art(gid, path, 'hero'),
                          'known': True, 'installed': True, 'handler': True})
        # Playable first, then "Get handler", then "Not installed".
        games.sort(key=lambda g: (not g['installed'], not g['handler'], g['name'].lower()))
        if games == self._all:
            return          # nothing new: don't rebuild the grid (it would re-animate)
        self._all = games
        self._apply()

    def _apply(self):
        self.beginResetModel()
        self._rows = list(self._all)
        self.endResetModel()

    def set_hub_keys(self, keys: set):
        """The games the hub has handlers for (HubBrowser.available): their
        installed games are listed as "Get handler"."""
        if keys != self._hub_keys:
            self._hub_keys = set(keys)
            self.refresh()


def renumber(players, mover, delta, now=None):
    """Move *mover* (a player dict) one number in direction *delta* (+1/-1).

    Numbers held by pinned players (set within PIN_SECONDS) are skipped when
    there is an unpinned one further along; the mover then takes it and every
    other unpinned player shifts into the freed numbers, keeping their order.
    Returns False when there is nowhere to go (already at the end)."""
    now = time.monotonic() if now is None else now
    n = len(players)
    pinned = {id(p) for p in players
              if p is not mover and now - p.get('set_at', -1e9) < PIN_SECONDS}
    holder = {p['number']: p for p in players}
    cur = mover['number']
    target = None
    k = cur + delta
    while 1 <= k <= n:
        if id(holder[k]) not in pinned:
            target = k
            break
        k += delta
    if target is None:
        if not 1 <= cur + delta <= n:
            return False
        target = cur + delta          # everyone that way is pinned: displace the nearest
    mover['number'] = target
    mover['set_at'] = now
    keep = [p for p in players if id(p) in pinned and p['number'] != target]
    taken = {target} | {p['number'] for p in keep}
    free = [i for i in range(1, n + 1) if i not in taken]
    shift = sorted((p for p in players if p is not mover and p not in keep),
                   key=lambda p: p['number'])
    for p, num in zip(shift, free):
        p['number'] = num
    return True


class Backend(QObject):
    padsChanged = Signal()
    playersChanged = Signal()
    layoutChanged = Signal()
    gameChanged = Signal()
    optionsChanged = Signal()
    runningChanged = Signal()
    statusChanged = Signal()
    messageChanged = Signal()
    launchFinished = Signal()
    profilesChanged = Signal()
    setupRequested = Signal()     # "Set up a controller…" chosen in the options
    removeMappingRequested = Signal(str, str)   # guid, pad name: confirm first
    leaveGame = Signal()          # a non-Steam game's setup gave up: back to the games
    # Setup steps / actions of a non-Steam game run on a worker thread; these
    # bring their progress, end and Janus.warn/error back to the UI thread.
    _actionProgress = Signal(str)
    _actionFinished = Signal(str, str, bool)      # level, text, recheck setup
    _firstRunDone = Signal(str)                   # first_run.run's error, '' = ok
    _setupMessage = Signal(str, str, object)      # level, text, done()
    _infoReady = Signal(int, object)              # generation, {profile id: line}

    def __init__(self, parent=None):
        super().__init__(parent)
        self._games = GameListModel(self)
        self._games.refresh()
        self._hub = HubBrowser(self)
        # The hub's games ("Get handler" cards) and handler updates (badges).
        self._hub.changed.connect(lambda: self._games.set_hub_keys(self._hub.available()))
        self._hub.checkUpdates()
        self._appid = None
        self._game_name = ''
        self._art: dict = {}               # hero / heroBlur / logo URLs
        self._pads: list[dict] = []
        # {'js', 'name', 'vid', 'pid', 'number' (1-based), 'seat' (tile index),
        #  'set_at', 'profile' (profiles.py id)}
        self._players: list[dict] = []
        self._profile_saves = False        # handler: Game.profileSaves
        self._side_by_side_first = False   # handler: Game.sideBySideFirst
        self._monitors = get_all_monitors()
        self._layout_index = 0
        self._layout_memory: dict = {}     # str(player count) -> layout id
        self._handler_path = None
        self._option_specs: list = []      # [(key, spec)]
        self._option_values: dict = {}
        # Render scale / upscale filter (SessionConfig fields shared with the
        # desktop GUI), shown as built-in rows after the handler's options.
        self._display: dict = {}
        self._input_sheet: dict = {}       # the handler sheet (Game.janusInput / steamInput)
        self._input_mode = ''               # SessionConfig.input_mode
        self._nintendo_models: set = set()  # connected (vid, pid): option rows
        self._conflicts_shown: set = set()   # appids warned about two handlers
        self._isolation = 'secondary'
        self._running = False
        self._last_start_focus = 0.0
        self._rows: list[dict] = []        # running session, one per instance
        self._message = None               # (level, text, done, cancel, accept label)
        # Non-Steam game (nonsteam.py): its handler, kept loaded for setup
        # steps and actions; the owner account replacing the Steam account.
        self._setup_script = None
        self._owner = None                 # {name, label} or None
        self._after_setup = None           # what to do once setup is complete
        self._setup_fail = None            # ... and when it's abandoned
        self._actionProgress.connect(self._on_action_progress)
        # Each profile's save line (save_info.py), computed off the UI thread.
        self._profile_info: dict = {}
        self._info_gen = 0
        self._infoReady.connect(self._on_info_ready)
        self._actionFinished.connect(self._on_action_finished)
        self._firstRunDone.connect(self._on_first_run_done)
        self._setupMessage.connect(self._on_script_message)

        self._launcher = LauncherService(self)
        self._launcher.instanceStarted.connect(
            lambda i, pid: self._set_row(i, status=f'Running (pid {pid})'))
        self._launcher.instanceFinished.connect(
            lambda i, rc: self._set_row(i, status=f'Exited ({rc})'))
        self._launcher.instanceError.connect(
            lambda i, msg: self._set_row(i, status=f'Error: {msg}'))
        self._launcher.lobbyDetected.connect(
            lambda i, lobby: self._set_row(i, lobby=str(lobby)))
        self._launcher.allFinished.connect(self._on_all_finished)
        self._launcher.scriptMessage.connect(self._on_script_message)
        # Dynamic players: a re-flow uses the layout this player count would
        # get on the players screen (remembered, else the default).
        self._launcher.layout_for = self._session_layout

        self._refresh_pads()
        self._pad_timer = QTimer(self)
        self._pad_timer.timeout.connect(self._refresh_pads)
        self._pad_timer.start(2000)

    # --- per-game config -------------------------------------------------

    def _load_config(self) -> SessionConfig:
        return PresetManager().load_game(self._appid) or SessionConfig(
            appid=self._appid, game_name=self._game_name, overrides=set())

    def _save_config(self, mutate):
        """Load the game's config, apply *mutate*, save. Only the fields
        Big Picture owns are touched; the desktop GUI's settings stay."""
        cfg = self._load_config()
        cfg.game_name = cfg.game_name or self._game_name
        mutate(cfg)
        PresetManager().save_game(self._appid, cfg)

    # --- games ---------------------------------------------------------

    @Property(QObject, constant=True)
    def games(self):
        return self._games

    @Property(QObject, constant=True)
    def hub(self):
        return self._hub

    @Slot()
    def refreshGames(self):
        """Rescan Steam's libraries and the handler dirs (a game installed or
        a handler added while we were open)."""
        self._games.refresh()

    @Slot(int)
    def installInSteam(self, appid):
        """Open Steam's install dialog for a game that has a handler but
        isn't installed."""
        subprocess.Popen(['xdg-open', f'steam://install/{int(appid)}'],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)

    @Property(str, notify=gameChanged)
    def gameName(self):
        return self._game_name

    @Property(str, notify=gameChanged)
    def heroUrl(self):
        """Wide banner (library_hero.jpg, ~3840x1240) of the selected game."""
        return self._art.get('hero', '')

    @Property(str, notify=gameChanged)
    def heroBlurUrl(self):
        return self._art.get('heroBlur', '')

    @Property(str, notify=gameChanged)
    def logoUrl(self):
        """Transparent title logo (logo.png), shown instead of the name."""
        return self._art.get('logo', '')

    @Slot('QVariant', str)
    def selectGame(self, appid, name):
        if isinstance(appid, float):
            appid = int(appid)
        self._appid = appid
        self._game_name = name
        self._handler_path = scripting.find_handler(appid)
        if scripting.is_steam_key(appid):
            roots = find_all_steam_roots()
            self._art = {'hero': _library_file(roots, appid, 'library_hero.jpg'),
                         'heroBlur': _library_file(roots, appid, 'library_hero_blur.jpg'),
                         'logo': _library_file(roots, appid, 'logo.png')}
        elif self._handler_path is not None:
            self._art = {k: _non_steam_art(appid, self._handler_path, kind)
                         for k, kind in (('hero', 'hero'), ('heroBlur', 'hero_blur'),
                                         ('logo', 'logo'))}
            self._art['heroBlur'] = self._art['heroBlur'] or self._art['hero']
        cfg = self._load_config()
        self._layout_memory = dict(cfg.bigpicture.get('layouts', {}))
        # Two handlers for one game: say which one is used, once per game.
        conflict = scripting.handler_conflict(appid)
        if conflict and appid not in self._conflicts_shown:
            self._conflicts_shown.add(appid)
            self._show_message('warning', conflict, lambda: None)
        self._option_specs, sheet = [], {}
        self._setup_script, self._owner = None, None
        if self._handler_path is not None:
            try:
                hs = scripting.HandlerScript(self._handler_path, appid,
                                             option_values=cfg.script_options)
                self._option_specs, sheet = hs.option_specs, hs.sheet
                if hs.non_steam:
                    hs.progress = self._actionProgress.emit
                    hs.set_notify(self._setupMessage.emit, ui_thread=threading.get_ident())
                    self._setup_script = hs
                    self._owner = nonsteam.owner(hs)
            except scripting.HandlerError as e:
                self._show_message('error', f'The handler script failed to load:\n{e}',
                                   lambda: None)
        self._option_values = {k: cfg.script_options.get(k, spec.get('default'))
                               for k, spec in self._option_specs}
        self._display = {k: getattr(cfg, k) for k in DISPLAY_FIELDS}
        self._input_sheet = dict(sheet)
        self._input_mode = cfg.input_mode
        apply_script_defaults(cfg, sheet)
        self._isolation = cfg.isolation
        self._profile_saves = bool(sheet.get('profileSaves'))
        self._side_by_side_first = bool(sheet.get('sideBySideFirst'))
        self._restore_layout()
        self._profile_info = {}
        self.refreshProfileInfo()
        self.gameChanged.emit()
        self.optionsChanged.emit()
        self.layoutChanged.emit()
        self.profilesChanged.emit()
        if self._setup_script is not None:
            # Not set up: walk the steps; giving up returns to the games, so
            # nobody picks controllers for a game that can't start.
            self._run_setup(lambda: None, on_fail=self.leaveGame.emit)

    # --- non-Steam games: setup steps and actions ------------------------

    @Property(str, notify=gameChanged)
    def ownerLabel(self):
        """What the owner profile is: the Steam account, or the account a
        non-Steam game's handler names (Game.owner, "Microsoft account")."""
        return (self._owner or {}).get('label') or 'Steam account'

    def _profile_name(self, prof) -> str:
        if prof.owner and self._owner:
            return self._owner['name']
        return prof.name

    def _run_setup(self, then, on_fail=None):
        """Run the game's setup steps (Game.setupCheck) one after the other,
        each offered as a question whose A runs its fix; then() once all
        pass. B stops, and so does B on a failed step (A tries it again);
        on_fail() runs when it stops."""
        fail = on_fail or (lambda: None)
        st = nonsteam.setup_status(self._setup_script)
        if st['ready']:
            self._after_setup = self._setup_fail = None
            then()
            return
        self._after_setup, self._setup_fail = then, on_fail
        if not st['action']:
            self._show_message('error', st['message'], fail)
            return
        self._show_message('setup', st['message'],
                           lambda: self._start_action(st['action'], recheck=True),
                           cancel=fail, accept=st['actionLabel'])

    def _start_action(self, key, recheck=False):
        script = self._setup_script
        label = dict(script.action_specs)[key]['label']
        self._show_message('busy', label + '…', lambda: None)

        def work():
            try:
                text, level = nonsteam.run_action(script, key), 'info'
            except scripting.HandlerError as e:
                self._actionFinished.emit('error', str(e), recheck)
                return
            self._actionFinished.emit(level, text, recheck)
        threading.Thread(target=work, name='janus-action', daemon=True).start()

    def _on_action_progress(self, text):
        if self._message is not None and self._message[0] == 'busy':
            self._message = ('busy', text, *self._message[2:])
            self.messageChanged.emit()

    def _on_action_finished(self, level, text, recheck):
        if self._message is not None and self._message[0] == 'busy':
            self._message = None
            self.messageChanged.emit()
        self._owner = nonsteam.owner(self._setup_script) if self._setup_script else None
        self.gameChanged.emit()
        self.profilesChanged.emit()
        self.layoutChanged.emit()
        self.optionsChanged.emit()
        then = self._after_setup if recheck else None
        fail = self._setup_fail if recheck else None
        if level == 'error' and then:
            # A setup step failed: the game can't be played yet. A tries
            # again (the check runs again, so it may be another step now).
            self._show_message(
                'setup', f"{self._game_name} isn't set up yet:\n\n{text}",
                lambda: self._run_setup(then, fail),
                cancel=fail or (lambda: None), accept='Try again')
            return
        go_on = (lambda: self._run_setup(then, fail)) if then else (lambda: None)
        if text:
            self._show_message(level, text, go_on)
        else:
            go_on()

    @Slot(str)
    def runAction(self, key):
        """A maintenance action from the options panel ("Open Prism Launcher")."""
        if self._setup_script is not None and key in dict(self._setup_script.action_specs):
            self._after_setup = self._setup_fail = None
            self._start_action(key)

    # --- pads and players ----------------------------------------------

    @staticmethod
    def _same_pad(player, pad) -> bool:
        """Is *pad* the hardware *player* joined with? uniq (serial/MAC) when
        known, else VID:PID plus the USB port, else VID:PID plus name.
        Another model on the same USB port is the same pad in another mode:
        some pads re-enumerate on a replug (MACHENIKE G3V2 36e8:3001 comes
        back as an Xbox 360 pad 045e:028e). Not over Bluetooth, whose phys is
        the adapter's address, the same for every pad."""
        if (player['vid'], player['pid']) != (pad['vid'], pad['pid']):
            return bool(pad.get('usb') and player.get('usb') and player['phys']
                        and player['phys'] == pad['phys'] and not pad['uniq'])
        if player['uniq'] or pad['uniq']:
            return player['uniq'] == pad['uniq']
        if player['phys'] and pad['phys']:
            return player['phys'] == pad['phys']
        return player['name'] == pad['name']

    def _refresh_pads(self):
        pads = [{'js': c.index, 'name': c.name, 'vid': c.vendor_id,
                 'pid': c.product_id, 'uniq': c.uniq, 'phys': c.port,
                 'usb': c.phys.startswith('usb-'),
                 'battery': pad_battery(c.index)} for c in list_controllers()]
        changed = pads != self._pads
        self._pads = pads
        by_js = {p['js']: p for p in pads}
        now = time.monotonic()
        players_changed = False
        # A player whose pad is gone (or whose js index now belongs to another
        # pad) waits for it instead of leaving straight away.
        for pl in self._players:
            if pl['js'] >= 0 and not (pl['js'] in by_js and self._same_pad(pl, by_js[pl['js']])):
                pl['js'], pl['missing_since'] = -1, now
                players_changed = True
            elif pl['js'] >= 0 and (pl['vid'], pl['pid']) != (by_js[pl['js']]['vid'],
                                                            by_js[pl['js']]['pid']):
                # Back at the same js index as another model (changed mode).
                back = by_js[pl['js']]
                pl.update({k: back[k] for k in ('name', 'vid', 'pid', 'uniq', 'phys', 'usb')})
                players_changed = True
        taken = {pl['js'] for pl in self._players if pl['js'] >= 0}
        for pl in [pl for pl in self._players if pl['js'] < 0]:
            back = next((p for p in pads if p['js'] not in taken and self._same_pad(pl, p)), None)
            if back:
                pl['js'], pl['missing_since'] = back['js'], None
                # The pad may have come back as another model (changed mode).
                pl.update({k: back[k] for k in ('name', 'vid', 'pid', 'uniq', 'phys', 'usb')})
                taken.add(back['js'])
                players_changed = True
            elif now - pl['missing_since'] > RECONNECT_SECONDS:
                self._remove_player(pl)
        if players_changed:
            self._players_changed()
        elif changed:
            self.padsChanged.emit()
        # The options list has a row per connected Nintendo pad model.
        models = {(p['vid'], p['pid']) for p in pads if p['vid'] == NINTENDO_VID}
        if models != self._nintendo_models:
            self._nintendo_models = models
            self.optionsChanged.emit()

    def _player(self, js):
        if js < 0:
            return None
        return next((p for p in self._players if p['js'] == js), None)

    @Property('QVariantList', notify=padsChanged)
    def pads(self):
        num = {p['js']: p['number'] for p in self._players if p['js'] >= 0}
        names = [short_pad_name(p['name'], p['vid'], p['pid']) for p in self._pads]
        out = []
        for i, p in enumerate(self._pads):
            label = names[i]
            if names.count(label) > 1:    # identical pads: number them
                label += f' ({names[:i + 1].count(label)})'
            out.append({'js': p['js'], 'name': label, 'battery': p['battery'],
                        'number': num.get(p['js'], 0)})
        return out

    @Property(int, notify=playersChanged)
    def playerCount(self):
        return len(self._players)

    @Slot(int, result=bool)
    def isJoined(self, js):
        return self._player(js) is not None

    def _players_changed(self):
        self.playersChanged.emit()
        self.padsChanged.emit()
        self.layoutChanged.emit()

    @Slot(int)
    def join(self, js):
        if js < 0 or self._player(js) or len(self._players) >= MAX_PLAYERS:
            return
        pad = next((p for p in self._pads if p['js'] == js), None)
        if pad is None:
            return
        n = len(self._players) + 1
        self._players.append(dict(pad, number=n, seat=n - 1, set_at=-1e9,
                                  missing_since=None,
                                  profile=self._default_profile(pad)))
        self._restore_layout()
        self._players_changed()
        self.profilesChanged.emit()

    # --- profiles --------------------------------------------------------

    def _default_profile(self, pad, taken=None) -> str:
        """The profile a pad joins as: the one it played as last time if it's
        free, else the Steam account if free, else the first free profile,
        else a new "Player N"."""
        if taken is None:
            taken = {p['profile'] for p in self._players}
        known = profiles.load()
        last = profiles.remembered_for_pad(pad_key(pad))
        if last in {p.id for p in known} and last not in taken:
            return last
        free = next((p.id for p in known if p.id not in taken), None)
        if free:
            return free
        k = 2
        while any(p.name.lower() == f'player {k}' for p in known):
            k += 1
        return profiles.create(f'Player {k}').id

    def _saves_tag(self, prof) -> str:
        """'real', 'shared' or 'own' (save_info.saves_tag)."""
        return save_info.saves_tag(prof, self._isolation, self._profile_saves,
                                   self._setup_script is not None)

    @Slot()
    def refreshProfileInfo(self):
        """Recompute every profile's save line ("1.4 GB · played 3 days
        ago", or the handler's Game.profileInfo) on a worker thread; the
        profiles list updates when it's done."""
        if self._appid is None:
            return
        self._info_gen += 1
        gen, key, path = self._info_gen, self._appid, self._handler_path
        opts = dict(self._option_values)
        own, tag_of = self._owner, self._saves_tag
        profs = profiles.load()

        def work():
            try:
                texts = save_info.profile_texts(key, path, opts, profs, tag_of, own)
            except Exception as e:  # noqa: BLE001 — decoration only
                print(f'[bigpicture] profile info failed: {e}', flush=True)
                texts = {}
            self._infoReady.emit(gen, texts)
        threading.Thread(target=work, daemon=True, name='profile-info').start()

    def _on_info_ready(self, gen, texts):
        if gen == self._info_gen:
            self._profile_info = dict(texts)
            self.profilesChanged.emit()

    @Property('QVariantList', notify=profilesChanged)
    def profiles(self):
        """Every profile, with the player number using it (0 = free)."""
        using = {p['profile']: p['number'] for p in self._players}
        return [{'id': p.id, 'name': self._profile_name(p), 'owner': p.owner,
                 'takenBy': using.get(p.id, 0),
                 'info': self._profile_info.get(p.id, '')} for p in profiles.load()]

    @Slot(int, result=str)
    def profileOf(self, js):
        p = self._player(js)
        return p['profile'] if p else ''

    @Slot(int, str)
    def setProfile(self, js, profile_id):
        """This pad's player plays as *profile_id*; a player already using it
        takes this player's profile in exchange (swap)."""
        p = self._player(js)
        if not p:
            return
        for q in self._players:
            if q is not p and q['profile'] == profile_id:
                q['profile'] = p['profile']
        p['profile'] = profile_id
        self.profilesChanged.emit()
        self.layoutChanged.emit()

    @Slot(int, str, result=str)
    def createProfile(self, js, name):
        """New profile for this pad's player. Returns '' or an error."""
        try:
            prof = profiles.create(name)
        except ValueError as e:
            return str(e)
        self.setProfile(js, prof.id)
        self.profilesChanged.emit()
        return ''

    @Slot(str, result=str)
    def profileDeleteText(self, profile_id):
        """What deleting a profile throws away, for the confirmation."""
        prof = profiles.get(profile_id)
        if prof is None:
            return ''
        stats = save_info.total_stats(profile_id)
        what = (f'{save_info.human_size(stats[0])} of saves in every game'
                if stats and stats[0] else 'no saves')
        return (f'{prof.name} is removed with everything Janus keeps for them: '
                f'{what}. This can\'t be undone.')

    @Slot(int, str, str, result=str)
    def deleteProfile(self, js, profile_id, confirm):
        """Delete a virtual profile and all its saves. *confirm* must be the
        word "delete". A profile another player is using can't go; this
        pad's own player moves to another profile. Returns '' or an error."""
        if confirm.strip().lower() != 'delete':
            return 'Type "delete" to confirm.'
        if self._running:
            return "Profiles can't be deleted while games are running."
        me = self._player(js)
        if any(p['profile'] == profile_id and p is not me for p in self._players):
            return 'Another player is using this profile.'
        try:
            profiles.delete(profile_id, delete_data=True)
        except (ValueError, KeyError, OSError) as e:
            return str(e)
        if me is not None and me['profile'] == profile_id:
            me['profile'] = self._default_profile(me)
        self._profile_info.pop(profile_id, None)
        self.profilesChanged.emit()
        self.layoutChanged.emit()
        return ''

    @Slot(str, str, result=str)
    def renameProfile(self, profile_id, name):
        try:
            profiles.rename(profile_id, name)
        except (ValueError, KeyError) as e:
            return str(e)
        self.profilesChanged.emit()
        self.layoutChanged.emit()
        return ''

    def _remove_player(self, p):
        self._players.remove(p)
        for q in self._players:
            if q['number'] > p['number']:
                q['number'] -= 1
        self._restore_layout()
        self._players_changed()
        self.profilesChanged.emit()

    @Slot(int)
    def leave(self, js):
        p = self._player(js)
        if p:
            self._remove_player(p)

    @Slot(int, int)
    def changeNumber(self, js, delta):
        p = self._player(js)
        if p and renumber(self._players, p, 1 if delta > 0 else -1):
            self._players_changed()
            # Redraw when the pin runs out (the tile's "pinned" marker).
            QTimer.singleShot(int(PIN_SECONDS * 1000) + 50, self.layoutChanged.emit)

    @Slot(int, str)
    def moveSeat(self, js, direction):
        """Swap this player's tile with the nearest tile in *direction*
        (up/down/left/right), or move into it if it's empty."""
        p = self._player(js)
        tiles = self._tile_rects()
        if not p or p['seat'] >= len(tiles):
            return
        ax, ay, aw, ah = tiles[p['seat']]
        cx, cy = ax + aw / 2, ay + ah / 2
        dirx, diry = {'left': (-1, 0), 'right': (1, 0),
                      'up': (0, -1), 'down': (0, 1)}[direction]
        best, best_score = None, None
        for i, (x, y, w, h) in enumerate(tiles):
            dx, dy = x + w / 2 - cx, y + h / 2 - cy
            along = dx * dirx + dy * diry
            across = abs(dx * diry) + abs(dy * dirx)
            if along <= 0:
                continue
            score = along + 2 * across
            if best_score is None or score < best_score:
                best, best_score = i, score
        if best is None:
            return
        other = next((q for q in self._players if q['seat'] == best), None)
        if other:
            other['seat'] = p['seat']
        p['seat'] = best
        self._players_changed()

    @Slot(int, int)
    def moveSeatTo(self, js, seat):
        """Put this player's tile on *seat* (a tile dragged with the mouse),
        swapping with whoever sat there."""
        p = self._player(js)
        if not p or not 0 <= seat < len(self._tile_rects()) or seat == p['seat']:
            return
        other = next((q for q in self._players if q['seat'] == seat), None)
        if other:
            other['seat'] = p['seat']
        p['seat'] = seat
        self._players_changed()

    # --- layout --------------------------------------------------------

    def _options(self):
        return layout_options(self._monitors, len(self._players), self._side_by_side_first)

    def _restore_layout(self):
        """Pick the remembered layout for this player count, and give the
        players seats 0..n-1 in their current seat order."""
        opts = self._options()
        want = self._layout_memory.get(str(max(1, len(self._players))))
        self._layout_index = next((i for i, o in enumerate(opts) if o['id'] == want), 0)
        for i, p in enumerate(sorted(self._players, key=lambda p: (p['seat'], p['number']))):
            p['seat'] = i

    @Slot(int)
    def cycleLayout(self, delta):
        opts = self._options()
        self._layout_index = (self._layout_index + delta) % len(opts)
        self._layout_memory[str(max(1, len(self._players)))] = opts[self._layout_index]['id']
        self.layoutChanged.emit()

    @Property(str, notify=layoutChanged)
    def layoutName(self):
        return self._options()[self._layout_index]['name']

    @Property(int, notify=layoutChanged)
    def layoutCount(self):
        return len(self._options())

    def _tile_rects(self):
        """Current layout's tiles in desktop px: [(x, y, w, h)]."""
        out = []
        for mi, x, y, w, h in self._options()[self._layout_index]['tiles']:
            m = self._monitors[mi]
            out.append((m.x + round(x * m.width), m.y + round(y * m.height),
                        round(w * m.width), round(h * m.height)))
        return out

    def _bbox(self):
        x0 = min(m.x for m in self._monitors)
        y0 = min(m.y for m in self._monitors)
        x1 = max(m.x + m.width for m in self._monitors)
        y1 = max(m.y + m.height for m in self._monitors)
        return x0, y0, max(1, x1 - x0), max(1, y1 - y0)

    @Property(float, notify=layoutChanged)
    def previewAspect(self):
        _, _, w, h = self._bbox()
        return w / h

    @Property('QVariantList', notify=layoutChanged)
    def previewMonitors(self):
        """All monitors, normalized to their bounding box."""
        x0, y0, bw, bh = self._bbox()
        used = {t[0] for t in self._options()[self._layout_index]['tiles']}
        return [{'x': (m.x - x0) / bw, 'y': (m.y - y0) / bh,
                 'w': m.width / bw, 'h': m.height / bh,
                 'name': m.name, 'used': i in used}
                for i, m in enumerate(self._monitors)]

    @Property('QVariantList', notify=layoutChanged)
    def previewTiles(self):
        """Tiles normalized to the monitors' bounding box, with who sits there."""
        x0, y0, bw, bh = self._bbox()
        out = []
        now = time.monotonic()
        by_id = {p.id: p for p in profiles.load()}
        for i, (x, y, w, h) in enumerate(self._tile_rects()):
            p = next((q for q in self._players if q['seat'] == i), None)
            prof = by_id.get(p['profile']) if p else None
            out.append({
                'x': (x - x0) / bw, 'y': (y - y0) / bh, 'w': w / bw, 'h': h / bh,
                'number': p['number'] if p else 0,
                'js': p['js'] if p else -1,
                'name': short_pad_name(p['name'], p['vid'], p['pid']) if p else '',
                'reconnecting': bool(p) and p['js'] < 0,
                'pinned': bool(p) and now - p['set_at'] < PIN_SECONDS,
                'profile': self._profile_name(prof) if prof else '',
                'saves': self._saves_tag(prof) if prof else '',
            })
        return out

    def _instances(self) -> list[InstanceConfig]:
        """One InstanceConfig per player, in player-number order: P1 is
        instance 1 (the auto-join host). Saves follow the player's profile."""
        rects = self._tile_rects()
        tiles = self._options()[self._layout_index]['tiles']
        out = []
        for p in sorted(self._players, key=lambda p: p['number']):
            x, y, w, h = rects[p['seat']]
            out.append(InstanceConfig(
                controller_index=p['js'],
                instance_number=p['number'],
                gamescope_width=w, gamescope_height=h, pos_x=x, pos_y=y,
                monitor_name=self._monitors[tiles[p['seat']][0]].name,
                controller_vid=p['vid'], controller_pid=p['pid'],
                controller_name=p['name'],
                profile_id=p['profile'],
            ))
        return out

    # --- handler options -------------------------------------------------

    @Property('QVariantList', notify=optionsChanged)
    def options(self):
        out = []
        for key, spec in self._option_specs:
            typ = spec.get('type', 'string')
            val = self._option_values.get(key)
            if typ == 'bool':
                display = 'On' if val else 'Off'
            elif val is None or val == '':
                display = '(empty)' if typ == 'string' else ''
            else:
                display = str(val)
            out.append({'key': key, 'label': str(spec.get('label', key)),
                        'description': str(spec.get('description', '')),
                        'type': typ, 'display': display,
                        'text': '' if val is None else str(val),
                        'group': 'Game'})
        if self._setup_script is not None:
            st = nonsteam.setup_status(self._setup_script)
            for key, spec in nonsteam.menu_actions(self._setup_script):
                out.append({'key': '!' + key, 'label': str(spec['label']),
                            'description': str(spec.get('description', '')) +
                            (f"\n\nInstalled: {st['status']}" if st['status'] else ''),
                            'type': 'action', 'display': '', 'text': '',
                            'group': 'Maintenance'})
        return out + self._display_rows() + self._controller_rows()

    @Property('QVariantList', notify=optionsChanged)
    def globalOptions(self):
        """The games screen's Settings (Start): what isn't per game — the
        controller setup, every controller mapping made in Janus (connected
        or not; '#unmap:<guid>' asks, then removes it) and Nintendo pads'
        buttons by position."""
        rows = self._pad_rows()
        maps = []
        for guid, m in sorted(pad_mappings.janus_mappings().items(),
                              key=lambda kv: kv[1].split(',')[1].lower()):
            name = m.split(',')[1]
            maps.append({
                'key': f'#unmap:{guid}', 'label': f'{name}: button mapping',
                'type': 'action', 'display': 'Remove', 'text': '',
                'group': 'Controllers',
                'description': f'The buttons of {name} as set up in Janus. Remove '
                               'it if they\'re wrong (it asks first); the controller '
                               'goes back to its standard mapping, or to not '
                               'recognised. Set it up again with "Set up a '
                               'controller…".'})
        return rows[:1] + maps + rows[1:] + self._about_rows()   # maps after "Set up a controller…"

    def _about_rows(self):
        """The "About" group: the version ('info': nothing to change) and two
        links ('link': opened by a mouse click only, OptionsPanel.qml; A does
        nothing)."""
        kofi = about.KOFI_URL
        return [
            {'key': '#about:version', 'label': 'Janus Split Screen', 'type': 'info',
             'display': about.version_text(), 'text': '', 'group': 'About',
             'description': 'The Janus version you are running.'},
            {'key': '#about:author', 'label': f'Made by {about.AUTHOR}', 'type': 'link',
             'display': about.AUTHOR_URL.removeprefix('https://'), 'text': '',
             'url': about.AUTHOR_URL, 'group': 'About',
             'description': 'Click to open the GitHub page in your browser.'},
            {'key': '#about:kofi', 'label': 'Support Janus on Ko-fi',
             'type': 'link' if kofi else 'info',
             'display': kofi.removeprefix('https://') if kofi else 'Coming soon',
             'text': '', 'url': kofi, 'group': 'About',
             'description': ('Click to open the Ko-fi page in your browser. Thank you!' if kofi
                             else 'A Ko-fi page is on its way.')},
        ]

    def _controller_rows(self):
        """Built-in "Controllers" rows: the input mode (key '@input_mode'),
        then _pad_rows()."""
        if self._appid is None:
            return []
        modes = cinput.supported(self._input_sheet)
        mode = cinput.resolve(self._input_sheet, self._input_mode or None)
        need = ('"Disable Steam Input"' if mode == cinput.JANUS
                else '"Enable Steam Input"')
        rows = [{'key': '@input_mode', 'label': 'Controller input', 'type': 'choice',
                 'display': cinput.LABELS[mode], 'text': '', 'group': 'Controllers',
                 'description': (
                     'Janus input: Janus turns each controller into its own Xbox '
                     'controller and every game sees only its own. Steam Input: Steam '
                     'remaps the controllers (trackpads, gyro, Steam layouts). '
                     f'This game needs {need} in Steam (Properties → Controller).'
                     + ('' if len(modes) > 1 else
                        f' This game only works with {cinput.LABELS[modes[0]]}.'))}]
        return rows + self._pad_rows()

    def _pad_rows(self):
        """The controller setup wizard ('#setup') and, per connected
        Nintendo pad model, face buttons by position ('#pos:vvvv:pppp')."""
        rows = [{'key': '#setup', 'label': 'Set up a controller…', 'type': 'action',
                 'display': (f'{n} set up' if (n := len(pad_mappings.janus_mappings()))
                             else ''),
                 'text': '', 'group': 'Controllers',
                 'description': 'For a controller Janus input doesn\'t recognise (it '
                                'would do nothing in games), or to change its buttons: '
                                'press each button it asks for. Saved for every game. '
                                'The list shows which controllers are set up in Janus; '
                                'X there removes a mapping.'}]
        seen = set()
        for c in list_controllers():
            if c.vendor_id != NINTENDO_VID or (c.vendor_id, c.product_id) in seen:
                continue
            seen.add((c.vendor_id, c.product_id))
            on = pad_mappings.positional(c.vendor_id, c.product_id)
            rows.append({
                'key': f'#pos:{c.vendor_id:04x}:{c.product_id:04x}',
                'label': f'{short_pad_name(c.name, c.vendor_id, c.product_id)}: '
                         'buttons by position',
                'type': 'bool', 'display': 'On' if on else 'Off', 'text': '',
                'group': 'Controllers',
                'description': 'Janus input. Off: the button labelled A is Xbox A '
                               '(right). On: the bottom button is Xbox A, as on an '
                               'Xbox controller. For every game.'})
        return rows

    def _step_input_mode(self, delta):
        modes = cinput.supported(self._input_sheet)
        cur = cinput.resolve(self._input_sheet, self._input_mode or None)
        mode = modes[(modes.index(cur) + (1 if delta >= 0 else -1)) % len(modes)]
        self._input_mode = mode

        def mutate(cfg):
            cfg.input_mode = mode
        self._save_config(mutate)
        self.optionsChanged.emit()

    def _display_rows(self):
        """Built-in rows (keys start with '@'): render scale, and below 100%
        the upscale filter (+ sharpness for fsr/nis)."""
        d = self._display
        if not d:
            return []
        scale = int(d['render_scale'])
        rows = [{'key': '@render_scale', 'label': 'Render scale', 'type': 'choice',
                 'display': f'{scale}%', 'text': '', 'group': 'Display',
                 'description': 'The game renders at this share of its screen area '
                                'and is stretched to fill it. Lower runs faster. '
                                'Games whose menus are drawn at a fixed pixel size '
                                '(many pixel-art games) also get a bigger UI; '
                                'others only get softer.'}]
        if scale < 100:
            filt = d['upscale_filter']
            rows.append({'key': '@upscale_filter', 'label': 'Upscale filter',
                         'type': 'choice', 'display': UPSCALE_LABELS.get(filt, filt),
                         'text': '', 'group': 'Display',
                         'description': 'Smooth: soft, safe. FSR / NIS: sharper, for '
                                        '3D games. Nearest / Pixel: crisp pixels for '
                                        'pixel-art games (best at 50%).'})
            if filt in ('fsr', 'nis'):
                rows.append({'key': '@upscale_sharpness', 'label': 'Sharpness',
                             'type': 'int', 'display': str(int(d['upscale_sharpness'])),
                             'text': '', 'group': 'Display',
                             'description': 'FSR / NIS sharpening: 0 = sharpest, '
                                            '20 = softest.'})
        fps = int(d.get('fps_limit', perf.DEFAULT_FPS_LIMIT))
        rows.append({'key': '@fps_limit', 'label': 'Frame limit', 'type': 'choice',
                     'display': f'{fps} fps' if fps else 'Unlimited',
                     'text': '', 'group': 'Display',
                     'description': 'Each player\'s game runs at most this many frames '
                                    'per second. A game running faster than the screen '
                                    'takes graphics power the other players need.'})
        return rows

    def _step_display(self, key, delta):
        d = dict(self._display)
        step = 1 if delta >= 0 else -1
        if key == '@render_scale':
            i = RENDER_SCALES.index(d['render_scale']) if d['render_scale'] in RENDER_SCALES else len(RENDER_SCALES) - 1
            d['render_scale'] = RENDER_SCALES[(i + step) % len(RENDER_SCALES)]
        elif key == '@upscale_filter':
            names = list(UPSCALE_LABELS)
            i = names.index(d['upscale_filter']) if d['upscale_filter'] in names else 0
            d['upscale_filter'] = names[(i + step) % len(names)]
        elif key == '@upscale_sharpness':
            d['upscale_sharpness'] = max(0, min(20, int(d['upscale_sharpness']) + step))
        elif key == '@fps_limit':
            steps = list(perf.FPS_LIMITS)
            cur = int(d.get('fps_limit', perf.DEFAULT_FPS_LIMIT))
            i = steps.index(cur) if cur in steps else steps.index(perf.DEFAULT_FPS_LIMIT)
            d['fps_limit'] = steps[(i + step) % len(steps)]
        else:
            return
        self._display = d

        def mutate(cfg):
            for k, v in d.items():
                setattr(cfg, k, v)
        self._save_config(mutate)
        self.optionsChanged.emit()

    def _set_option(self, key, value):
        self._option_values[key] = value
        values = dict(self._option_values)

        def mutate(cfg):
            cfg.script_options = {**cfg.script_options, **values}
        self._save_config(mutate)
        self.optionsChanged.emit()

    @Slot(str, int)
    def stepOption(self, key, delta):
        """A / ◀▶ on an option: toggle a bool, cycle a choice, step an int."""
        if key == '@input_mode':
            self._step_input_mode(delta)
            return
        if key == '#setup':
            if delta > 0:
                self.setupRequested.emit()
            return
        if key.startswith('#about:'):      # info and mouse-only links
            return
        if key.startswith('#unmap:'):
            if delta > 0:
                guid = key[7:]
                m = pad_mappings.janus_mappings().get(guid, '')
                self.removeMappingRequested.emit(guid, m.split(',')[1] if m else guid)
            return
        if key.startswith('#pos:'):
            vid, pid = (int(x, 16) for x in key[5:].split(':'))
            pad_mappings.set_positional(vid, pid, not pad_mappings.positional(vid, pid))
            self.optionsChanged.emit()
            return
        if key.startswith('@'):
            self._step_display(key, delta)
            return
        if key.startswith('!'):
            if delta > 0:          # A (not ◀)
                self.runAction(key[1:])
            return
        spec = dict(self._option_specs).get(key)
        if spec is None:
            return
        typ = spec.get('type', 'string')
        val = self._option_values.get(key)
        if typ == 'bool':
            self._set_option(key, not val)
        elif typ == 'choice':
            choices = [str(c) for c in spec.get('choices') or []]
            if choices:
                i = choices.index(str(val)) if str(val) in choices else -1
                self._set_option(key, choices[(i + (1 if delta >= 0 else -1)) % len(choices)])
        elif typ == 'int':
            self._set_option(key, int(val or 0) + delta)

    @Slot(str, str)
    def setOptionText(self, key, text):
        self._set_option(key, text)

    # --- launch --------------------------------------------------------

    @Property(bool, notify=runningChanged)
    def running(self):
        return self._running

    @Property('QVariantList', notify=statusChanged)
    def sessionRows(self):
        return list(self._rows)

    @Property(bool, notify=statusChanged)
    def launching(self):
        """Some instance is still starting."""
        return self._running and any(r['status'] == 'Starting…' for r in self._rows)

    def _set_row(self, i, **kw):
        if 0 <= i < len(self._rows):
            self._rows[i] = {**self._rows[i], **kw}
            self.statusChanged.emit()

    @Slot(result=bool)
    def launch(self):
        """Start the session; a reason it can't is shown as a message."""
        error = self._launch()
        self._report_launch(error)
        return not error

    def _launch(self) -> str:
        """Start the session. Returns '' or an error message."""
        if self._appid is None:
            return 'Pick a game first.'
        if self._handler_path is None:
            return scripting.no_handler_message(self._appid)
        if not self._players:
            return 'Press A on a controller to join first.'
        if self._setup_script is not None and \
                not nonsteam.setup_status(self._setup_script)['ready']:
            # Set it up first (each step asks), then come back here.
            self._run_setup(lambda: self._report_launch(self._launch()))
            return ''
        gone = sorted(p['number'] for p in self._players if p['js'] < 0)
        if gone:
            return (f"P{', P'.join(map(str, gone))}'s controller is disconnected. "
                    f'Reconnect it, or wait {int(RECONNECT_SECONDS)} s and that '
                    'player is removed.')
        if ws.session_kind() != 'kwin-wayland':
            import nested_kwin
            if nested_kwin.in_gamescope_session():
                # Game Mode, but main.py couldn't start the nested KWin.
                why = nested_kwin.kwin_problem() or 'it was disabled (JANUS_NO_NESTED_KWIN)'
                return ('In Game Mode Janus places the game tiles with its own KWin, '
                        f'but {why}. On SteamOS it comes with the desktop.')
            return ('Janus needs KDE Plasma (KWin) on Wayland: the game tiles '
                    'are positioned by KWin window rules.')
        missing = ws.missing_tools()
        if missing:
            return 'Missing tools: ' + ', '.join(missing)
        problem = prereqs.gamescope_problem()
        if problem:
            return problem

        # Remember this player count's layout for next time.
        memory = dict(self._layout_memory)
        self._save_config(lambda cfg: cfg.bigpicture.update(layouts=memory))

        config = self._load_config()
        script = None
        try:
            sheet = (scripting.HandlerScript(self._handler_path, self._appid).sheet
                     if self._handler_path else {})
            apply_script_defaults(config, sheet)
            # Fresh script for the session, built with the resolved settings
            # (as the desktop GUI does).
            if self._handler_path:
                script = scripting.HandlerScript(
                    self._handler_path, self._appid,
                    option_values=config.script_options, verbose=config.verbose)
        except scripting.HandlerError as e:
            return f'The handler script failed to load:\n{e}'
        def checked(mode):
            """The game's Steam Input setting must suit the input mode; when
            the other mode would work, A plays with it this time."""
            try:
                steam_input.check(self._appid, script, self._game_name or None, mode)
            except steam_input.SteamInputNotEnabled as e:
                if not e.alternative:
                    script.finish()
                    return str(e)
                # Bind it now: Python deletes `e` when the except block ends,
                # long before the player answers.
                alternative = e.alternative
                self._show_message(
                    'choice', str(e),
                    lambda: self._report_launch(checked(alternative)),
                    cancel=script.finish,
                    accept=f'Play with {cinput.LABELS[e.alternative]}')
                return ''
            config.input_mode = mode
            return warn_then_launch(mode)

        def launch_message():
            # What the players must know first: the launch waits for A (B = back).
            message = scripting.launch_message(script)
            if not message:
                return self._start(config, script)
            self._show_message(
                'launch', message,
                lambda: self._report_launch(self._start(config, script)),
                cancel=script.finish)
            return ''

        def warn_then_launch(mode):
            # Warnings the players may play through (A), one after the other:
            # pads that work badly in this input mode, pads without a Gamepad
            # layout (Steam Input), a missing Steam Linux Runtime.
            pads = session_pads(self._instances())
            warnings = cinput.pad_warnings(mode, pads)
            if mode == cinput.STEAM:
                layout = steam_input.gamepad_layout_warning(self._appid, script, pads)
                if layout:
                    warnings.append(layout)
            try:
                runtime = prereqs.runtime_warning(
                    SteamGame(self._appid, launch_command=config.launch_command or None),
                    script)
            except FileNotFoundError:
                runtime = None
            if runtime:
                warnings.append(runtime)
            return next_step(warnings)

        def next_step(warnings, i=0):
            if i == len(warnings):
                return launch_message()
            self._show_message(
                'launch-warning', warnings[i],
                lambda: self._report_launch(next_step(warnings, i + 1)),
                cancel=script.finish)
            return ''
        build = None
        exe, cmd = config.exe_name or None, config.launch_command or None
        if prereqs.expected_build(script, exe, cmd) is not None:   # not for non-Steam games
            try:
                build = prereqs.build_problem(SteamGame(self._appid), script, exe, cmd)
            except FileNotFoundError:
                pass            # the launch itself reports it
        if build:               # Linux vs Windows build: it can't start
            if script is not None:
                script.finish()
            return build
        # Never started from Steam (no Proton prefix): Steam starts it once
        # first, then the launch begins again from the top.
        import first_run
        try:
            game = SteamGame(self._appid)
        except FileNotFoundError:
            game = None
        if game is not None and first_run.needed(game, script):
            if script is not None:
                script.finish()
            self._show_message('busy', first_run.message(game), lambda: None)

            def work():
                err = first_run.run(game, progress=self._actionProgress.emit)
                self._firstRunDone.emit(err or '')
            threading.Thread(target=work, daemon=True, name='first-run').start()
            return ''
        return checked(cinput.resolve(script, config.input_mode or None))

    def _on_first_run_done(self, err):
        if self._message is not None and self._message[0] == 'busy':
            self._message = None
            self.messageChanged.emit()
        if err:
            self._show_message('error', err, lambda: None)
        else:
            self._report_launch(self._launch())

    def _report_launch(self, error):
        if error:
            self._show_message('error', error, lambda: None)

    def _start(self, config, script) -> str:
        """Launch *config*. Returns '' or an error message."""
        config.instances = self._instances()
        config.goldberg_host_instance = 1        # P1 hosts under auto-join
        # Each pad joins as the same player next time.
        profiles.remember_pads({pad_key(p): p['profile'] for p in self._players})

        names = {p.id: self._profile_name(p) for p in profiles.load()}
        self._rows = [self._row(c, names) for c in config.instances]
        self.statusChanged.emit()
        self._running = True
        self.runningChanged.emit()
        try:
            self._launcher.launch_config(config, script=script)
        except Exception as e:
            self._launcher.stop_all()
            self._running = False
            self.runningChanged.emit()
            return f'Launch failed: {e}'
        # Goldberg auto-join: the others start once P1 has a lobby.
        for i in self._launcher._pending_autojoin:
            self._rows[i]['status'] = "Waiting for P1's lobby…"
        self.statusChanged.emit()
        if not prereqs.steam_running():
            self._show_message('warning', prereqs.STEAM_NOT_RUNNING, lambda: None)
        return ''

    @staticmethod
    def _row(c, names) -> dict:
        return {'number': c.instance_number, 'js': c.controller_index,
                'name': names.get(c.profile_id, '') + '  ·  ' + short_pad_name(
                    c.controller_name or '', c.controller_vid, c.controller_pid),
                'status': 'Starting…', 'lobby': ''}

    # --- dynamic players ---------------------------------------------------

    def _session_layout(self, n):
        """Tiles for n players in the running session: the layout the
        players screen remembers for n, else the default."""
        from layouts import default_rects
        return default_rects(self._monitors, n, self._side_by_side_first,
                             self._layout_memory.get(str(n)))

    @Property(str, notify=statusChanged)
    def joinHint(self):
        """How a free controller joins the running session ('' = it can't)."""
        if not self._running or self._launcher.join_problem():
            return ''
        return 'Hold Start on a free controller to join'

    @Slot(int)
    def padHeldStart(self, js):
        """Start held on a pad that isn't playing: it joins the running
        session as a new player (Janus input, Game.lateJoin), the others'
        tiles re-flow."""
        if not self._running:
            return
        active = self._launcher.active_instances()
        if any(c.controller_index == js for c in active):
            return                         # already playing (Switch pads read via hidraw)
        pad = next((p for p in self._pads if p['js'] == js), None)
        if pad is None:
            return
        problem = self._launcher.join_problem()
        if problem:
            print(f'[bigpicture] join refused: {problem}', flush=True)
            self._show_message('warning', problem, lambda: None)
            return
        profile = self._default_profile(pad, {c.profile_id for c in active})
        try:
            index = self._launcher.join_pad(js, profile)
        except (ValueError, KeyError) as e:
            print(f'[bigpicture] join failed: {e}', flush=True)
            self._show_message('error', f"The new player couldn't join: {e}", lambda: None)
            return
        cfg = self._launcher.instance_config(index)
        number = cfg.instance_number
        names = {p.id: self._profile_name(p) for p in profiles.load()}
        for r in self._rows:               # a leaver's old row, if they came back
            if r['number'] == number:
                r['number'] = 0            # hidden on the running screen
        while len(self._rows) < index:
            self._rows.append({'number': 0, 'js': -1, 'name': '', 'status': '', 'lobby': ''})
        row = self._row(cfg, names)
        if index in self._launcher._pending_autojoin:
            row['status'] = "Waiting for P1's lobby…"
        self._rows[index:index + 1] = [row]
        profiles.remember_pads({pad_key(pad): profile})
        # On the players screen afterwards too.
        if not self._player(js) and len(self._players) < MAX_PLAYERS:
            self._players.append(dict(pad, number=len(self._players) + 1,
                                      seat=len(self._players), set_at=-1e9,
                                      missing_since=None, profile=profile))
            self._restore_layout()
            self._players_changed()
        self.statusChanged.emit()

    @Slot(int)
    def removeInstance(self, index):
        """Take one player out of the running session (asks first); their
        game is closed and the others' tiles re-flow."""
        if not self._running or not 0 <= index < len(self._rows):
            return
        row = self._rows[index]
        self._show_message(
            'remove', f"Remove P{row['number']} from the session? Their game is closed.",
            lambda: self._launcher.stop_instance(index), cancel=lambda: None,
            accept=f"Remove P{row['number']}")

    @Slot()
    def stop(self):
        self._launcher.stop_all()
        self._on_all_finished()

    def _on_all_finished(self):
        if self._running:
            self._running = False
            self.runningChanged.emit()
            self.launchFinished.emit()
            self.refreshProfileInfo()     # the session wrote saves

    # --- handler messages (Janus.warn / Janus.error) -----------------------

    def _on_script_message(self, level, text, done):
        # The hook that raised it blocks until done() — i.e. until the
        # player dismisses the popup.
        self._show_message(level, text, done)

    def _show_message(self, level, text, done, cancel=None, accept=''):
        """Show a popup; done() runs when it's dismissed. With *cancel* it's
        a question (level 'launch': the handler's launch message; 'setup': a
        non-Steam game's setup step, A = *accept*): A runs done(), B — or
        another popup replacing it — runs cancel(). Level 'busy' (an action
        running) has no buttons; the action's end replaces it."""
        if self._message is not None:
            self._close_message(accept=False)
        self._message = (level, text, done, cancel, accept)
        self.messageChanged.emit()

    def _close_message(self, accept):
        if self._message is None:
            return
        _, _, done, cancel, _ = self._message
        self._message = None
        self.messageChanged.emit()
        (done if accept or cancel is None else cancel)()

    @Property(str, notify=messageChanged)
    def messageText(self):
        return self._message[1] if self._message else ''

    @Property(str, notify=messageChanged)
    def messageLevel(self):
        return self._message[0] if self._message else ''

    @Property(str, notify=messageChanged)
    def messageAccept(self):
        """The A button's label for a 'setup' question ("Download ...")."""
        return self._message[4] if self._message else ''

    @Slot()
    def dismissMessage(self):
        if self._message is not None and self._message[0] == 'busy':
            return             # an action is running; its end closes this
        self._close_message(accept=True)

    @Slot()
    def cancelMessage(self):
        if self._message is not None and self._message[0] == 'busy':
            return
        self._close_message(accept=False)

    def checkStartup(self, shortcut_appid: int = 0):
        """Startup popups, one after the other: gamescope missing (nothing can
        launch), Steam not running, then, when Steam started us, the overlay
        warning."""
        def overlay():
            if shortcut_appid:
                self.checkSteamOverlay(shortcut_appid)

        def steam():
            if prereqs.steam_running():
                overlay()
            else:
                self._show_message('warning', prereqs.STEAM_NOT_RUNNING, overlay)
        problem = prereqs.gamescope_problem()
        if problem:
            self._show_message('error', problem, steam)
        else:
            steam()

    def checkSteamOverlay(self, shortcut_appid: int):
        """Warn when Steam's overlay is (or may be) on for the Janus shortcut.
        It must be off: on a native Wayland window it can't draw, so it never
        works, and with it on Steam's overlay library is loaded into the
        launcher for nothing (its Steam + X keyboard included)."""
        from src.bigpicture.steam_shortcut import overlay_enabled
        try:
            on = overlay_enabled(find_all_steam_roots(), shortcut_appid)
        except Exception as e:
            print(f'[bigpicture] overlay check failed: {e}', flush=True)
            return
        if on is False:
            return
        how = ("In Steam, open this shortcut's Properties → General and turn off "
               "\"Enable Steam Overlay while in-game\".")
        if on:
            text = "The Steam overlay is enabled for Janus. Please turn it off. " + how
        else:
            text = ("The Steam overlay may be enabled for Janus: this shortcut follows "
                    "Steam's global overlay setting. Please turn it off for this "
                    "shortcut. " + how)
        self._show_message('warning', text, lambda: None)

    @Slot(int)
    def padStartInactive(self, js):
        """Start on a pad while another window has the focus: bring the
        launcher back, unless games are running (then Start is theirs, and
        focus_pads.py raises their tiles). At most once a second."""
        if self._running:
            return
        now = time.monotonic()
        if now - self._last_start_focus < 1.0:
            return
        self._last_start_focus = now
        print(f'[bigpicture] Start on js{js}: bringing the launcher back', flush=True)
        self.activateWindow()

    @Slot()
    def activateWindow(self):
        """Bring the launcher back to the front. A Wayland client can't
        unminimize or focus itself, so ask KWin (off the GUI thread: it shells
        out to qdbus6)."""
        threading.Thread(target=ws.activate_pid_window, args=(os.getpid(),),
                         daemon=True).start()

    @Slot()
    def shutdown(self):
        if self._message is not None:
            self._close_message(accept=False)
        if self._launcher.is_running():
            self._launcher.stop_all()

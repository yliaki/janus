"""Main window - assembles all widgets and wires signals/slots."""

import shlex
import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QGroupBox, QMessageBox, QDialog, QTextEdit, QDialogButtonBox,
    QProgressDialog,
)

from .models.config import (
    InstanceConfig, SessionConfig, apply_script_defaults,
)
from .models.controller_model import ControllerModel
from .models.presets import PresetManager
from .services.launcher_service import LauncherService, expand_game_args, session_pads
import scripting  # host module; sys.path extended by launcher_service import
import nonsteam
import profiles
import save_info
import prereqs
import steam_input
import controller_input as input_mode
from steam import SteamGame
from .widgets.game_selector import GameSelector
from .widgets.global_settings import GlobalSettings
from .widgets.layout_editor import LayoutEditor
from .widgets.lobby_panel import LobbyPanel
from .widgets.process_bar import ProcessBar
from .widgets.slot_item import SlotItem


class MainWindow(QMainWindow):
    # A non-Steam game's action runs on a worker thread; these bring its
    # progress, end and Janus.warn/error back to the UI thread.
    _actionProgress = Signal(str)
    _actionFinished = Signal(str, str, object)    # level, text, then()
    _setupMessage = Signal(str, str, object)      # level, text, done()
    _hubUpdates = Signal(object)                  # {game key: latest hub version}

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Janus Split Screen - Local Multiplayer Setup")
        self.setMinimumSize(900, 600)
        self.resize(1100, 720)

        self._appid = None
        self._game_name = None
        # Handler-script info for the selected game:
        # {'path', 'sheet', 'specs', 'label', 'error'}
        self._script_info = self._empty_script_info()
        self._conflicts_shown: set = set()   # appids warned about two handlers
        self._bigpicture: dict = {}   # Big Picture section of the loaded config
        # Non-Steam game (nonsteam.py): its handler, kept loaded for setup
        # steps and actions.
        self._setup_script = None
        self._progress = None         # QProgressDialog while an action runs

        # Shared models
        self._controller_model = ControllerModel(self)
        self._controller_model.refresh()

        # Services
        self._launcher = LauncherService(self)
        self._owner = None
        SlotItem.profile_info = self._profile_info
        SlotItem.session_running = self._launcher.is_running
        self._presets = PresetManager()

        # --- Build UI ---
        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(6, 6, 6, 6)
        root_layout.setSpacing(4)

        # Main splitter (left | right)
        splitter = QSplitter(Qt.Horizontal)
        root_layout.addWidget(splitter, stretch=1)

        # --- Left column ---
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)

        self._game_selector = GameSelector(self._presets)
        left_layout.addWidget(self._game_selector, stretch=1)

        self._global_settings = GlobalSettings()
        group = QGroupBox("Settings")
        group_layout = QVBoxLayout(group)
        group_layout.addWidget(self._global_settings)
        left_layout.addWidget(group)

        splitter.addWidget(left)

        # --- Right column ---
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)

        self._layout_editor = LayoutEditor(self._controller_model)
        right_layout.addWidget(self._layout_editor, stretch=1)

        splitter.addWidget(right)
        splitter.setSizes([350, 650])

        # Lobby panel (between layout area and process bar). Hidden until
        # at least one instance is configured.
        self._lobby_panel = LobbyPanel()
        root_layout.addWidget(self._lobby_panel)

        # Process bar (bottom)
        self._process_bar = ProcessBar()
        root_layout.addWidget(self._process_bar)

        # --- Wire signals ---
        self._game_selector.gameSelected.connect(self._on_game_selected)
        self._game_selector.gameAdded.connect(self._on_game_selected)
        self._game_selector.gameRemoved.connect(self._on_game_removed)
        self._layout_editor.configurationChanged.connect(self._on_configuration_changed)
        self._process_bar.launchRequested.connect(self._on_launch)
        self._process_bar.dryRunRequested.connect(self._on_dry_run)
        self._process_bar.stopRequested.connect(self._on_stop)
        self._launcher.instanceStarted.connect(self._process_bar.set_instance_started)
        self._launcher.instanceFinished.connect(self._process_bar.set_instance_finished)
        self._launcher.instanceError.connect(self._on_instance_error)
        self._launcher.allFinished.connect(self._on_all_finished)
        self._launcher.lobbyDetected.connect(self._lobby_panel.set_lobby_id)
        self._launcher.scriptMessage.connect(self._on_script_message)
        # Dynamic players: joiners get a status label; right-click → Remove.
        self._launcher.instanceAdded.connect(self._process_bar.add_instance)
        self._process_bar.removeRequested.connect(self._launcher.stop_instance)
        self._join_input = None     # GamepadInput watching for a held Start
        self._global_settings.actionRequested.connect(lambda key: self._run_action(key))
        # Handler hub: browse/install, and a startup check for newer handlers.
        self._hub_updates: dict = {}
        self._hubUpdates.connect(self._on_hub_updates)
        self._game_selector.hubRequested.connect(lambda: self._open_hub())
        self._global_settings.hubRequested.connect(
            lambda: self._open_hub(str(self._appid) if self._appid is not None else ''))
        self._check_hub_updates()
        self._actionProgress.connect(self._on_action_progress)
        self._actionFinished.connect(self._on_action_finished)
        self._setupMessage.connect(self._on_script_message)

        # Initialize process bar
        self._process_bar.set_instance_count(0)

    @staticmethod
    def _empty_script_info():
        return {'path': None, 'sheet': {}, 'specs': [], 'label': None,
                'error': None, 'conflict': None}

    def _load_handler_info(self, appid) -> dict:
        """Parse the game's handler script (if any) for its property sheet
        and option declarations. Never raises — a broken handler is reported
        through the settings indicator."""
        info = self._empty_script_info()
        path = scripting.find_handler(appid)
        if path is None:
            return info
        info['path'] = path
        info['label'] = str(path)
        info['conflict'] = scripting.handler_conflict(appid)
        try:
            hs = scripting.HandlerScript(path, appid)
            info['sheet'] = hs.sheet
            info['specs'] = hs.option_specs
            if hs.get('name'):
                info['label'] = f"{path.name} ({hs.get('name')})"
            if hs.non_steam:
                info['script'] = hs
        except scripting.HandlerError as e:
            info['error'] = str(e)
        return info

    # --- handler hub ----------------------------------------------------------

    def _check_hub_updates(self):
        """Ask the hub, on a thread, for newer versions of the handlers it
        installed; quietly nothing when it can't be reached. The cached list
        (hub.cached_games) answers at once."""
        import hub

        def updates(games):
            local = hub.installed()
            return {hub.game_key(g): g['latest_version'] for g in games
                    if hub.status(g, local) == 'update'}
        if hub.installed() and hub.cached_games():
            self._on_hub_updates(updates(hub.cached_games()))

        def work():
            try:
                if not hub.installed():
                    return
                found = updates(hub.list_games())
            except hub.HubError as e:
                print(f"[gui] hub update check: {e}", flush=True)
                return
            self._hubUpdates.emit(found)
        threading.Thread(target=work, name='janus-hub-updates', daemon=True).start()

    def _on_hub_updates(self, found):
        self._hub_updates = found
        self._game_selector.set_update_count(len(found))
        if self._appid is not None:
            self._global_settings.set_hub_update(found.get(self._appid))

    def _open_hub(self, search: str = ''):
        from .widgets.hub_dialog import HubDialog
        dlg = HubDialog(self, search=search)
        changed = []
        dlg.handlersChanged.connect(lambda: changed.append(True))
        dlg.exec()
        if changed:
            self._check_hub_updates()
            if self._appid is not None:     # its handler may have changed
                self._on_game_selected(self._appid, self._game_name)

    # --- non-Steam games: setup steps and actions ------------------------

    def _refresh_setup(self):
        """The Settings "Setup" box and the owner's name in the slot menus."""
        hs = self._setup_script
        if hs is None:
            self._global_settings.set_setup(None, [])
            SlotItem.owner_display = None
            self._owner = None
            return
        self._global_settings.set_setup(nonsteam.setup_status(hs),
                                        nonsteam.menu_actions(hs))
        own = nonsteam.owner(hs)
        self._owner = own
        SlotItem.owner_display = (own['name'], own['label']) if own else None

    def _run_action(self, key, then=None):
        """Run a handler action on a worker thread behind a progress dialog;
        then() once it has succeeded. With *then* (a setup step), a failure
        offers Try again, which runs then() too: it checks the setup again."""
        hs = self._setup_script
        if hs is None or self._progress is not None:
            return
        label = dict(hs.action_specs)[key]['label']
        self._progress = QProgressDialog(label + "…", None, 0, 0, self)
        self._progress.setWindowTitle(self._game_name or "Janus")
        self._progress.setWindowModality(Qt.WindowModal)
        self._progress.setMinimumDuration(0)
        self._progress.show()

        def work():
            try:
                self._actionFinished.emit('info', nonsteam.run_action(hs, key), then)
            except scripting.HandlerError as e:
                self._actionFinished.emit('error', str(e), then)
        threading.Thread(target=work, name='janus-action', daemon=True).start()

    def _on_action_progress(self, text):
        if self._progress is not None:
            self._progress.setLabelText(text)

    def _on_action_finished(self, level, text, then):
        if self._progress is not None:
            self._progress.close()
            self._progress = None
        self._refresh_setup()
        if level == 'error' and then:
            box = QMessageBox(QMessageBox.Critical, f"Set up {self._game_name}",
                              f"{self._game_name} isn't set up yet:\n\n{text}", parent=self)
            retry = box.addButton("Try again", QMessageBox.AcceptRole)
            box.addButton(QMessageBox.Cancel)
            box.exec()
            if box.clickedButton() is retry:
                then()
            return
        if text:
            box = QMessageBox.critical if level == 'error' else QMessageBox.information
            box(self, self._game_name or "Janus", text)
        if then:
            then()

    def _ensure_setup(self, then):
        """Walk the game's setup steps, asking before each fix; then() once
        all pass. Cancel stops."""
        st = nonsteam.setup_status(self._setup_script)
        if st['ready']:
            then()
            return
        self._refresh_setup()          # the Setup box may still say "Ready"
        box = QMessageBox(QMessageBox.Information, f"Set up {self._game_name}",
                          st['message'], parent=self)
        fix = box.addButton(st['actionLabel'], QMessageBox.AcceptRole) if st['action'] else None
        box.addButton(QMessageBox.Cancel)
        box.exec()
        if fix is not None and box.clickedButton() is fix:
            self._run_action(st['action'], then=lambda: self._ensure_setup(then))

    def _on_game_selected(self, appid, name: str):
        # Auto-save current game config before switching
        if self._appid is not None:
            self._presets.save_game(self._appid, self._build_config())

        self._appid = appid
        self._game_name = name
        self.setWindowTitle(f"Janus - {name} ({appid})")

        # Handler script drives the default state of every setting.
        self._script_info = self._load_handler_info(appid)
        self._setup_script = self._script_info.get('script')
        if self._setup_script is not None:
            self._setup_script.progress = self._actionProgress.emit
            self._setup_script.set_notify(self._setupMessage.emit,
                                          ui_thread=threading.get_ident())
        self._refresh_setup()
        self._layout_editor.set_side_by_side_first(
            bool(self._script_info['sheet'].get('sideBySideFirst')))
        self._global_settings.set_script_sheet(
            self._script_info['sheet'],
            label=self._script_info['label'],
            error=self._script_info['error'],
            warning=self._script_info['conflict'])
        self._global_settings.set_hub_update(self._hub_updates.get(appid))
        # Two handlers for one game: say which one is used (once per game
        # per session; the settings header keeps a persistent marker).
        conflict = self._script_info['conflict']
        if conflict and appid not in self._conflicts_shown:
            self._conflicts_shown.add(appid)
            QMessageBox.warning(self, "Two handlers", conflict)

        # Auto-load saved config for this game, or reset to defaults
        saved = self._presets.load_game(appid)
        if saved is None:
            # New game with a handler: start with zero overrides (all
            # settings follow the script); without one, plain defaults.
            saved = SessionConfig(appid=appid, game_name=name,
                                  overrides=set())
        saved.appid = appid
        saved.game_name = name
        self._global_settings.set_script_options(
            self._script_info['specs'], saved.script_options)
        self._load_config(saved)

    def _profile_info(self) -> dict:
        """{profile id: save line} for the selected game, for the slot
        menu's Profile list (save_info.py; the handler's Game.profileInfo)."""
        if self._appid is None:
            return {}
        cfg = self._build_config()
        sheet = self._script_info.get('sheet') or {}
        non_steam = self._setup_script is not None
        return save_info.profile_texts(
            self._appid, self._script_info.get('path'), cfg.script_options,
            profiles.load(),
            lambda p: save_info.saves_tag(p, cfg.isolation, bool(sheet.get('profileSaves')),
                                          non_steam),
            self._owner)

    def _on_game_removed(self):
        """Last game was removed -- clear state so we don't auto-save a deleted game."""
        self._appid = None
        self._game_name = None
        self.setWindowTitle("Janus Split Screen - Local Multiplayer Setup")

    def _on_configuration_changed(self):
        """Layout editor configuration changed -- update process bar count."""
        count = self._layout_editor.get_instance_count()
        self._process_bar.set_instance_count(count)

    def _build_config(self) -> SessionConfig:
        """Build SessionConfig from current UI state.

        Explicit user choices become the config's `overrides`; every other
        overridable field is then resolved to its effective value (handler
        sheet, else built-in) via apply_script_defaults — so the returned
        config's plain attributes are always the values to launch with."""
        instances = self._layout_editor.get_instances()
        layout = self._layout_editor.get_layout_state()
        ov = self._global_settings.get_overrides()
        cfg = SessionConfig(
            appid=self._appid,
            game_name=self._game_name,
            instances=instances,
            layout=layout,
            extra_env=self._global_settings.get_extra_env(),
            **self._global_settings.get_display(),
            input_mode=self._global_settings.get_input_mode(),
            overrides=set(ov),
            script_options=self._global_settings.get_option_values(),
            # Big Picture mode's section: not edited here, carried through.
            bigpicture=dict(self._bigpicture),
        )
        for k, v in ov.items():
            setattr(cfg, k, v)
        apply_script_defaults(cfg, self._script_info['sheet'])
        return cfg

    def _load_config(self, config: SessionConfig):
        """Apply a SessionConfig to the UI (set_script_sheet must have been
        called for the current game first — default states come from it)."""
        # Game
        if config.appid:
            self._appid = config.appid
            if config.game_name:
                self._game_name = config.game_name
            self._game_selector.select_appid(config.appid)
            if self._game_name:
                self.setWindowTitle(f"Janus - {self._game_name} ({self._appid})")

        self._bigpicture = dict(config.bigpicture)

        # Layout
        if config.layout:
            self._layout_editor.load_layout_state(config.layout)
        else:
            self._layout_editor.load_instances(config.instances)

        # Global settings: overrides -> explicit states, rest -> default
        self._global_settings.load_config(config)

        # Update process bar
        count = self._layout_editor.get_instance_count()
        self._process_bar.set_instance_count(count)

    def _handler_active(self) -> bool:
        return bool(self._script_info['path']
                    and not self._script_info['error'])

    def _sheet_owns(self, config: SessionConfig, field: str) -> bool:
        """True when the printed CLI should leave *field* to the handler:
        a handler is active and the user didn't override the field. The CLI
        reloads the handler and applies its sheet itself, so emitting the
        (already sheet-resolved) value too would apply it twice — e.g.
        Game.gameArgs ended up duplicated on the command line."""
        return (self._handler_active() and config.overrides is not None
                and field not in config.overrides)

    def _cli_caveats(self, config: SessionConfig,
                     isolation_default: str) -> list:
        """GUI choices the printed CLI can't reproduce while the handler is
        loaded: the CLI only lets a flag override the sheet when it differs
        from the argparse default (SCRIPTING.md §3), so an override back *to*
        the default is lost, and CLI game args are prepended with the
        sheet's rather than replacing them."""
        if not self._handler_active():
            return []
        sheet = self._script_info['sheet']
        checks = [
            ('isolation', 'isolation', isolation_default),
            ('verbose', 'verbose', False),
            ('nvidia', 'nvidia', False),
            ('copy_prefix', 'copyPrefix', False),
            ('copy_userdata', 'copyUserdata', False),
            ('steam_input_filter', 'steamInputFilter', False),
            ('goldberg_enabled', 'goldberg', False),
        ]
        if config.goldberg_enabled:
            checks += [
                ('goldberg_no_overlay', 'goldbergNoOverlay', True),
                ('goldberg_disable_lan', 'goldbergDisableLan', True),
                ('goldberg_debug', 'goldbergDebug', False),
            ]
        out = []
        for field, key, cli_default in checks:
            val = getattr(config, field)
            if (key in sheet and val == cli_default
                    and sheet[key] != cli_default):
                out.append(f'{field}={val} (CLI will use the handler\'s '
                           f'{sheet[key]})')
        if (config.overrides is not None and 'game_args' in config.overrides
                and sheet.get('gameArgs')):
            out.append('game args override (CLI prepends the handler\'s '
                       f'gameArgs "{sheet["gameArgs"]}" instead of replacing)')
        if (config.overrides is not None
                and 'extra_gamescope_args' in config.overrides
                and sheet.get('extraGamescopeArgs')):
            out.append('extra gamescope args override (CLI also appends the '
                       f'handler\'s "{sheet["extraGamescopeArgs"]}")')
        return out

    def _env_prefix(self, config: SessionConfig) -> str:
        """Shared env-var prefix (hook toggles + extra env). Applies to
        both the per-instance `steam` and Wayland `steam-split` forms."""
        env_parts = {}
        if config.steam_input_filter:
            env_parts['JANUS_STEAM_INPUT_FILTER'] = '1'
        env_parts.update(config.extra_env)
        if not env_parts:
            return ''
        return ' '.join(f'{k}={v}' for k, v in env_parts.items()) + ' '

    def _build_cli_commands(self, config: SessionConfig) -> str:
        """Build the equivalent CLI for the current config: the GUI runs a
        single `steam-split` with one --tile per instance (KWin Wayland "Path
        A": gamescope --backend sdl @ scale 1.0 + KWin window rules)."""
        parts = ['python3 src/host/main.py steam-split', str(config.appid)]
        for i, inst in enumerate(config.instances):
            c = inst.controller_index if inst.controller_index is not None else i
            tile = (f'--tile {c}:{inst.pos_x},{inst.pos_y},'
                    f'{inst.gamescope_width},{inst.gamescope_height}')
            if inst.profile_id:
                tile += '@' + shlex.quote(inst.profile_id)
            parts.append(tile)

        # Always explicit: steam-split defaults to 'secondary', so omitting
        # 'none' (as the X11 form does) would print the wrong isolation.
        parts.append(f'--isolation {config.isolation}')
        if config.verbose:
            parts.append('-v')
        if config.nvidia:
            parts.append('--nvidia')
        if config.copy_prefix:
            parts.append('--copy-prefix')
        if config.copy_userdata:
            parts.append('--copy-userdata')
        if config.goldberg_enabled:
            parts.append('--goldberg')
            if config.goldberg_no_overlay:
                parts.append('--goldberg-no-overlay')
            if not config.goldberg_disable_lan:
                parts.append('--goldberg-lan')
            if config.goldberg_debug:
                parts.append('--goldberg-debug')
        if config.exe_name:
            parts.append(f'--exe {config.exe_name}')
        if config.launch_command:
            # Shared across tiles like game_args below — render the template.
            parts.append(f'--launch-cmd {shlex.quote(config.launch_command)}')
        if config.render_scale < 100:
            parts.append(f'--render-scale {config.render_scale}')
            parts.append(f'--upscale-filter {config.upscale_filter}')
            if config.upscale_filter in ('fsr', 'nis'):
                parts.append(f'--sharpness {config.upscale_sharpness}')
        if config.fps_limit != 60:
            parts.append(f'--fps-limit {config.fps_limit}')
        if config.game_args and not self._sheet_owns(config, 'game_args'):
            # game_args are shared across tiles here (per-instance %TAG%
            # expansion can't be shown in one command), so render the template.
            parts.append(f'-- {config.game_args}')

        lines = [
            '# KWin Wayland "Path A": one steam-split command, one --tile per',
            '# instance (gamescope --backend sdl @ scale 1.0 + KWin window rules).',
        ]

        # Extras the GUI applies on Wayland that steam-split has no CLI flag for.
        notes = []
        if config.grab:
            notes.append('gamescope --grab (keyboard grab)')
        if config.force_grab_cursor:
            notes.append('gamescope --force-grab-cursor')
        if (config.extra_gamescope_args
                and not self._sheet_owns(config, 'extra_gamescope_args')):
            notes.append(f'extra gamescope args ({config.extra_gamescope_args})')
        kw = []
        if not config.kwin_keep_above:
            kw.append('keep-above off')
        if not config.kwin_accept_focus:
            kw.append('accept-focus off')
        if not config.kwin_disable_fsp:
            kw.append('focus-stealing-prevention left on')
        if kw:
            notes.append('KWin rule overrides: ' + ', '.join(kw))
        if config.goldberg_enabled and config.goldberg_auto_join:
            notes.append('goldberg auto-join')
        if config.osk_enabled:
            notes.append('on-screen keyboard daemon')
        if notes:
            lines.append('# GUI also applies (no steam-split CLI flag): '
                         + '; '.join(notes))
        caveats = self._cli_caveats(config, isolation_default='secondary')
        if caveats:
            lines.append('# NOT reproduced by this CLI (handler loaded): '
                         + '; '.join(caveats))

        lines.append(self._env_prefix(config) + ' \\\n    '.join(parts))
        lines.append('')
        return '\n'.join(lines)

    def _on_dry_run(self):
        if not self._appid:
            QMessageBox.warning(self, "No Game", "Select a game first.")
            return

        config = self._build_config()
        if not config.instances:
            QMessageBox.warning(self, "No Instances", "Place controllers on monitor slots first.")
            return

        text = self._build_cli_commands(config)

        dlg = QDialog(self)
        dlg.setWindowTitle("Dry Run - CLI Commands")
        dlg.resize(700, 400)
        layout = QVBoxLayout(dlg)
        edit = QTextEdit()
        edit.setReadOnly(True)
        edit.setFontFamily("monospace")
        edit.setPlainText(text)
        layout.addWidget(edit)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok)
        buttons.accepted.connect(dlg.accept)
        layout.addWidget(buttons)
        dlg.exec()

    def _check_session_warn(self) -> bool:
        """Check the host session; return True to proceed with launch.

        Only KWin Wayland (Path A) is supported: anything else aborts.
        """
        import wayland_session as ws
        kind = ws.session_kind()
        if kind == 'kwin-wayland':
            missing = ws.missing_tools()
            if missing:
                QMessageBox.warning(
                    self, "Missing tools",
                    "These tools are required for Wayland split-screen but "
                    f"weren't found on PATH:\n\n  {', '.join(missing)}\n\n"
                    "Install them and try again.")
                return False
            return True
        QMessageBox.critical(
            self, "Unsupported session",
            "Janus requires KDE Plasma (KWin) on Wayland: the game tiles are "
            "positioned by KWin window rules.\n\n"
            + ("This is a Wayland session but KWin was not detected."
               if kind == 'wayland-other' else "This is an X11 session.")
            + "\n\nLaunch aborted.")
        return False

    def _check_steam_input_warn(self, config) -> bool:
        """Warn if the Steam client isn't running.

        Steam Input needs the running Steam client — it grabs the pads (hidraw) and
        creates/routes the per-player virtual controllers. Without it, controllers may not
        work or both players share one pad. This applies even with Goldberg (which only
        replaces networking/auth, not controller input). Returns True to proceed with launch.
        """
        if prereqs.steam_running():
            return True
        reply = QMessageBox.warning(
            self, "Steam not running", prereqs.STEAM_NOT_RUNNING + "\n\nLaunch anyway?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        return reply == QMessageBox.Yes

    def _on_launch(self):
        if not self._appid:
            QMessageBox.warning(self, "No Game", "Select a game first.")
            return

        if not self._script_info['path']:
            QMessageBox.warning(self, "No handler", scripting.no_handler_message(self._appid))
            return

        if self._setup_script is not None and \
                not nonsteam.setup_status(self._setup_script)['ready']:
            self._ensure_setup(self._on_launch)    # set up, then launch again
            return

        config = self._build_config()

        if not config.instances:
            QMessageBox.warning(self, "No Instances", "Place controllers on monitor slots first.")
            return

        # Validate controllers assigned
        unassigned = [i+1 for i, inst in enumerate(config.instances) if inst.controller_index is None]
        if unassigned:
            reply = QMessageBox.question(
                self, "Missing Controllers",
                f"Instance(s) {unassigned} have no controller assigned. Launch anyway?",
            )
            if reply != QMessageBox.Yes:
                return

        # Warn about non-KWin-Wayland sessions before doing anything.
        if not self._check_session_warn():
            return

        problem = prereqs.gamescope_problem()
        if problem:
            QMessageBox.critical(self, "gamescope is needed", problem)
            return

        # Warn if Steam Input needs the Steam client but it isn't running.
        if not self._check_steam_input_warn(config):
            return

        # Auto-save config for this game
        self._presets.save_game(config.appid, config)

        self._process_bar.set_running(True)
        # Reset & show the lobby panel rows
        self._lobby_panel.set_instances(
            len(config.instances),
            debug_active=config.goldberg_enabled and config.goldberg_debug,
        )
        # Fresh HandlerScript for this session (hooks + Janus.* API). The
        # sheet's *settings* are already folded into config; the script object
        # is what fires play()/onLobbyDetected()/... during the session.
        script = None
        if self._script_info['path'] and not self._script_info['error']:
            try:
                script = scripting.HandlerScript(
                    self._script_info['path'], config.appid,
                    option_values=config.script_options,
                    verbose=config.verbose)
            except scripting.HandlerError as e:
                QMessageBox.critical(
                    self, "Handler script error",
                    f"The handler script failed to load:\n\n{e}\n\n"
                    "Launch aborted. Fix the script, or get a working one "
                    "from the handler hub.")
                self._process_bar.set_running(False)
                return

        if not (self._check_build(config, script)
                and self._check_first_run(config, script)
                and self._check_input_mode(config, script)
                and self._check_gamepad_layout_warn(config, script)
                and self._check_runtime_warn(config, script)):
            if script is not None:
                script.finish()
            self._process_bar.set_running(False)
            self._lobby_panel.clear_all()
            return

        # What the players must know before the game starts (the handler's
        # Game.launchMessage): launch only once it has been read.
        message = scripting.launch_message(script)
        if message and not self._confirm_launch_message(config, message):
            script.finish()
            self._process_bar.set_running(False)
            self._lobby_panel.clear_all()
            return

        try:
            self._launcher.launch_config(config, script=script)
        except ValueError as e:     # e.g. one profile on two slots
            QMessageBox.warning(self, "Can't launch", str(e))
            self._process_bar.set_running(False)
            return
        self._start_join_watch()

    # --- dynamic players: a free pad holding Start joins ------------------

    def _start_join_watch(self):
        """While the session allows joining (Janus input, Game.lateJoin), a
        free controller holding Start for 1.5 s joins it (the same gesture
        as in Big Picture, focus or not)."""
        self._stop_join_watch()
        if self._launcher.join_problem():
            return
        try:
            from src.bigpicture.gamepad import GamepadInput
            self._join_input = GamepadInput(self)
        except Exception as e:      # no SDL2: no joining, nothing else lost
            print(f'[gui] join watch unavailable: {e}', flush=True)
            return
        self._join_input.enabled = False      # only the held-Start signal
        self._join_input.startHeld.connect(self._on_join_request)

    def _stop_join_watch(self):
        if self._join_input is not None:
            self._join_input.shutdown()
            self._join_input.deleteLater()
            self._join_input = None

    def _on_join_request(self, js):
        if not self._launcher.is_running():
            return
        if any(c.controller_index == js for c in self._launcher.active_instances()):
            return                  # already playing
        problem = self._launcher.join_problem()
        if problem:
            print(f'[gui] join refused: {problem}', flush=True)
            return
        try:
            self._launcher.join_pad(js)
        except ValueError as e:
            QMessageBox.warning(self, "Couldn't join", str(e))

    def _check_input_mode(self, config, script) -> bool:
        """Settle how the controllers reach the games (controller_input.py) for this
        launch: the game's Steam Input setting must suit the mode (Check
        again / play with the other mode this time / Cancel), then per-pad
        advice (Launch anyway / Cancel). Sets config.input_mode to the mode
        used. True to proceed."""
        mode = input_mode.resolve(script, config.input_mode or None)
        while True:
            try:
                steam_input.check(config.appid, script, config.game_name or None, mode)
                break
            except steam_input.SteamInputNotEnabled as e:
                box = QMessageBox(QMessageBox.Warning, "Steam Input setting", str(e),
                                  parent=self)
                box.setTextFormat(Qt.PlainText)
                again = box.addButton("Check again", QMessageBox.AcceptRole)
                other = (box.addButton(f"Play with {input_mode.LABELS[e.alternative]}",
                                       QMessageBox.ActionRole)
                         if e.alternative else None)
                box.addButton(QMessageBox.Cancel)
                box.setDefaultButton(again)
                box.exec()
                if box.clickedButton() is other:
                    mode = e.alternative
                elif box.clickedButton() is not again:
                    return False
        config.input_mode = mode
        warnings = input_mode.pad_warnings(mode, session_pads(config.instances))
        if not warnings:
            return True
        box = QMessageBox(QMessageBox.Warning, "Controllers",
                          '\n\n'.join(warnings), parent=self)
        box.setTextFormat(Qt.PlainText)
        play = box.addButton("Launch anyway", QMessageBox.AcceptRole)
        cancel = box.addButton(QMessageBox.Cancel)
        box.setDefaultButton(cancel)
        box.exec()
        return box.clickedButton() is play

    def _check_gamepad_layout_warn(self, config, script) -> bool:
        """Warn when a placed pad has no Gamepad layout for a game that needs
        one (Game.requireGamepadLayout). True to proceed. Steam Input only."""
        if config.input_mode == input_mode.JANUS:
            return True
        warning = steam_input.gamepad_layout_warning(
            config.appid, script, session_pads(config.instances),
            config.game_name or None)
        if not warning:
            return True
        box = QMessageBox(QMessageBox.Warning, "Controller layout", warning, parent=self)
        box.setTextFormat(Qt.PlainText)
        play = box.addButton("Launch anyway", QMessageBox.AcceptRole)
        cancel = box.addButton(QMessageBox.Cancel)
        box.setDefaultButton(cancel)
        box.exec()
        return box.clickedButton() is play

    def _check_build(self, config, script) -> bool:
        """Refuse when Steam installed the game's other build (Linux vs
        Windows) than the handler is written for: the launch would only say
        "executable not found". True to proceed."""
        exe, cmd = config.exe_name or None, config.launch_command or None
        if prereqs.expected_build(script, exe, cmd) is None:   # also non-Steam games
            return True
        try:
            game = SteamGame(config.appid)
        except FileNotFoundError:
            return True     # the launch itself reports it
        problem = prereqs.build_problem(game, script, exe, cmd)
        if not problem:
            return True
        box = QMessageBox(QMessageBox.Critical, "Wrong build installed",
                          problem, parent=self)
        box.setTextFormat(Qt.PlainText)
        box.exec()
        return False

    def _check_first_run(self, config, script) -> bool:
        """A Proton game Steam never started (no prefix yet): start it once
        through Steam (first_run.py), with a progress box. True to proceed."""
        import first_run
        try:
            game = SteamGame(config.appid)
        except FileNotFoundError:
            return True
        if not first_run.needed(game, script):
            return True
        from PySide6.QtWidgets import QProgressDialog, QApplication
        dlg = QProgressDialog(first_run.message(game), None, 0, 0, self)
        dlg.setWindowTitle("First start through Steam")
        dlg.setMinimumDuration(0)
        dlg.setWindowModality(Qt.WindowModal)
        dlg.show()
        result, texts = {}, []

        def work():
            result['err'] = first_run.run(game, progress=texts.append)
        t = threading.Thread(target=work, daemon=True, name='first-run')
        t.start()
        while t.is_alive():
            if texts:
                dlg.setLabelText(first_run.message(game) + "\n\n" + texts[-1])
            QApplication.processEvents()
            t.join(0.1)
        dlg.close()
        if result.get('err'):
            QMessageBox.warning(self, "First start through Steam", result['err'])
            return False
        return True

    def _check_runtime_warn(self, config, script) -> bool:
        """Warn when the game's Steam Linux Runtime isn't installed (it still
        launches, outside it or in another one). True to proceed."""
        try:
            game = SteamGame(config.appid, launch_command=config.launch_command or None)
        except FileNotFoundError:
            return True     # the launch itself reports it
        warning = prereqs.runtime_warning(game, script)
        if not warning:
            return True
        box = QMessageBox(QMessageBox.Warning, "Steam Linux Runtime missing",
                          warning, parent=self)
        box.setTextFormat(Qt.PlainText)
        play = box.addButton("Launch anyway", QMessageBox.AcceptRole)
        box.addButton(QMessageBox.Cancel)
        box.setDefaultButton(play)
        box.exec()
        return box.clickedButton() is play

    def _confirm_launch_message(self, config, message) -> bool:
        box = QMessageBox(QMessageBox.Information,
                          f"Before you play {config.game_name or config.appid}",
                          message, parent=self)
        box.setTextFormat(Qt.PlainText)
        play = box.addButton("Play", QMessageBox.AcceptRole)
        box.addButton(QMessageBox.Cancel)
        box.setDefaultButton(play)
        box.exec()
        return box.clickedButton() is play

    def _on_stop(self):
        self._stop_join_watch()
        self._launcher.stop_all()
        self._process_bar.set_running(False)
        self._lobby_panel.clear_all()

    def _on_instance_error(self, index, msg):
        self._process_bar.set_instance_error(index, msg)

    def _on_script_message(self, level, text, done):
        # Modal: the hook that raised it is blocked until done() — the
        # launch resumes only once the user has closed the box.
        box = QMessageBox(
            QMessageBox.Critical if level == 'error' else QMessageBox.Warning,
            "Handler script " + level, text, QMessageBox.Ok, self)
        try:
            box.exec()
        finally:
            done()

    def _on_all_finished(self):
        self._stop_join_watch()
        self._process_bar.set_running(False)

    def closeEvent(self, event):
        # Auto-save current game config
        if self._appid is not None:
            self._presets.save_game(self._appid, self._build_config())

        if self._launcher.is_running():
            reply = QMessageBox.question(
                self, "Games Running",
                "Games are still running. Stop all and quit?",
            )
            if reply != QMessageBox.Yes:
                event.ignore()
                return
            self._launcher.stop_all()
        event.accept()

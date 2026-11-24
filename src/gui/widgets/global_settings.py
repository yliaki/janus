"""Global session settings widget.

Handler-script integration ("script is default, GUI stores overrides"):
every session-level setting is tri-state — by default it shows the value the
game's handler script (or the built-in default) provides, and only an
explicit user choice is recorded as an override in the per-game JSON
(`override_keys`). See SCRIPTING.md.

Widget semantics:
- Checkboxes are tristate: PartiallyChecked = "default (from handler)",
  Checked/Unchecked = explicit override.
- Combos get a leading "default (...)" item showing the resolved value.
- Line edits: empty = default (the handler value is shown as placeholder);
  non-empty = override. (Overriding *to* empty isn't expressible — use a
  user-local handler for that.)
- The host-instance spinbox is an override only when it differs from the
  default (handler sheet, else 1). Extra env is always explicit.
"""

import sys
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QFormLayout, QComboBox, QCheckBox, QLineEdit, QSpinBox,
    QHBoxLayout, QLabel, QPushButton, QTabWidget, QVBoxLayout, QScrollArea,
    QFrame, QSlider, QGroupBox,
)

sys.path.insert(0, str(Path(__file__).parent.parent))
from models.config import (  # noqa: E402
    SHEET_FIELD_MAP, session_field_default,
)
import about  # noqa: E402

# snake_case field -> camelCase sheet key
_FIELD_TO_SHEET = {v: k for k, v in SHEET_FIELD_MAP.items()}


class GlobalSettings(QWidget):
    """Session settings with handler-script defaults + tri-state overrides.

    Two tabs: "Game" (shown by default) holds only the options the handler
    script declares itself (Janus.addOption); "All settings" holds every
    session setting with its default/override state.
    """

    changed = Signal()
    # A non-Steam game's action button (setup fix or maintenance): its key.
    actionRequested = Signal(str)
    hubRequested = Signal()         # the handler header's hub button

    _GAME_TAB, _ALL_TAB = 0, 1

    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self._tabs = QTabWidget()

        # --- "Game" tab: handler-declared options (Janus.addOption) ---
        game_tab = QWidget()
        game_l = QVBoxLayout(game_tab)
        # Non-Steam games (nonsteam.py): setup state, its fix, maintenance.
        self._setup_box = QGroupBox("Setup")
        setup_l = QVBoxLayout(self._setup_box)
        self._setup_label = QLabel()
        self._setup_label.setWordWrap(True)
        setup_l.addWidget(self._setup_label)
        self._setup_buttons = QHBoxLayout()
        setup_l.addLayout(self._setup_buttons)
        self._setup_box.setVisible(False)
        game_l.addWidget(self._setup_box)
        game_l.addLayout(self._build_input_form())
        game_l.addLayout(self._build_display_form())
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        game_l.addWidget(line)
        self._options_form = QFormLayout()
        game_l.addLayout(self._options_form)
        self._no_options_label = QLabel(
            "This game has no game-specific options.\n"
            "See \"All settings\" for the full list of launch settings.")
        self._no_options_label.setStyleSheet("color: gray;")
        self._no_options_label.setWordWrap(True)
        game_l.addWidget(self._no_options_label)
        game_l.addStretch()
        self._opt_widgets = {}   # key -> (spec, widget)
        self._tabs.addTab(game_tab, "Game")

        # --- "All settings" tab: every session setting ---
        # Scrolled, so the long list doesn't set the panel's minimum height
        # (a QTabWidget sizes to its largest page).
        all_tab = QWidget()
        layout = QFormLayout(all_tab)
        self._layout = layout
        scroll = QScrollArea()
        scroll.setWidget(all_tab)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._tabs.addTab(scroll, "All settings")
        self._tabs.addTab(self._build_about_tab(), "About")

        self._sheet = {}          # handler property sheet (camelCase keys)
        self._checks = {}         # field -> QCheckBox (tristate)
        self._combos = {}         # field -> QComboBox (item 0 = default)
        self._lines = {}          # field -> QLineEdit (empty = default)
        self._loading = False     # guard: suppress changed during load

        # --- handler indicator + reset ---
        hdr = QWidget()
        hdr_l = QHBoxLayout(hdr)
        hdr_l.setContentsMargins(0, 0, 0, 0)
        self._handler_label = QLabel("Handler: none")
        self._handler_label.setStyleSheet("color: gray;")
        hdr_l.addWidget(self._handler_label, stretch=1)
        self._hub_btn = QPushButton("Handler hub…")
        self._hub_btn.setToolTip("Find this game's handler on the handler hub")
        self._hub_btn.clicked.connect(self.hubRequested)
        hdr_l.addWidget(self._hub_btn)
        self._reset_btn = QPushButton("Use handler defaults")
        self._reset_btn.setToolTip(
            "Clear every override so all settings follow the game's handler "
            "script (handlers/<game-name>.js) again.")
        self._reset_btn.clicked.connect(self.reset_to_defaults)
        self._reset_btn.setEnabled(False)
        hdr_l.addWidget(self._reset_btn)
        # Above the tabs: the handler and the reset apply to both.
        root.addWidget(hdr)
        root.addWidget(self._tabs)
        self.changed.connect(self._refresh_override_count)

        # --- combos ---
        self._isolation = self._add_combo(
            'isolation', "Isolation:",
            ['none', 'prefix', 'secondary', 'full'],
            "none: shared prefix+home. prefix: per-instance Wine prefix, SHARED "
            "home — keeps Steam Input / controller menus working (recommended for "
            "local co-op). secondary: everyone but your Steam account's profile gets "
            "an isolated home+prefix (per player profile). full: "
            "all isolated. 'secondary'/'full' unshare $HOME, which hides Steam's "
            "config and breaks the controller UI.")

        # --- tristate checkboxes ---
        def chk(fieldname, label, tooltip=None):
            w = QCheckBox(label)
            w.setTristate(True)
            w.setCheckState(Qt.PartiallyChecked)
            if tooltip:
                w.setToolTip(tooltip)
            w.checkStateChanged.connect(self._on_changed)
            layout.addRow(w)
            self._checks[fieldname] = w
            return w

        self._kwin_keep_above = chk(
            'kwin_keep_above', "Keep above (always on top) [Wayland]",
            "Force the game tiles always-on-top via the KWin 'above' rule. Turn "
            "OFF if a tile won't take keyboard focus while kept above.")
        self._kwin_accept_focus = chk(
            'kwin_accept_focus', "Accept keyboard focus [Wayland]",
            "Force the KWin 'accept focus' rule so the tile is allowed to "
            "receive keyboard focus at all.")
        self._kwin_disable_fsp = chk(
            'kwin_disable_fsp', "Disable focus-stealing prevention [Wayland]",
            "Force focus-stealing prevention to None (fsplevel=0) so a tile can "
            "activate and grab the keyboard when it appears, instead of the keys "
            "staying on the terminal/desktop. Usual fix for a game window that "
            "won't receive keyboard input on KWin Wayland.")
        self._borderless = chk('borderless', "Borderless (-b)")
        self._nvidia = chk('nvidia', "NVIDIA PRIME")
        self._verbose = chk('verbose', "Verbose logging")
        self._copy_prefix = chk('copy_prefix', "Full-copy Wine prefix")
        self._copy_userdata = chk('copy_userdata', "Copy user data (saves)")
        self._force_grab_cursor = chk(
            'force_grab_cursor', "Force grab cursor (--force-grab-cursor)")
        self._grab = chk('grab', "Grab keyboard (--grab)")
        self._steam_input_filter = chk(
            'steam_input_filter',
            "Filter Steam Input per-instance (keep Steam Input on)",
            "Keep Steam Input active and filter each instance to its assigned pad "
            "(vtable filter), instead of disabling Steam Input. Enable for controllers "
            "that only work through Steam Input (e.g. generic adapters with no SDL mapping).")
        self._osk_enabled = chk(
            'osk_enabled', "On-screen keyboard (L3+R3 to summon)",
            "Spawns the OSK daemon alongside the session. Press L3+R3 on a "
            "controller to summon a gamepad-driven keyboard over its instance. "
            "The pad is grabbed while the keyboard is open so the game stops "
            "seeing its input.")
        self._hide_panel = chk('hide_panel', "Hide KDE panel during session")
        self._goldberg_enabled = chk(
            'goldberg_enabled', "Use Goldberg Emulator (LAN multiplayer)")
        self._goldberg_no_overlay = chk(
            'goldberg_no_overlay', "Disable Goldberg overlay")
        self._goldberg_disable_lan = chk(
            'goldberg_disable_lan', "Disable Goldberg LAN auto-discovery",
            "Stops gbe_fork from advertising the game on UDP broadcast.\n"
            "Required for Lethal Company and other Unity Netcode games where\n"
            "two same-host instances would otherwise auto-connect via LAN and\n"
            "deadlock on a divergent handshake. With this on, players can still\n"
            "manually create/join lobbies via the in-game online menu.")
        self._goldberg_debug = chk(
            'goldberg_debug', "Use debug gbe_fork build (enables lobby ID detection)",
            "Use the debug build of gbe_fork from vendor/goldberg/debug/.\n"
            "Only the debug build emits STEAM_LOG_*.log — required so Janus can\n"
            "detect the host's lobby ID for the manual Copy button and auto-join.\n"
            "The regular build has logging compiled out (EMU_RELEASE_BUILD).\n"
            "Debug is larger and slower; download the matching debug archive from\n"
            "https://github.com/Detanup01/gbe_fork/releases and place files in\n"
            "vendor/goldberg/debug/.")

        # Auto-join row: checkbox + host instance picker on a single line
        autojoin_row = QWidget()
        autojoin_layout = QHBoxLayout(autojoin_row)
        autojoin_layout.setContentsMargins(0, 0, 0, 0)
        self._goldberg_auto_join = QCheckBox("Auto-join host lobby")
        self._goldberg_auto_join.setTristate(True)
        self._goldberg_auto_join.setCheckState(Qt.PartiallyChecked)
        self._goldberg_auto_join.setToolTip(
            "Launch the host instance first, watch its gbe_fork log for a lobby ID,\n"
            "then launch the other instances with '+connect_lobby <id>' so they\n"
            "auto-join. Requires the debug gbe_fork build (logs the lobby ID).\n"
            "Without auto-join: all instances launch simultaneously and lobby IDs\n"
            "are still shown in the panel below for manual Copy/Paste.")
        self._goldberg_auto_join.checkStateChanged.connect(self._on_changed)
        self._checks['goldberg_auto_join'] = self._goldberg_auto_join
        autojoin_layout.addWidget(self._goldberg_auto_join)
        autojoin_layout.addStretch()
        autojoin_layout.addWidget(QLabel("Host instance:"))
        self._goldberg_host_instance = QSpinBox()
        self._goldberg_host_instance.setRange(1, 8)
        self._goldberg_host_instance.setValue(1)
        self._goldberg_host_instance.setToolTip(
            "Which instance (1-based) will host the lobby. Janus launches this\n"
            "one first under auto-join; the others wait until its lobby ID is\n"
            "detected, then launch with '+connect_lobby <id>'.")
        self._goldberg_host_instance.valueChanged.connect(self._on_changed)
        autojoin_layout.addWidget(self._goldberg_host_instance)
        layout.addRow(autojoin_row)

        # --- line edits (empty = handler default) ---
        self._exe_name = self._add_line(
            'exe_name', "Executable:", "Borderlands2.exe",
            "Filename of the executable to launch. Searched for in the game's\n"
            "install dir (recursively). Leave empty to use the handler's value\n"
            "(or auto-detect). Ignored when a Launch command is set.")
        self._launch_command = self._add_line(
            'launch_command', "Launch command:",
            "./jre/bin/java -Xmx4G -jar Necesse.jar",
            "Full command to start the game, relative to its Steam install dir.\n"
            "Overrides 'Executable:' and disables all executable auto-detection\n"
            "(including the auto-detected game args). Leave empty to use the\n"
            "handler's value. Supports the same %TAG% placeholders as Game args.")
        self._extra_gamescope_args = self._add_line(
            'extra_gamescope_args', "Extra gamescope args:",
            "--filter linear --fsr-sharpness 5", None)
        self._game_args = self._add_line(
            'game_args', "Game args:", "-w %WIDTH% -h %HEIGHT%", None)

        # Extra env: always explicit (merged over handler extraEnv at launch)
        self._extra_env = QLineEdit()
        self._extra_env.setPlaceholderText("KEY=VAL KEY2=VAL2")
        self._extra_env.textChanged.connect(self._on_changed)
        layout.addRow("Extra env:", self._extra_env)

        # The OSK is X11-only (kept for a future Wayland port).
        self._osk_enabled.setEnabled(False)
        self._osk_enabled.setToolTip(
            "Not available yet: the OSK renders as an X11 override-redirect "
            "surface positioned over the gamescope window via xdotool, but "
            "native Wayland gamescope windows are invisible to X11 tooling. "
            "See WAYLAND.md.")

    # ------------------------------------------------------------------ build


    @staticmethod
    def _build_about_tab():
        """Version, author and Ko-fi (about.py); links open in the browser."""
        tab = QWidget()
        lay = QVBoxLayout(tab)
        title = QLabel("Janus Split Screen")
        font = title.font()
        font.setPointSizeF(font.pointSizeF() * 1.6)
        font.setBold(True)
        title.setFont(font)
        lay.addWidget(title)
        ver = QLabel(f"Version {about.version_text()}")
        ver.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(ver)
        made = QLabel(f'Made by <a href="{about.AUTHOR_URL}">{about.AUTHOR}</a>')
        made.setOpenExternalLinks(True)
        lay.addWidget(made)
        if about.KOFI_URL:
            kofi = QLabel(f'Support Janus on <a href="{about.KOFI_URL}">Ko-fi</a>')
            kofi.setOpenExternalLinks(True)
        else:
            kofi = QLabel("Support Janus on Ko-fi: coming soon")
            kofi.setStyleSheet("color: gray;")
        lay.addWidget(kofi)
        lay.addStretch()
        return tab
    def _build_input_form(self):
        """Controller input mode (src/host/controller_input.py). A plain per-game
        setting like the render scale; the handler only limits the choice
        (Game.janusInput / Game.steamInput)."""
        form = QFormLayout()
        self._input_mode = QComboBox()
        self._input_mode.setToolTip(
            "Janus input: Janus turns each controller into its own virtual Xbox\n"
            "controller and gives every game only its own one. Needs \"Disable\n"
            "Steam Input\" for the game in Steam. Robust: pads that reconnect\n"
            "keep their player, identical pads don't get mixed up.\n\n"
            "Steam Input: Steam remaps the controllers (trackpads, gyro, Steam\n"
            "layouts). Needs \"Enable Steam Input\" for the game in Steam.")
        self._input_mode.currentIndexChanged.connect(self._on_changed)
        form.addRow("Controller input:", self._input_mode)
        self._fill_input_modes()
        return form

    def _fill_input_modes(self, current=None):
        import controller_input as im
        if current is None:
            current = self.get_input_mode() if self._input_mode.count() else ''
        modes = im.supported(getattr(self, '_sheet', None) or None)
        was = getattr(self, '_loading', False)
        self._loading = True
        try:
            self._input_mode.clear()
            self._input_mode.addItem(f"Default ({im.LABELS[modes[0]]})", '')
            for m in modes:
                self._input_mode.addItem(im.LABELS[m], m)
            idx = self._input_mode.findData(current)
            self._input_mode.setCurrentIndex(max(0, idx))
            self._input_mode.setEnabled(len(modes) > 1)
        finally:
            self._loading = was

    def get_input_mode(self) -> str:
        """'' (default), 'janus' or 'steam', for SessionConfig.input_mode."""
        return self._input_mode.currentData() or ''

    def _build_display_form(self):
        """Render scale + upscale filter. Plain per-game settings (not
        handler-overridable), always stored explicitly like Extra env."""
        form = QFormLayout()
        row = QHBoxLayout()
        self._render_scale = QSlider(Qt.Horizontal)
        self._render_scale.setRange(25, 100)
        self._render_scale.setSingleStep(5)
        self._render_scale.setPageStep(25)
        self._render_scale.setTickInterval(25)
        self._render_scale.setTickPosition(QSlider.TicksBelow)
        self._render_scale.setValue(100)
        self._render_scale.setToolTip(
            "The game renders at this % of its tile; gamescope upscales it to\n"
            "fill the tile. Lower = faster. Games whose UI is sized in pixels\n"
            "(many pixel-art / older games) also get a BIGGER UI; games whose\n"
            "UI scales with the resolution only get softer — use their own UI\n"
            "scale setting instead.")
        self._render_scale_label = QLabel()
        self._render_scale_label.setMinimumWidth(40)
        row.addWidget(self._render_scale, stretch=1)
        row.addWidget(self._render_scale_label)
        form.addRow("Render scale:", row)

        frow = QHBoxLayout()
        self._upscale_filter = QComboBox()
        self._upscale_filter.addItems(
            ['linear', 'fsr', 'nis', 'nearest', 'pixel'])
        self._upscale_filter.setToolTip(
            "How gamescope upscales below 100%.\n"
            "linear: soft, safe.\n"
            "fsr / nis: sharpened upscaling for 3D games.\n"
            "nearest / pixel: crisp pixels for pixel-art games\n"
            "(pick 50% for exact 2x pixels).")
        self._sharpness = QSpinBox()
        self._sharpness.setRange(0, 20)
        self._sharpness.setValue(2)
        self._sharpness.setPrefix("sharpness ")
        self._sharpness.setToolTip("fsr / nis sharpness: 0 = sharpest, 20 = softest.")
        frow.addWidget(self._upscale_filter, stretch=1)
        frow.addWidget(self._sharpness)
        form.addRow("Upscale filter:", frow)

        self._fps_limit = QSpinBox()
        self._fps_limit.setRange(0, 500)
        self._fps_limit.setSingleStep(5)
        self._fps_limit.setValue(60)
        self._fps_limit.setSuffix(" fps")
        self._fps_limit.setSpecialValueText("Unlimited")
        self._fps_limit.setToolTip(
            "Frame limit for each game instance (0 = unlimited). A tile that\n"
            "would run faster than the screen wastes GPU the other players need.\n"
            "Proton games: DXVK / vkd3d limiter. Native games: MangoHud's\n"
            "limiter when MangoHud is installed (else only vsync games).")
        form.addRow("Frame limit:", self._fps_limit)
        self._fps_limit.valueChanged.connect(self._on_changed)

        self._render_scale.valueChanged.connect(self._on_display_changed)
        self._upscale_filter.currentIndexChanged.connect(self._on_display_changed)
        self._sharpness.valueChanged.connect(self._on_changed)
        self._loading = True      # initial label/enable state, no `changed`
        self._on_display_changed()
        return form

    def _on_display_changed(self, *_):
        # Snap to the 5% steps (dragging the handle doesn't honour singleStep).
        v = self._render_scale.value()
        if v % 5:
            self._render_scale.setValue(round(v / 5) * 5)
            return
        self._render_scale_label.setText(f"{v}%")
        scaled = v < 100
        self._upscale_filter.setEnabled(scaled)
        self._sharpness.setEnabled(
            scaled and self._upscale_filter.currentText() in ('fsr', 'nis'))
        self._on_changed()

    def get_display(self):
        """{render_scale, upscale_filter, upscale_sharpness} for SessionConfig."""
        return {
            'render_scale': self._render_scale.value(),
            'upscale_filter': self._upscale_filter.currentText(),
            'upscale_sharpness': self._sharpness.value(),
            'fps_limit': self._fps_limit.value(),
        }

    def _add_combo(self, fieldname, label, items, tooltip):
        w = QComboBox()
        w.addItem(self._default_item_text(fieldname))
        w.addItems(items)
        w.setToolTip(tooltip)
        w.currentIndexChanged.connect(self._on_changed)
        self._layout.addRow(label, w)
        self._combos[fieldname] = w
        return w

    def _add_line(self, fieldname, label, example, tooltip):
        w = QLineEdit()
        w.setPlaceholderText(example)
        w.setProperty('split_example', example)
        if tooltip:
            w.setToolTip(tooltip)
        w.textChanged.connect(self._on_changed)
        self._layout.addRow(label, w)
        self._lines[fieldname] = w
        return w

    def _on_changed(self, *_):
        if not self._loading:
            self.changed.emit()

    def _refresh_override_count(self):
        """Show on the (hidden-by-default) tab how many settings deviate
        from the handler/built-in defaults, so overrides aren't invisible."""
        n = len(self.get_overrides())
        n += 1 if self._extra_env.text().strip() else 0
        self._tabs.setTabText(
            self._ALL_TAB, f"All settings ({n})" if n else "All settings")
        self._tabs.setTabToolTip(
            self._ALL_TAB,
            f"{n} setting(s) overridden for this game; the rest follow the "
            f"handler script / built-in defaults." if n else
            "Every launch setting. All currently follow the handler script / "
            "built-in defaults.")

    # ------------------------------------------------------- default display

    def _field_default(self, fieldname):
        """Resolved non-override value: handler sheet, else built-in."""
        camel = _FIELD_TO_SHEET.get(fieldname)
        if camel and camel in self._sheet:
            return self._sheet[camel]
        return session_field_default(fieldname)

    def _default_item_text(self, fieldname):
        return f"default ({self._field_default(fieldname)})"

    def _refresh_default_hints(self):
        for fieldname, w in self._combos.items():
            w.setItemText(0, self._default_item_text(fieldname))
        for fieldname, w in self._checks.items():
            val = self._field_default(fieldname)
            src = ('handler' if _FIELD_TO_SHEET.get(fieldname) in self._sheet
                   else 'built-in')
            base = w.toolTip().split('\n\nDefault:')[0]
            w.setToolTip(f"{base}\n\nDefault: {'on' if val else 'off'} ({src}). "
                         f"The square 'partial' state means this default is in "
                         f"effect; check/uncheck to override.")
        for fieldname, w in self._lines.items():
            val = self._field_default(fieldname)
            # Reset to the built-in example when there's no handler value —
            # otherwise the previous game's handler default stays displayed.
            w.setPlaceholderText(f"{val}   (handler default)" if val
                                 else w.property('split_example') or '')

    # -------------------------------------------------------------- sheet API

    def set_script_sheet(self, sheet, label=None, error=None, warning=None):
        """Install the handler property sheet driving all default states.

        sheet: camelCase Game.* dict (or {}/None for no handler).
        label: text for the indicator (e.g. 'handlers/left-4-dead-2.js (Left 4 Dead 2)').
        error: handler load error to surface instead.
        warning: shown with the label (e.g. two handlers for the game).
        """
        self._sheet = dict(sheet or {})
        if error:
            self._handler_label.setText("Handler: BROKEN — see tooltip")
            self._handler_label.setStyleSheet("color: #cc4444;")
            self._handler_label.setToolTip(str(error))
        elif label and warning:
            self._handler_label.setText(f"Handler: {label}  ⚠ two handlers")
            self._handler_label.setStyleSheet("color: #d08a00;")
            self._handler_label.setToolTip(str(warning))
        elif label:
            self._handler_label.setText(f"Handler: {label}")
            self._handler_label.setStyleSheet("")
            self._handler_label.setToolTip(
                "Settings left at 'default' follow this script. Only explicit "
                "choices are saved as overrides.")
        else:
            self._handler_label.setText("Handler: none — this game can't be launched")
            self._handler_label.setStyleSheet("color: #cc4444;")
            self._handler_label.setToolTip(
                "Janus only launches games that have a handler script. Get one "
                "from the handler hub (Big Picture: Y, or `main.py hub get <appid>`).")
        self._hub_btn.setText("Handler hub…" if label and not error else "Get a handler…")
        self._reset_btn.setEnabled(True)
        self._refresh_default_hints()
        self._fill_input_modes()

    def set_hub_update(self, version):
        """The hub has a newer handler version for this game (None: not)."""
        if version:
            self._hub_btn.setText(f"Update handler (v{version})…")

    def set_setup(self, status, actions):
        """Show a non-Steam game's setup state (nonsteam.setup_status) and
        buttons: its fix when not ready, then *actions* [(key, spec)] (the
        maintenance ones). status None hides the box (a Steam game)."""
        self._setup_box.setVisible(status is not None)
        while self._setup_buttons.count():
            w = self._setup_buttons.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        if status is None:
            return
        if status['ready']:
            self._setup_label.setText("Ready" + (f": {status['status']}" if status['status'] else "."))
            self._setup_label.setStyleSheet("")
        else:
            self._setup_label.setText(status['message'])
            self._setup_label.setStyleSheet("color: #cc8800;")
        buttons = ([(status['action'], {'label': status['actionLabel']})]
                   if not status['ready'] and status['action'] else []) + list(actions)
        for key, spec in buttons:
            b = QPushButton(str(spec['label']))
            if spec.get('description'):
                b.setToolTip(str(spec['description']))
            b.clicked.connect(lambda _=False, k=key: self.actionRequested.emit(k))
            self._setup_buttons.addWidget(b)
        self._setup_buttons.addStretch()

    def set_script_options(self, specs, values=None):
        """Render Janus.addOption declarations. specs: [(key, spec)],
        values: saved answers {key: value}."""
        # Clear previous rows
        while self._options_form.rowCount():
            self._options_form.removeRow(0)
        self._opt_widgets = {}
        values = values or {}
        for key, spec in specs or []:
            typ = spec.get('type', 'string')
            val = values.get(key, spec.get('default'))
            if typ == 'choice':
                w = QComboBox()
                w.addItems([str(c) for c in spec.get('choices') or []])
                if val is not None:
                    w.setCurrentText(str(val))
                w.currentIndexChanged.connect(self._on_changed)
            elif typ == 'bool':
                w = QCheckBox()
                w.setChecked(bool(val))
                w.toggled.connect(self._on_changed)
            elif typ == 'int':
                w = QSpinBox()
                w.setRange(-1000000, 1000000)
                w.setValue(int(val or 0))
                w.valueChanged.connect(self._on_changed)
            else:
                w = QLineEdit()
                w.setText('' if val is None else str(val))
                w.textChanged.connect(self._on_changed)
            if spec.get('description'):
                w.setToolTip(str(spec['description']))
            self._options_form.addRow(str(spec.get('label', key)) + ":", w)
            self._opt_widgets[key] = (spec, w)
        self._no_options_label.setVisible(not self._opt_widgets)

    def get_option_values(self):
        out = {}
        for key, (spec, w) in self._opt_widgets.items():
            if isinstance(w, QComboBox):
                out[key] = w.currentText()
            elif isinstance(w, QCheckBox):
                out[key] = w.isChecked()
            elif isinstance(w, QSpinBox):
                out[key] = w.value()
            else:
                out[key] = w.text()
        return out

    # ---------------------------------------------------------- config API

    def load_config(self, config):
        """Set widget states from a SessionConfig: explicit states for its
        overrides, default states elsewhere. Call set_script_sheet first."""
        self._loading = True
        try:
            ov = config.overrides  # None = legacy: everything explicit
            for fieldname, w in self._checks.items():
                if ov is None or fieldname in ov:
                    w.setCheckState(Qt.Checked if getattr(config, fieldname)
                                    else Qt.Unchecked)
                else:
                    w.setCheckState(Qt.PartiallyChecked)
            for fieldname, w in self._combos.items():
                if ov is None or fieldname in ov:
                    # findText, not setCurrentText: on a non-editable combo an
                    # unknown value leaves the PREVIOUS game's selection.
                    idx = w.findText(str(getattr(config, fieldname)))
                    w.setCurrentIndex(idx if idx > 0 else 0)
                else:
                    w.setCurrentIndex(0)
            for fieldname, w in self._lines.items():
                if ov is None or fieldname in ov:
                    w.setText(getattr(config, fieldname) or '')
                else:
                    w.setText('')
            # Not tri-stated: a value equal to the handler/built-in default
            # counts as "default", so a non-overridden field shows the sheet.
            host = (config.goldberg_host_instance
                    if ov is None or 'goldberg_host_instance' in ov
                    else self._field_default('goldberg_host_instance'))
            self._goldberg_host_instance.setValue(max(1, int(host)))
            self._extra_env.setText(' '.join(
                f'{k}={v}' for k, v in (config.extra_env or {}).items()))
            self._render_scale.setValue(int(config.render_scale))
            idx = self._upscale_filter.findText(config.upscale_filter)
            self._upscale_filter.setCurrentIndex(max(0, idx))
            self._sharpness.setValue(int(config.upscale_sharpness))
            self._fps_limit.setValue(int(config.fps_limit))
            self._fill_input_modes(config.input_mode or '')
        finally:
            self._loading = False
        self._refresh_override_count()

    def get_overrides(self):
        """Explicit user choices only: {field_name: value}."""
        out = {}
        for fieldname, w in self._checks.items():
            st = w.checkState()
            if st != Qt.PartiallyChecked:
                out[fieldname] = (st == Qt.Checked)
        for fieldname, w in self._combos.items():
            if w.currentIndex() != 0:
                out[fieldname] = w.currentText()
        for fieldname, w in self._lines.items():
            if w.text().strip():
                out[fieldname] = w.text().strip()
        # Host-instance spinbox: an override only when it differs from the
        # default (recording it unconditionally made Game.goldbergHostInstance
        # unreachable — the spinbox's 1 always won).
        host = self._goldberg_host_instance.value()
        if host != self._field_default('goldberg_host_instance'):
            out['goldberg_host_instance'] = host
        return out

    def get_extra_env(self):
        result = {}
        for token in self._extra_env.text().split():
            if '=' in token:
                k, v = token.split('=', 1)
                result[k] = v
        return result

    def reset_to_defaults(self):
        """Clear every override -> everything follows the handler/built-ins."""
        self._loading = True
        try:
            for w in self._checks.values():
                w.setCheckState(Qt.PartiallyChecked)
            for w in self._combos.values():
                w.setCurrentIndex(0)
            for w in self._lines.values():
                w.setText('')
            self._goldberg_host_instance.setValue(
                max(1, int(self._field_default('goldberg_host_instance'))))
        finally:
            self._loading = False
        self.changed.emit()

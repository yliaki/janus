"""Data models for Janus GUI session configuration."""

from dataclasses import dataclass, field, fields as dataclass_fields
from typing import Optional

# Handler-script property sheet (camelCase Game.* global) -> SessionConfig
# field. These are the session-level fields a handler can default; the GUI
# stores only explicit user choices ("overrides") on top of them.
# See SCRIPTING.md.
SHEET_FIELD_MAP = {
    'isolation': 'isolation',
    'verbose': 'verbose',
    'nvidia': 'nvidia',
    'borderless': 'borderless',
    'copyPrefix': 'copy_prefix',
    'copyUserdata': 'copy_userdata',
    'forceGrabCursor': 'force_grab_cursor',
    'grab': 'grab',
    'hidePanel': 'hide_panel',
    'extraGamescopeArgs': 'extra_gamescope_args',
    'gameArgs': 'game_args',
    'goldberg': 'goldberg_enabled',
    'goldbergNoOverlay': 'goldberg_no_overlay',
    'goldbergDisableLan': 'goldberg_disable_lan',
    'goldbergDebug': 'goldberg_debug',
    'goldbergAutoJoin': 'goldberg_auto_join',
    'goldbergHostInstance': 'goldberg_host_instance',
    'steamInputFilter': 'steam_input_filter',
    'kwinKeepAbove': 'kwin_keep_above',
    'kwinAcceptFocus': 'kwin_accept_focus',
    'kwinDisableFsp': 'kwin_disable_fsp',
    'oskEnabled': 'osk_enabled',
    'exeName': 'exe_name',
    'launchCommand': 'launch_command',
}

# The SessionConfig fields that participate in the script-default /
# user-override model (= the values of SHEET_FIELD_MAP).
OVERRIDABLE_FIELDS = set(SHEET_FIELD_MAP.values())


def session_field_default(name: str):
    """The dataclass default for a SessionConfig field."""
    import dataclasses
    for f in dataclass_fields(SessionConfig):
        if f.name == name:
            if f.default is not dataclasses.MISSING:
                return f.default
            if f.default_factory is not dataclasses.MISSING:
                return f.default_factory()
            return None
    raise KeyError(name)


def resolve_field(name: str, config: 'SessionConfig',
                  sheet: Optional[dict]):
    """Effective value of one overridable field:
    user override > handler sheet > built-in default."""
    if config.overrides is None or name in config.overrides:
        return getattr(config, name)
    if sheet:
        for camel, snake in SHEET_FIELD_MAP.items():
            if snake == name and camel in sheet:
                return sheet[camel]
    return session_field_default(name)


def apply_script_defaults(config: 'SessionConfig',
                          sheet: Optional[dict]) -> 'SessionConfig':
    """Fill every non-overridden overridable field of *config* in place with
    its effective value (sheet default, else built-in default). After this
    the config's plain attributes are the values to launch with. Returns
    *config*. Legacy configs (overrides is None) are left untouched — every
    stored value is treated as an explicit override."""
    if config.overrides is None:
        return config
    for name in OVERRIDABLE_FIELDS:
        if name not in config.overrides:
            setattr(config, name, resolve_field(name, config, sheet))
    return config


@dataclass
class InstanceConfig:
    controller_index: Optional[int] = None
    instance_number: int = 1
    gamescope_width: int = 960
    gamescope_height: int = 1080
    pos_x: int = 0
    pos_y: int = 0
    monitor_name: Optional[str] = None
    extra_env: dict = field(default_factory=dict)
    # Controller identity — lets the hook bind by hardware (VID:PID/name) instead
    # of a bare kernel index that a flaky wireless pad can renumber out from under.
    controller_vid: int = 0
    controller_pid: int = 0
    controller_name: Optional[str] = None
    # Player profile (src/host/profiles.py) id. None = the instance's default
    # (the Steam account for instance 1, "Player N" for the others).
    profile_id: Optional[str] = None

    def to_dict(self) -> dict:
        d = {
            'controller_index': self.controller_index,
            'instance_number': self.instance_number,
            'gamescope_width': self.gamescope_width,
            'gamescope_height': self.gamescope_height,
            'pos_x': self.pos_x,
            'pos_y': self.pos_y,
            'extra_env': dict(self.extra_env),
        }
        if self.monitor_name is not None:
            d['monitor_name'] = self.monitor_name
        if self.controller_vid:
            d['controller_vid'] = self.controller_vid
        if self.controller_pid:
            d['controller_pid'] = self.controller_pid
        if self.controller_name:
            d['controller_name'] = self.controller_name
        if self.profile_id:
            d['profile_id'] = self.profile_id
        return d

    @classmethod
    def from_dict(cls, d: dict) -> 'InstanceConfig':
        return cls(
            controller_index=d.get('controller_index'),
            instance_number=d.get('instance_number', 1),
            gamescope_width=d.get('gamescope_width', 960),
            gamescope_height=d.get('gamescope_height', 1080),
            pos_x=d.get('pos_x', 0),
            pos_y=d.get('pos_y', 0),
            monitor_name=d.get('monitor_name'),
            extra_env=d.get('extra_env', {}),
            controller_vid=d.get('controller_vid', 0),
            controller_pid=d.get('controller_pid', 0),
            controller_name=d.get('controller_name'),
            profile_id=d.get('profile_id'),
        )


@dataclass
class SessionConfig:
    appid: Optional[int | str] = None     # Steam appid, or a non-Steam game id
    game_name: Optional[str] = None
    instances: list = field(default_factory=lambda: [InstanceConfig(instance_number=1), InstanceConfig(instance_number=2)])
    isolation: str = 'secondary'
    verbose: bool = False
    nvidia: bool = False
    borderless: bool = True
    copy_prefix: bool = False
    copy_userdata: bool = False
    force_grab_cursor: bool = False
    grab: bool = False
    hide_panel: bool = True
    extra_gamescope_args: str = ""
    game_args: str = ""
    extra_env: dict = field(default_factory=dict)
    layout: Optional[dict] = None
    goldberg_enabled: bool = False
    goldberg_no_overlay: bool = True
    goldberg_disable_lan: bool = True
    # Debug build of gbe_fork emits STEAM_LOG_*.log — required for the lobby
    # watcher (which detects the host's lobby ID by tailing the log). Place
    # the debug build in vendor/goldberg/debug/.
    goldberg_debug: bool = False
    # Auto-join: launch goldberg_host_instance first, watch its log for a
    # lobby ID, then launch the remaining instances with
    # "+connect_lobby <id>" appended to game_args.
    goldberg_auto_join: bool = False
    # 1-based instance index whose game will be launched first under
    # auto-join. The others wait until its lobby ID is detected.
    goldberg_host_instance: int = 1
    # Steam Input vtable filter: keep Steam Input active per-instance and filter
    # GetConnectedControllers to the assigned pad (JANUS_STEAM_INPUT_FILTER),
    # instead of nulling Steam Input. Needed for controllers that only work via
    # Steam Input (e.g. generic adapters with no SDL gamecontroller mapping).
    steam_input_filter: bool = False
    # KWin Wayland (Path A) per-tile window-rule knobs. Ignored outside a KWin
    # Wayland session. See wayland_session._rules_text.
    kwin_keep_above: bool = False     # force always-on-top (above)
    kwin_accept_focus: bool = True    # force acceptfocus
    kwin_disable_fsp: bool = True     # force focus-stealing-prevention -> None
    # On-screen keyboard: spawns src.host.osk alongside the session.
    # L3+R3 on a controller summons a gamepad-driven keyboard over that
    # instance's gamescope window. While shown, the bound pad is
    # EVIOCGRAB'd so the game ignores the navigation inputs.
    osk_enabled: bool = False
    exe_name: str = ""
    # Full launch command relative to the game's Steam install dir, e.g.
    # "./jre/bin/java -Xmx4G -jar Necesse.jar". Overrides exe_name and all
    # executable auto-detection — needed for Java/interpreter-launched games
    # whose real entry point isn't a discoverable binary. Supports the same
    # %TAG% expansion as game_args.
    launch_command: str = ""
    # Render scale: the game renders at this % of its tile and gamescope
    # upscales it with upscale_filter (sharpness for fsr/nis). Lower = faster,
    # and a bigger UI in games whose UI is sized in pixels. Not handler-
    # overridable (plain user setting, like extra_env).
    render_scale: int = 100
    upscale_filter: str = 'linear'
    upscale_sharpness: int = 2
    # Frame limit per game (perf.py): gamescope -r/-o + the game-side limiter
    # (DXVK/vkd3d under Proton, MangoHud for native games). 0 = unlimited.
    fps_limit: int = 60
    # How the controllers reach the games (src/host/controller_input.py): 'janus'
    # (Janus's own virtual Xbox pads), 'steam' (Steam Input), '' = default
    # (Janus input when the handler allows it: Game.janusInput/steamInput).
    # Not a sheet field: the handler only says which modes it supports.
    input_mode: str = ''
    # Explicit user overrides over the handler script's defaults: the set of
    # OVERRIDABLE_FIELDS names the user actually chose in the GUI. None =
    # legacy config (every stored value is an override — pre-scripting
    # behavior preserved). Non-overridden fields take the handler sheet's
    # value (or the built-in default) via apply_script_defaults().
    overrides: Optional[set] = None
    # Answers to the handler's Janus.addOption declarations ({key: value}).
    script_options: dict = field(default_factory=dict)
    # Big Picture mode's own memory (layout per player count, ...). Kept
    # separate from `layout`/`instances` so the two GUIs don't overwrite each
    # other; the desktop GUI just carries it through.
    bigpicture: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = {
            'appid': self.appid,
            'game_name': self.game_name,
            'instances': [i.to_dict() for i in self.instances],
            'isolation': self.isolation,
            'verbose': self.verbose,
            'nvidia': self.nvidia,
            'borderless': self.borderless,
            'copy_prefix': self.copy_prefix,
            'copy_userdata': self.copy_userdata,
            'force_grab_cursor': self.force_grab_cursor,
            'grab': self.grab,
            'hide_panel': self.hide_panel,
            'extra_gamescope_args': self.extra_gamescope_args,
            'game_args': self.game_args,
            'extra_env': dict(self.extra_env),
            'goldberg_enabled': self.goldberg_enabled,
            'goldberg_no_overlay': self.goldberg_no_overlay,
            'goldberg_disable_lan': self.goldberg_disable_lan,
            'goldberg_debug': self.goldberg_debug,
            'goldberg_auto_join': self.goldberg_auto_join,
            'goldberg_host_instance': self.goldberg_host_instance,
            'steam_input_filter': self.steam_input_filter,
            'kwin_keep_above': self.kwin_keep_above,
            'kwin_accept_focus': self.kwin_accept_focus,
            'kwin_disable_fsp': self.kwin_disable_fsp,
            'osk_enabled': self.osk_enabled,
            'exe_name': self.exe_name,
            'launch_command': self.launch_command,
            'render_scale': self.render_scale,
            'upscale_filter': self.upscale_filter,
            'upscale_sharpness': self.upscale_sharpness,
            'fps_limit': self.fps_limit,
            'input_mode': self.input_mode,
        }
        if self.layout is not None:
            d['layout'] = self.layout
        if self.overrides is not None:
            d['override_keys'] = sorted(self.overrides)
        if self.script_options:
            d['script_options'] = dict(self.script_options)
        if self.bigpicture:
            d['bigpicture'] = dict(self.bigpicture)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> 'SessionConfig':
        instances = [InstanceConfig.from_dict(i) for i in d.get('instances', [])]
        if not instances:
            instances = [InstanceConfig(instance_number=1), InstanceConfig(instance_number=2)]
        return cls(
            appid=d.get('appid'),
            game_name=d.get('game_name'),
            instances=instances,
            isolation=d.get('isolation', 'secondary'),
            verbose=d.get('verbose', False),
            nvidia=d.get('nvidia', False),
            borderless=d.get('borderless', True),
            copy_prefix=d.get('copy_prefix', False),
            copy_userdata=d.get('copy_userdata', False),
            force_grab_cursor=d.get('force_grab_cursor', False),
            grab=d.get('grab', False),
            hide_panel=d.get('hide_panel', True),
            extra_gamescope_args=d.get('extra_gamescope_args', ''),
            game_args=d.get('game_args', ''),
            extra_env=d.get('extra_env', {}),
            layout=d.get('layout'),
            goldberg_enabled=d.get('goldberg_enabled', False),
            goldberg_no_overlay=d.get('goldberg_no_overlay', True),
            goldberg_disable_lan=d.get('goldberg_disable_lan', True),
            goldberg_debug=d.get('goldberg_debug', False),
            goldberg_auto_join=d.get('goldberg_auto_join', False),
            goldberg_host_instance=d.get('goldberg_host_instance', 1),
            steam_input_filter=d.get('steam_input_filter', False),
            kwin_keep_above=d.get('kwin_keep_above', False),
            kwin_accept_focus=d.get('kwin_accept_focus', True),
            kwin_disable_fsp=d.get('kwin_disable_fsp', True),
            osk_enabled=d.get('osk_enabled', False),
            exe_name=d.get('exe_name', ''),
            launch_command=d.get('launch_command', ''),
            render_scale=d.get('render_scale', 100),
            upscale_filter=d.get('upscale_filter', 'linear'),
            upscale_sharpness=d.get('upscale_sharpness', 2),
            fps_limit=int(d.get('fps_limit', 60)),
            input_mode=d.get('input_mode', ''),
            overrides=(set(d['override_keys'])
                       if 'override_keys' in d else None),
            script_options=d.get('script_options', {}),
            bigpicture=d.get('bigpicture', {}),
        )

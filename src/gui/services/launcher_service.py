"""Launch service - QThread workers wrapping launch_steam_game()."""

import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Qt, Signal

sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'host'))
from steam import launch_steam_game, goldberg_mirror_dir
import profiles
from focus_release import run_daemon as run_focus_release, release_soon
import steam_input
from pad_matcher import PadMatcher
import wayland_session as ws
from split_session import gamescope_size_args, render_size
from janus_procs import (JANUS_PROC_MARKER, janus_launched_pids, kill_janus_processes,
                         kill_instance_processes)
import layouts
import resize
import perf
import focus_pads
import kbm
import controller_input as im
from padd import PadDaemonError

from .lobby_watcher import LobbyWatcher


def expand_game_args(template, **kwargs):
    """Expand %TAG% placeholders in a game args template string.

    Supported tags: %WIDTH%, %HEIGHT%, %CONTROLLER%, %INSTANCE%,
    %APPID%, %POS_X%, %POS_Y%, %NUM_PLAYERS%.
    """
    result = template
    for key, value in kwargs.items():
        result = result.replace(f'%{key}%', str(value))
    return result


# Session processes are found by their environment marker (janus_procs.py).
_JANUS_PROC_MARKER = JANUS_PROC_MARKER
_janus_launched_pids = janus_launched_pids
_kill_janus_processes = kill_janus_processes


class InstanceWorker(QObject):
    """Worker that runs in a QThread to launch and monitor one game instance."""

    started = Signal(int, int)    # instance_index, pid
    finished = Signal(int, int)   # instance_index, return_code
    error = Signal(int, str)      # instance_index, error_message

    def __init__(self, instance_index, appid, controller, instance_number,
                 isolation, verbose, nvidia, borderless,
                 copy_prefix, copy_userdata, force_grab_cursor, grab,
                 gamescope_w, gamescope_h, pos_x, pos_y,
                 extra_gamescope_args, game_args, extra_env,
                 goldberg_enabled=False, goldberg_no_overlay=True,
                 goldberg_disable_lan=True,
                 goldberg_debug=False, connect_lobby_args=None,
                 num_players=1, exe_name=None, launch_command=None,
                 wmclass=None, rate=60,
                 controller_vid=0, controller_pid=0, controller_name=None,
                 script=None, profile_id=None, players=None,
                 render_scale=100, upscale_filter='linear',
                 upscale_sharpness=2, input_nodes=None, nested=None,
                 fps_limit=None):
        super().__init__()
        self.fps_limit = fps_limit      # perf.py: the game's frame limit
        # Resizable game (resize.py): gamescope's screen, monitor-sized.
        self.nested = nested
        # Janus input: this instance's virtual pad nodes (padd.py); None =
        # Steam Input mode.
        self.input_nodes = input_nodes
        self.instance_index = instance_index
        self.appid = appid
        self.controller = controller
        self.controller_vid = controller_vid
        self.controller_pid = controller_pid
        self.controller_name = controller_name
        self.instance_number = instance_number
        self.isolation = isolation
        self.verbose = verbose
        self.nvidia = nvidia
        self.borderless = borderless
        self.copy_prefix = copy_prefix
        self.copy_userdata = copy_userdata
        self.force_grab_cursor = force_grab_cursor
        self.grab = grab
        self.gamescope_w = gamescope_w
        self.gamescope_h = gamescope_h
        self.render_scale = render_scale
        self.upscale_filter = upscale_filter
        self.upscale_sharpness = upscale_sharpness
        self.pos_x = pos_x
        self.pos_y = pos_y
        self.extra_gamescope_args = extra_gamescope_args
        self.game_args = game_args
        self.extra_env = extra_env
        self.goldberg_enabled = goldberg_enabled
        self.goldberg_no_overlay = goldberg_no_overlay
        self.goldberg_disable_lan = goldberg_disable_lan
        self.goldberg_debug = goldberg_debug
        self.connect_lobby_args = connect_lobby_args or []
        self.num_players = num_players
        self.script = script
        self.exe_name = exe_name
        self.launch_command = launch_command
        self.wmclass = wmclass          # per-instance SDL_VIDEO_X11_WMCLASS
        self.rate = rate                # gamescope -r / -o (no unfocused throttle)
        self.profile_id = profile_id    # resolved player profile (profiles.py)
        self.players = players or []    # whole session: [{'instance', 'profile'}]
        self._proc = None

    def run(self):
        try:
            # Build gamescope args. Path A: gamescope SDL backend so its host
            # window is matchable by a KWin rule (via SDL_VIDEO_X11_WMCLASS).
            # Output sized in native px (the session pins the output to scale
            # 1.0); -o == -r so a backgrounded tile keeps full framerate.
            # -w/-h (what the game renders at) = render_scale % of the tile;
            # %WIDTH%/%HEIGHT% and ctx.width/height are that render size.
            render_w, render_h = render_size(
                self.gamescope_w, self.gamescope_h, self.render_scale)
            gs_args = [
                '--backend', 'sdl',
                *gamescope_size_args(self.gamescope_w, self.gamescope_h,
                                     self.render_scale, self.upscale_filter,
                                     self.upscale_sharpness, nested=self.nested),
                '-r', str(self.rate), '-o', str(self.rate),
            ]
            if self.borderless:
                gs_args.append('-b')
            if self.force_grab_cursor:
                gs_args.append('--force-grab-cursor')
            if self.grab:
                gs_args.append('--grab')
            if self.extra_gamescope_args:
                gs_args.extend(shlex.split(self.extra_gamescope_args))

            # Set extra env vars
            for k, v in self.extra_env.items():
                os.environ[k] = v

            def _expand(template):
                return expand_game_args(
                    template,
                    WIDTH=render_w, HEIGHT=render_h,
                    CONTROLLER=self.controller if self.controller is not None else '',
                    INSTANCE=self.instance_number, APPID=self.appid,
                    POS_X=self.pos_x, POS_Y=self.pos_y,
                    NUM_PLAYERS=self.num_players,
                )

            extra_args = shlex.split(_expand(self.game_args)) if self.game_args else []
            # The launch command takes the same %TAG% placeholders, so a Java
            # game can size itself per tile (e.g. "-Dwidth=%WIDTH%").
            launch_command = _expand(self.launch_command) if self.launch_command else None
            if self.connect_lobby_args:
                # Join args for gbe_fork's lobby (default '+connect_lobby <id>',
                # overridable per game via Game.lobbyConnectArg or an
                # onLobbyDetected hook). Appending to the user's game_args is
                # safe: order doesn't matter for Source / Unity / Unreal games.
                extra_args = extra_args + list(self.connect_lobby_args)

            self._proc = launch_steam_game(
                appid=self.appid,
                controller=self.controller,
                instance=self.instance_number,
                isolation=self.isolation,
                verbose=self.verbose,
                gamescope_args=gs_args,
                nvidia_prime=self.nvidia,
                # Game.useRuntime (the GUI has no runtime toggle of its own)
                use_runtime=(bool(self.script.get('useRuntime', True))
                             if self.script is not None else True),
                copy_prefix=self.copy_prefix,
                copy_userdata=self.copy_userdata,
                extra_args=extra_args,
                goldberg_enabled=self.goldberg_enabled,
                goldberg_no_overlay=self.goldberg_no_overlay,
                goldberg_disable_lan=self.goldberg_disable_lan,
                goldberg_debug=self.goldberg_debug,
                exe_name=self.exe_name,
                launch_command=launch_command,
                wmclass=self.wmclass,
                controller_vid=self.controller_vid,
                controller_pid=self.controller_pid,
                controller_name=self.controller_name,
                script=self.script,
                script_ctx={'width': render_w,
                            'height': render_h,
                            'posX': self.pos_x, 'posY': self.pos_y,
                            'numPlayers': self.num_players,
                            'players': self.players},
                profile=self.profile_id,
                input_nodes=self.input_nodes,
                fps_limit=self.fps_limit,
            )

            self.started.emit(self.instance_index, self._proc.pid)

            # Window placement: the KWin rule (written by the session) positions
            # the tile at map time.

            # Wait for process to exit
            rc = self._proc.wait()
            self.finished.emit(self.instance_index, rc)

        except Exception as e:
            self.error.emit(self.instance_index, str(e))

    def stop(self):
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except Exception:
                self._proc.kill()
        if self._proc is not None and getattr(self._proc, 'janus_cleanup', None):
            self._proc.janus_cleanup()     # a launcher's game (nonsteam.py)


def _js_number(nodes):
    """N of the /dev/input/jsN among *nodes*, None if there's none."""
    for n in nodes:
        base = os.path.basename(n)
        if base.startswith('js') and base[2:].isdigit():
            return int(base[2:])
    return None


def session_pads(instances):
    """The connected controllers of *instances* (InstanceConfigs), for checks
    that need their identity, uniq included."""
    from controllers import list_controllers
    by_index = {c.index: c for c in list_controllers()}
    pads = []
    for inst in instances:
        c = by_index.get(inst.controller_index)
        if c is not None and (not inst.controller_vid or
                              (c.vendor_id, c.product_id) ==
                              (inst.controller_vid, inst.controller_pid)):
            pads.append(c)
    return pads


class LauncherService(QObject):
    """Manages launching multiple game instances with staggered starts."""

    instanceStarted = Signal(int, int)    # index, pid
    instanceFinished = Signal(int, int)   # index, return_code
    instanceError = Signal(int, str)      # index, error_message
    allFinished = Signal()
    stopRequested = Signal()              # global Alt+2 hotkey
    lobbyDetected = Signal(int, str)      # instance_index, lobby_id
    scriptMessage = Signal(str, str, object)  # level, text, done() callback
    # Dynamic players: the tiles moved (a player left or joined).
    # {instance index: (x, y, w, h)} of every instance still in the session.
    tilesChanged = Signal(object)
    instanceAdded = Signal(int, int)      # a player joined: index, instance number

    def __init__(self, parent=None):
        super().__init__(parent)
        self._workers: list = []
        self._threads: list[QThread] = []
        self._running_count = 0
        self._focus_release_thread = None
        self.stopRequested.connect(self.stop_all, Qt.QueuedConnection)
        # KWin Wayland "Path A" session (scale/panel/rules).
        self._wsession = None
        self._pad_matcher = None
        self._pad_daemon = None   # Janus input: the session's padd.PadDaemon
        self._osk_virtual = False
        # OSK daemon: lazy-started once all instances have reported PIDs.
        self._osk_enabled = False
        self._osk_proc: subprocess.Popen | None = None
        self._osk_pending: dict[int, int] = {}  # controller_index → pid
        self._osk_expected: int = 0              # how many instances we wait for
        self._osk_index_to_controller: dict[int, int | None] = {}
        # Listen for instances starting so we can collect their pids for the
        # OSK bindings. Direct connection — we just stash data, fast.
        self.instanceStarted.connect(self._on_instance_started_for_osk)

        # Lobby watcher: tails gbe_fork debug logs. Always created, only
        # started when Goldberg is active. Re-export its signal so MainWindow
        # subscribes to a single service; auto-join logic lives in its own slot.
        self._lobby_watcher = LobbyWatcher(self)
        self._lobby_watcher.lobbyDetected.connect(self.lobbyDetected)
        self._lobby_watcher.lobbyDetected.connect(self._maybe_dispatch_autojoin)

        # Auto-join state: when goldberg_auto_join is set, host launches
        # immediately and non-host indices are stashed here. They start once
        # the watcher reports the host's lobby ID.
        self._pending_autojoin: list[int] = []
        self._autojoin_host_index: int | None = None
        self._autojoin_started: bool = False  # flips True once non-hosts dispatched

        # Handler script for the running session (scripting.HandlerScript).
        # Per-instance hooks fire inside launch_steam_game (worker threads;
        # invoke() serializes); the session-level hooks (onAllStarted /
        # onInstanceExit / onStop) fire here on the GUI thread.
        self._script = None
        self._script_finished = True
        self._script_started_pids: dict[int, int] = {}
        self._script_expected = 0
        self.instanceStarted.connect(self._on_instance_started_for_script)
        # Env keys the previous session's workers wrote into os.environ
        # (extra_env + toggle vars). Cleared before the next launch so one
        # game's env can't leak into another launched later in the same GUI
        # process. (Caveat: if the user's own shell also exported one of
        # these keys, it is dropped too — re-set it via Extra env.)
        self._env_injected: set[str] = set()

        # Dynamic players (a player's game quits: the others re-flow; a
        # player joins mid-session). _order = the session's instances, in
        # visual order (tile order of the layout); finished ones leave it.
        # layout_for(n) -> [(x, y, w, h, monitor name)] gives the tiles for n
        # players (a GUI sets it; else layouts.default_rects on the session's
        # monitors).
        self.layout_for = None
        self._order: list[int] = []
        self._reflow = False
        self._late_join = False
        self._input_mode = None
        self._host_lobby = None
        self._monitors = []
        self._all_started_fired = False
        # Game.resizeGame = "window": a resize.WindowKeeper per instance.
        self._keepers: dict = {}
        self.instanceStarted.connect(self._start_keeper)
        # Start on a playing pad brings the tiles back (focus_pads.py).
        self._start_watch = None
        # Game.keyboardMouse: a kbm.KbmEmulator per instance.
        self._kbm: dict = {}
        self._kbm_map = None

    def _pad_node(self, index: int):
        """The evdev node the Start watcher reads for instance *index*: its
        virtual pad (Janus input) or its physical pad (Steam Input)."""
        c = self._launch_common
        cfg = c['instances_config'][index]
        if c.get('input_nodes') is not None:
            nodes = c['input_nodes'].get(cfg.instance_number) or []
            return next((n for n in nodes if os.path.basename(n).startswith('event')), None)
        from controllers import event_node
        try:
            return event_node(cfg.controller_index)
        except Exception:
            return None

    def _watch_pad(self, index: int):
        if self._start_watch is None:
            self._start_watch = focus_pads.StartWatcher(self._on_pad_start)
        node = self._pad_node(index)
        if node:
            self._start_watch.set_pad(index, node)

    def _start_kbm(self, index: int):
        """Game.keyboardMouse (Janus input): this player's pad drives
        keyboard and mouse in their own gamescope (kbm.py)."""
        if not self._kbm_map or index in self._kbm:
            return
        node = self._pad_node(index)
        if not node:
            return
        wmclass = f'janus-p{index + 1}'
        number = self._launch_common['instances_config'][index].instance_number
        self._kbm[index] = kbm.KbmEmulator(
            node, lambda: resize.nested_display(wmclass), self._kbm_map,
            log=lambda m: print(m, flush=True), label=f'P{number}',
            socket_fn=lambda: resize.gamescope_socket(wmclass))

    def _stop_kbm(self, index: int):
        e = self._kbm.pop(index, None)
        if e is not None:
            e.stop()

    def _on_pad_start(self, index):
        """Start on a playing pad (watcher thread): tiles back to the front
        unless one is already active."""
        if index is not None and self._running_count > 0:
            focus_pads.refocus_tiles(f'janus-p{index + 1}')

    def _stop_start_watch(self):
        if self._start_watch is not None:
            self._start_watch.stop()
            self._start_watch = None

    def _start_keeper(self, index: int, pid: int):
        if getattr(self, '_resize_mode', None) != 'window' or index in self._keepers:
            return
        cfg = self._launch_common['instances_config'][index]
        self._keepers[index] = resize.WindowKeeper(
            self._launch_common['appid'], cfg.instance_number, f'janus-p{index + 1}',
            log=lambda m: print(m, flush=True))

    def _stop_keeper(self, index: int):
        k = self._keepers.pop(index, None)
        if k is not None:
            k.stop()

    def launch_all(self, appid, instances_config, isolation, verbose, nvidia,
                   borderless, copy_prefix, copy_userdata,
                   force_grab_cursor=False, grab=False,
                   hide_panel=False,
                   extra_gamescope_args='', game_args='', extra_env=None,
                   goldberg_enabled=False, goldberg_no_overlay=True,
                   goldberg_disable_lan=True,
                   goldberg_debug=False, goldberg_auto_join=False,
                   goldberg_host_instance=1,
                   osk_enabled=False,
                   exe_name=None, launch_command=None,
                   kwin_keep_above=False, kwin_accept_focus=True,
                   kwin_disable_fsp=True, script=None,
                   render_scale=100, upscale_filter='linear',
                   upscale_sharpness=2, input_mode='steam', fps_limit=None):
        """Launch all instances with 3s stagger between each.

        input_mode 'janus': the session's input daemon (padd.py) gives every
        instance its own virtual Xbox pad (raises ValueError when it can't
        start); 'steam': Steam Input, filtered per instance by the hook.

        Under ``goldberg_auto_join``, the instance at the 1-based index
        ``goldberg_host_instance`` is started first; the others are held back
        until the lobby watcher reports a lobby ID from that instance, then
        launched with '+connect_lobby <id>' appended to their game_args.
        """
        # Who plays which instance. Resolved up front: an error (one profile
        # twice) aborts before anything starts, and every instance's handler
        # sees the whole session in ctx.players.
        session_profiles = profiles.resolve_session(
            [(getattr(cfg, 'profile_id', None), cfg.instance_number)
             for cfg in instances_config])

        self.stop_all()
        input_nodes = None
        if input_mode == im.JANUS:
            try:
                self._pad_daemon = im.start_daemon(
                    appid, [(cfg.instance_number, cfg.controller_index)
                            for cfg in instances_config], verbose)
            except PadDaemonError as e:
                raise ValueError(f"Janus input couldn't start: {e}") from e
            input_nodes = self._pad_daemon.nodes
        # Steam applies the game's Steam Input layout whatever has focus
        # (steam_input.force_layout); released in _restore_panel().
        steam_input.force_layout(appid, script)

        # Purge env keys injected by the previous session (see _env_injected).
        for k in self._env_injected:
            os.environ.pop(k, None)
        self._env_injected = set(extra_env or {}) | {
            k for cfg in instances_config for k in (cfg.extra_env or {})
        }

        self._script = script
        self._script_finished = script is None
        if script is not None:
            # Janus.warn/error → MainWindow dialog. Hooks run on worker
            # threads; the signal is queued onto the GUI thread and the hook
            # blocks until done() (dialog closed). On the GUI thread itself
            # the slot runs directly and its modal exec() does the blocking.
            script.set_notify(self.scriptMessage.emit,
                              ui_thread=threading.get_ident())
        self._script_started_pids = {}
        self._script_expected = len(instances_config)

        # KWin Wayland "Path A": a WaylandSession (scale 1.0, panel hide, KWin
        # window rules matched by SDL_VIDEO_X11_WMCLASS) places the tiles.
        # MainWindow refuses to launch outside a KWin Wayland session.
        self._start_wayland_session(
            instances_config, hide_panel,
            keep_above=kwin_keep_above, accept_focus=kwin_accept_focus,
            disable_fsp=kwin_disable_fsp)

        self._running_count = len(instances_config)

        # Identical pads -> their Steam virtual pads, one-to-one (pad_matcher.py).
        # Rewrites each instance's file after its staggered launch clears it.
        self._stop_pad_matcher()
        if input_nodes is None:
            self._pad_matcher = PadMatcher(
                appid, [(cfg.instance_number, cfg.controller_index)
                        for cfg in instances_config]).start()

        # OSK daemon state: collect controller_index → pid per instance,
        # spawn the daemon once we've seen all instances start.
        self._osk_enabled = bool(osk_enabled)
        self._osk_expected = len(instances_config)
        self._osk_pending = {}
        # Janus input: the OSK reads the player's virtual pad (the physical one
        # is held by the input daemon), bound by its js number.
        self._osk_virtual = input_nodes is not None
        self._osk_index_to_controller = {
            i: (_js_number(input_nodes.get(cfg.instance_number, []))
                if input_nodes is not None else cfg.controller_index)
            for i, cfg in enumerate(instances_config)
        }

        # Start the focus-release / stop-all hotkey daemon
        if self._focus_release_thread is None or not self._focus_release_thread.is_alive():
            self._focus_release_thread = threading.Thread(
                target=run_focus_release,
                kwargs={
                    'key_combo': 'alt+1',
                    'stop_combo': 'alt+2',
                    'stop_callback': self.stopRequested.emit,
                },
                daemon=True,
            )
            self._focus_release_thread.start()

        # Common launch parameters captured for delayed (auto-join) workers.
        self._launch_common = dict(
            appid=appid, isolation=isolation, verbose=verbose, nvidia=nvidia,
            borderless=borderless,
            copy_prefix=copy_prefix, copy_userdata=copy_userdata,
            force_grab_cursor=force_grab_cursor, grab=grab,
            extra_gamescope_args=extra_gamescope_args, game_args=game_args,
            extra_env=extra_env or {},
            goldberg_enabled=goldberg_enabled,
            goldberg_no_overlay=goldberg_no_overlay,
            goldberg_disable_lan=goldberg_disable_lan,
            goldberg_debug=goldberg_debug,
            num_players=len(instances_config),
            exe_name=exe_name or None,
            launch_command=launch_command or None,
            instances_config=instances_config,
            rate=perf.gamescope_rate(perf.DEFAULT_FPS_LIMIT if fps_limit is None
                                     else fps_limit),
            fps_limit=fps_limit,
            render_scale=render_scale,
            upscale_filter=upscale_filter,
            upscale_sharpness=upscale_sharpness,
            script=script,
            profile_ids=[p.id for p in session_profiles],
            players=[{'instance': cfg.instance_number, 'profile': p.ctx()}
                     for cfg, p in zip(instances_config, session_profiles)],
            input_nodes=input_nodes,
        )

        # Dynamic players: the instances in visual order (monitor, then top
        # to bottom, left to right) — what a re-flow keeps.
        from .screen_info import get_all_monitors
        try:
            mons = get_all_monitors()
        except Exception:
            mons = []
        used = {cfg.monitor_name for cfg in instances_config}
        self._monitors = [m for m in mons if m.name in used] or mons
        rank = {m.name: i for i, m in enumerate(
            sorted(self._monitors, key=lambda m: (not m.primary, m.x, m.y)))}
        self._order = sorted(
            range(len(instances_config)),
            key=lambda i: (rank.get(instances_config[i].monitor_name, 0),
                           instances_config[i].pos_y, instances_config[i].pos_x))
        self._reflow = bool(script.get('reflow', True)) if script is not None else True
        self._late_join = bool(script.get('lateJoin', True)) if script is not None else False
        self._input_mode = input_mode
        self._host_lobby = None
        self._all_started_fired = False
        self._side_by_side_first = bool(script.get('sideBySideFirst')) if script is not None else False
        # A game that resizes with its tile (Game.resizeGame): every
        # gamescope screen as big as the biggest monitor, so any tile fits.
        self._resize_mode = resize.mode(script)
        self._nested = ((max(m.width for m in mons), max(m.height for m in mons))
                        if self._resize_mode and mons else None)

        self._stop_start_watch()
        for i in range(len(instances_config)):
            self._watch_pad(i)
        for i in list(self._kbm):
            self._stop_kbm(i)
        self._kbm_map = None
        if script is not None and script.get('keyboardMouse'):
            if input_nodes is None:
                print('[launcher] Game.keyboardMouse needs Janus input; '
                      'in Steam Input mode use a keyboard/mouse Steam layout', flush=True)
            else:
                self._kbm_map = dict(script.get('keyboardMouse'))
                for i in range(len(instances_config)):
                    self._start_kbm(i)

        # Start the lobby watcher whenever Goldberg is on. It just polls; if
        # the debug build isn't in use, it never finds a log and never emits.
        if goldberg_enabled:
            mirror_dirs = {
                i: goldberg_mirror_dir(appid, cfg.instance_number)
                for i, cfg in enumerate(instances_config)
            }
            self._lobby_watcher.start(mirror_dirs)

        # Decide launch order. Auto-join requires Goldberg+debug to be on
        # AND at least two instances — otherwise fall back to all-at-once.
        host_idx = self._resolve_host_index(
            instances_config, goldberg_host_instance,
        )
        do_autojoin = (
            goldberg_auto_join and goldberg_enabled and goldberg_debug
            and len(instances_config) > 1 and host_idx is not None
        )

        from PySide6.QtCore import QTimer
        # On Wayland, give kscreen-doctor's scale flip time to apply before
        # gamescope queries the output (else it reads the pre-flip mode/size).
        settle = 1500
        if do_autojoin:
            self._autojoin_host_index = host_idx
            self._autojoin_started = False
            # Build & start the host worker now.
            host_worker, host_thread = self._build_worker(host_idx)
            self._workers.append(host_worker)
            self._threads.append(host_thread)
            # Stash the others; they'll launch when lobbyDetected fires.
            self._pending_autojoin = [
                i for i in range(len(instances_config)) if i != host_idx
            ]
            QTimer.singleShot(settle, host_thread.start)
        else:
            self._autojoin_host_index = None
            self._pending_autojoin = []
            for i in range(len(instances_config)):
                w, t = self._build_worker(i)
                self._workers.append(w)
                self._threads.append(t)
            for i, thread in enumerate(self._threads):
                QTimer.singleShot(settle + i * 3000, thread.start)

    def launch_config(self, config, script=None):
        """launch_all() for a fully resolved SessionConfig (handler defaults
        already applied). Shared by the desktop GUI and Big Picture mode.
        Raises ValueError without a handler: there is no default launch."""
        if script is None:
            import scripting
            raise ValueError(scripting.no_handler_message(config.appid))
        mode = im.resolve(script, config.input_mode or None)
        steam_input.check(config.appid, script, config.game_name or None,
                          mode)  # SteamInputNotEnabled(ValueError)
        # (A missing Gamepad layout is only a warning: the GUIs ask first.)
        # Inject the hook toggle env vars — ALWAYS with an explicit value.
        # InstanceWorker writes these into the GUI process's os.environ, where
        # they'd otherwise persist across launches: setting them only when
        # non-default let e.g. JANUS_STEAM_INPUT_FILTER=1 from a TF2 session
        # leak into a later L4D2/PEAK launch whose config has the filter off.
        launch_env = dict(config.extra_env)
        launch_env['JANUS_STEAM_INPUT_FILTER'] = (
            '1' if config.steam_input_filter else '0')
        self.launch_all(
            script=script,
            appid=config.appid,
            instances_config=config.instances,
            isolation=config.isolation,
            verbose=config.verbose,
            nvidia=config.nvidia,
            borderless=config.borderless,
            copy_prefix=config.copy_prefix,
            copy_userdata=config.copy_userdata,
            force_grab_cursor=config.force_grab_cursor,
            grab=config.grab,
            hide_panel=config.hide_panel,
            extra_gamescope_args=config.extra_gamescope_args,
            game_args=config.game_args,
            extra_env=launch_env,
            goldberg_enabled=config.goldberg_enabled,
            goldberg_no_overlay=config.goldberg_no_overlay,
            goldberg_disable_lan=config.goldberg_disable_lan,
            goldberg_debug=config.goldberg_debug,
            goldberg_auto_join=config.goldberg_auto_join,
            goldberg_host_instance=config.goldberg_host_instance,
            osk_enabled=config.osk_enabled,
            exe_name=config.exe_name,
            launch_command=config.launch_command,
            kwin_keep_above=config.kwin_keep_above,
            kwin_accept_focus=config.kwin_accept_focus,
            kwin_disable_fsp=config.kwin_disable_fsp,
            render_scale=config.render_scale,
            upscale_filter=config.upscale_filter,
            upscale_sharpness=config.upscale_sharpness,
            input_mode=mode,
            fps_limit=config.fps_limit,
        )

    def _resolve_host_index(self, instances_config, host_instance: int):
        """Map the user's 1-based ``host_instance`` to a position in
        instances_config. Match by instance_number first, fall back to the
        positional index (host_instance - 1). Returns None if unmappable.
        """
        for i, cfg in enumerate(instances_config):
            if cfg.instance_number == host_instance:
                return i
        # Positional fallback (handles configs where instance_number is sparse)
        idx = host_instance - 1
        if 0 <= idx < len(instances_config):
            return idx
        return None

    def _build_worker(self, index: int, connect_lobby_args=None):
        """Construct an InstanceWorker + QThread pair for instances_config[index]."""
        c = self._launch_common
        cfg = c['instances_config'][index]
        rw, rh = render_size(cfg.gamescope_width, cfg.gamescope_height, c['render_scale'])
        resize.write_size(c['appid'], cfg.instance_number, rw, rh)
        nested = self._nested
        if nested:
            nested = (max(nested[0], rw), max(nested[1], rh))
        worker = InstanceWorker(
            instance_index=index,
            appid=c['appid'],
            controller=cfg.controller_index,
            instance_number=cfg.instance_number,
            isolation=c['isolation'],
            verbose=c['verbose'],
            nvidia=c['nvidia'],
            borderless=c['borderless'],
            copy_prefix=c['copy_prefix'],
            copy_userdata=c['copy_userdata'],
            force_grab_cursor=c['force_grab_cursor'],
            grab=c['grab'],
            gamescope_w=cfg.gamescope_width,
            gamescope_h=cfg.gamescope_height,
            pos_x=cfg.pos_x,
            pos_y=cfg.pos_y,
            extra_gamescope_args=c['extra_gamescope_args'],
            game_args=c['game_args'],
            extra_env={**c['extra_env'], **cfg.extra_env},
            goldberg_enabled=c['goldberg_enabled'],
            goldberg_no_overlay=c['goldberg_no_overlay'],
            goldberg_disable_lan=c['goldberg_disable_lan'],
            goldberg_debug=c['goldberg_debug'],
            connect_lobby_args=connect_lobby_args,
            num_players=c['num_players'],
            script=c.get('script'),
            exe_name=c['exe_name'],
            launch_command=c['launch_command'],
            wmclass=f'janus-p{index + 1}',
            rate=c['rate'],
            fps_limit=c.get('fps_limit'),
            render_scale=c['render_scale'],
            upscale_filter=c['upscale_filter'],
            upscale_sharpness=c['upscale_sharpness'],
            controller_vid=getattr(cfg, 'controller_vid', 0) or 0,
            controller_pid=getattr(cfg, 'controller_pid', 0) or 0,
            controller_name=getattr(cfg, 'controller_name', None),
            profile_id=c['profile_ids'][index],
            players=c['players'],
            input_nodes=(None if c.get('input_nodes') is None
                         # keyboardMouse: the game gets no pad, unless the
                         # handler keeps it (keepPad: pad + a few keys).
                         else [] if self._kbm_map and not self._kbm_map.get('keepPad')
                         else c['input_nodes'].get(cfg.instance_number)),
            nested=nested,
        )
        thread = QThread()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.started.connect(self.instanceStarted)
        worker.finished.connect(self._on_instance_finished)
        worker.error.connect(self._on_instance_error)
        # End the thread with its instance. Otherwise it idles in its event
        # loop until the next stop_all(), and quitting the GUI after the games
        # exited on their own destroyed it still running ("QThread: Destroyed
        # while thread is still running" -> abort).
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)
        return worker, thread

    def _maybe_dispatch_autojoin(self, instance_index: int, lobby_id: str):
        """Fire the queued non-host instances once the HOST reports its lobby.

        Ignore detections from non-host instances and ignore repeats (re-hosts) —
        the others have already launched, and we don't restart them on a new ID.
        """
        if instance_index == self._autojoin_host_index:
            self._host_lobby = lobby_id      # for players joining later
        if (self._autojoin_host_index is None or
                self._autojoin_started or
                instance_index != self._autojoin_host_index):
            return

        self._autojoin_started = True
        join_args = self._lobby_join_args(instance_index, lobby_id)
        from PySide6.QtCore import QTimer
        for stagger_pos, pending_idx in enumerate(self._pending_autojoin):
            w, t = self._build_worker(pending_idx, connect_lobby_args=join_args)
            self._workers.append(w)
            self._threads.append(t)
            # 3s apart so gamescope/pressure-vessel don't trample each other,
            # and a 1s initial delay to let the host's gamescope settle.
            QTimer.singleShot(1000 + stagger_pos * 3000, t.start)
        self._pending_autojoin = []

    def _lobby_join_args(self, instance_index: int, lobby_id: str) -> list:
        """Args appended to the queued instances' command line to join the
        host's lobby. Default '+connect_lobby <id>'; a handler script can
        replace the template (Game.lobbyConnectArg, %LOBBY% placeholder) or
        take over entirely via Game.onLobbyDetected — returning false
        suppresses the join args, returning an argv array replaces them."""
        template = '+connect_lobby %LOBBY%'
        script = self._script
        if script is not None and script.get('lobbyConnectArg'):
            template = str(script.get('lobbyConnectArg'))
        join_args = shlex.split(template.replace('%LOBBY%', str(lobby_id)))
        if script is not None and script.has_hook('onLobbyDetected'):
            c = self._launch_common
            r = script.invoke('onLobbyDetected', ctx={
                'appid': c['appid'], 'instance': instance_index + 1,
                'lobbyId': str(lobby_id), 'numPlayers': c['num_players'],
            }, soft=True)
            ret = (r or {}).get('ret')
            if ret is False:
                join_args = []
            elif isinstance(ret, list):
                join_args = [str(a) for a in ret]
        return join_args

    def _script_ctx_for(self, index: int) -> dict:
        """The per-instance ctx stashed on the worker's Popen (set by
        launch_steam_game), for exit hooks. Empty dict if unavailable."""
        for w in self._workers:
            if getattr(w, 'instance_index', None) == index:
                proc = getattr(w, '_proc', None)
                return getattr(proc, 'janus_script_ctx', None) or {}
        return {}

    def _on_instance_started_for_script(self, index: int, pid: int):
        if self._script is None:
            return
        self._script_started_pids[index] = pid
        if (len(self._script_started_pids) >= self._script_expected
                and not self._all_started_fired):
            self._all_started_fired = True
            release_soon()   # no tile holds the mouse until clicked
            c = self._launch_common
            self._script.invoke('onAllStarted', ctx={
                'appid': c['appid'], 'numPlayers': c['num_players'],
                'pids': list(self._script_started_pids.values()),
            }, soft=True)

    def _finish_script(self):
        """Fire onStop + HandlerScript.finish() exactly once per session
        (restores Janus.backup files, kills Janus.spawn helpers)."""
        if self._script is None or self._script_finished:
            return
        self._script_finished = True
        c = getattr(self, '_launch_common', {}) or {}
        self._script.invoke('onStop', ctx={
            'appid': c.get('appid'),
            'numPlayers': c.get('num_players'),
        }, soft=True)
        try:
            self._script.finish()
        except Exception as e:
            print(f'[launcher] script finish failed: {e}', flush=True)
        self._script = None

    def _on_instance_finished(self, index, rc):
        if self._script is not None:
            self._script.invoke(
                'onInstanceExit',
                ctx={**self._script_ctx_for(index), 'exitCode': rc},
                soft=True)
        print(f'[launcher] instance {index + 1} exited ({rc})', flush=True)
        self.instanceFinished.emit(index, rc)
        self._running_count -= 1
        self._cancel_autojoin_if_host_gone(index)
        if self._running_count <= 0:
            self._session_over()
        else:
            self._player_left(index)

    def _on_instance_error(self, index, msg):
        print(f'[launcher] instance {index + 1} failed: {msg}', flush=True)
        self.instanceError.emit(index, msg)
        self._running_count -= 1
        self._cancel_autojoin_if_host_gone(index)
        if self._running_count <= 0:
            self._session_over()
        else:
            self._player_left(index)

    # --- dynamic players --------------------------------------------------

    def _player_left(self, index):
        """An instance ended while others play on: release its pad (Janus
        input: it can join again) and, unless the handler says
        Game.reflow = false, re-flow the others' tiles."""
        self._stop_keeper(index)
        self._stop_kbm(index)
        if self._start_watch is not None:
            self._start_watch.remove_pad(index)
        if index not in self._order:
            return
        self._order.remove(index)
        cfg = self._launch_common['instances_config'][index]
        if self._osk_enabled:
            ctrl = self._osk_index_to_controller.pop(index, None)
            self._osk_expected = max(0, self._osk_expected - 1)
            if self._osk_pending.pop(ctrl, None) is not None:
                self._respawn_osk()
        if self._pad_daemon is not None:
            try:
                self._pad_daemon.remove_player(cfg.instance_number)
            except PadDaemonError as e:
                print(f'[launcher] P{cfg.instance_number}: pad not released: {e}', flush=True)
        if self._reflow:
            self._reflow_tiles()

    def _rects_for(self, n):
        if self.layout_for is not None:
            rects = self.layout_for(n)
            if rects and len(rects) >= n:
                return rects
        if not self._monitors:
            return None
        return layouts.default_rects(self._monitors, n, self._side_by_side_first)

    def _reflow_tiles(self):
        """Give the session's instances the tiles of the layout for their
        number, in their current visual order, and move the windows."""
        if not self._order:
            return
        rects = self._rects_for(len(self._order))
        if not rects:
            return
        configs = self._launch_common['instances_config']
        geometry, moved = {}, {}
        for index, (x, y, w, h, mon) in zip(self._order, rects):
            cfg = configs[index]
            cfg.pos_x, cfg.pos_y = x, y
            cfg.gamescope_width, cfg.gamescope_height = w, h
            cfg.monitor_name = mon
            geometry[index] = (x, y, w, h)
            moved[f'janus-p{index + 1}'] = (x, y, w, h)
        if self._wsession is not None:
            self._wsession.update_tiles(
                [ws.Tile(f'janus-p{i + 1}', configs[i].pos_x, configs[i].pos_y)
                 for i in self._order])
        ws.move_windows(moved)
        print('[launcher] re-flow: ' + ', '.join(
            f'P{configs[i].instance_number} {w}x{h}+{x}+{y}'
            for i, (x, y, w, h) in geometry.items()), flush=True)
        self._resize_games(geometry)
        self.tilesChanged.emit(geometry)

    def _resize_games(self, geometry):
        """Tell each game its new size (resize.py): the size file (read by
        the game's mod, or by its WindowKeeper for Game.resizeGame =
        "window") and the handler's onResize hook."""
        c = self._launch_common
        configs = c['instances_config']
        for index, (x, y, w, h) in geometry.items():
            cfg = configs[index]
            rw, rh = render_size(w, h, c['render_scale'])
            resize.write_size(c['appid'], cfg.instance_number, rw, rh)
            if self._script is not None and self._script.has_hook('onResize'):
                self._script.invoke('onResize', ctx={
                    **self._script_ctx_for(index), 'width': rw, 'height': rh,
                    'posX': x, 'posY': y, 'numPlayers': len(self._order)}, soft=True)

    def join_problem(self) -> str:
        """Why nobody can join the running session now ('' = they can)."""
        if self._running_count <= 0 or not hasattr(self, '_launch_common'):
            return 'No session is running.'
        if not self._late_join:
            return "This game's handler doesn't allow players to join mid-session."
        limit = layouts.MAX_PLAYERS
        try:
            if self._script is not None and self._script.get('maxPlayers'):
                limit = min(limit, int(self._script.get('maxPlayers')))
        except (TypeError, ValueError):
            pass
        if len(self._order) >= limit:
            return f'This game takes at most {limit} players.'
        return ''

    def active_instances(self) -> list:
        """The InstanceConfigs of the instances still in the session."""
        configs = self._launch_common['instances_config'] if hasattr(self, '_launch_common') else []
        return [configs[i] for i in self._order]

    def instance_config(self, index: int):
        """The InstanceConfig of instance *index* (joiners included)."""
        return self._launch_common['instances_config'][index]

    def free_instance_number(self) -> int:
        """The lowest instance number no running instance uses, never 1:
        handlers make instance 1 the host (it starts the server, owns the
        lobby), so a player joining after P1 left must not become one."""
        used = {c.instance_number for c in self.active_instances()}
        n = 2
        while n in used:
            n += 1
        return n

    def add_instance(self, cfg) -> int:
        """A player joins the running session: *cfg* (an InstanceConfig; its
        tile is decided here) gets a virtual pad and an instance, and the
        tiles re-flow to the layout for one more player. Returns the new
        instance's index. Raises ValueError with a reason."""
        problem = self.join_problem()
        if problem:
            raise ValueError(problem)
        c = self._launch_common
        configs = c['instances_config']
        if any(x.instance_number == cfg.instance_number for x in self.active_instances()):
            raise ValueError(f'Instance {cfg.instance_number} is still running.')
        active = self.active_instances()
        resolved = profiles.resolve_session(
            [(getattr(x, 'profile_id', None), x.instance_number) for x in active]
            + [(getattr(cfg, 'profile_id', None), cfg.instance_number)])
        prof = resolved[-1]
        from controllers import list_controllers
        ctrl = next((x for x in list_controllers() if x.index == cfg.controller_index), None)
        if ctrl is None:
            raise ValueError(f'No controller at index {cfg.controller_index}.')
        nodes = None
        if self._pad_daemon is not None:
            try:
                nodes = self._pad_daemon.add_player(cfg.instance_number, ctrl)
            except PadDaemonError as e:
                raise ValueError(f"The new player's controller couldn't be set up: {e}") from e
        else:
            # Steam Input: the hook finds the pad's Steam virtual pad by its
            # model (and the instance's rank among identical ones), which a
            # late joiner can't rely on when someone plays an identical pad.
            same = [x for x in active if (x.controller_vid, x.controller_pid)
                    == (ctrl.vendor_id, ctrl.product_id)]
            if same:
                raise ValueError(
                    f'Someone is already playing with an identical {ctrl.name}: with '
                    'Steam Input a second one can only join with Janus input.')

        index = len(configs)
        configs.append(cfg)
        c['profile_ids'].append(prof.id)
        c['players'] = [p for p in c['players']
                        if p['instance'] in {x.instance_number for x in active}]
        c['players'].append({'instance': cfg.instance_number, 'profile': prof.ctx()})
        if nodes is not None:
            c['input_nodes'][cfg.instance_number] = nodes
        self._order.append(index)
        c['num_players'] = len(self._order)
        self._watch_pad(index)
        self._start_kbm(index)
        self._running_count += 1
        self._script_expected += 1
        if self._osk_enabled:
            self._osk_expected += 1
            self._osk_index_to_controller[index] = (
                _js_number(nodes) if nodes is not None else cfg.controller_index)

        # Its tile (and everyone else's): the layout for one more player.
        self._reflow_tiles()
        if not cfg.gamescope_width:      # no layout: it can't be placed
            cfg.gamescope_width, cfg.gamescope_height = 1280, 720
            if self._wsession is not None:
                self._wsession.update_tiles(
                    [ws.Tile(f'janus-p{i + 1}', configs[i].pos_x, configs[i].pos_y)
                     for i in self._order])

        if c.get('goldberg_enabled'):
            self._lobby_watcher.add(index, goldberg_mirror_dir(c['appid'], cfg.instance_number))
        join_args = None
        if self._autojoin_host_index is not None and self._autojoin_host_index in self._order:
            if self._host_lobby:
                join_args = self._lobby_join_args(self._autojoin_host_index, self._host_lobby)
            elif not self._autojoin_started:
                self._pending_autojoin.append(index)    # launches with the others
                print(f'[launcher] P{cfg.instance_number} joins once the host has a lobby',
                      flush=True)
                self.instanceAdded.emit(index, cfg.instance_number)
                return index
        w, t = self._build_worker(index, connect_lobby_args=join_args)
        self._workers.append(w)
        self._threads.append(t)
        t.start()
        print(f'[launcher] P{cfg.instance_number} joined (instance index {index})', flush=True)
        self.instanceAdded.emit(index, cfg.instance_number)
        return index

    def join_pad(self, js: int, profile_id=None) -> int:
        """Controller *js* (controllers.py index) joins the running session as
        a new player (add_instance); *profile_id* None = the default profile
        for its instance number. Returns the instance index; ValueError."""
        if any(c.controller_index == js for c in self.active_instances()):
            raise ValueError('That controller is already playing.')
        from controllers import list_controllers
        ctrl = next((c for c in list_controllers() if c.index == js), None)
        if ctrl is None:
            raise ValueError(f'No controller at index {js}.')
        from ..models.config import InstanceConfig
        cfg = InstanceConfig(
            controller_index=js, instance_number=self.free_instance_number(),
            gamescope_width=0, gamescope_height=0, pos_x=0, pos_y=0,
            controller_vid=ctrl.vendor_id, controller_pid=ctrl.product_id,
            controller_name=ctrl.name, profile_id=profile_id)
        return self.add_instance(cfg)

    def stop_instance(self, index: int):
        """End one instance (a player removed from the session): its
        processes are killed; its exit then re-flows the others."""
        configs = self._launch_common['instances_config'] if hasattr(self, '_launch_common') else []
        if not 0 <= index < len(configs):
            return
        if index in self._pending_autojoin:          # never started
            self._pending_autojoin.remove(index)
            self._on_instance_finished(index, -9)
            return
        n = kill_instance_processes(f'janus-p{index + 1}')
        for w in self._workers:
            if w.instance_index == index and w._proc is not None and w._proc.poll() is None:
                try:
                    w._proc.kill()
                except OSError:
                    pass
                if getattr(w._proc, 'janus_cleanup', None):
                    w._proc.janus_cleanup()
        print(f'[launcher] P{configs[index].instance_number} removed '
              f'({n} process(es) killed)', flush=True)

    def _session_over(self):
        """Every instance has exited (or failed): end the session's helpers."""
        for i in list(self._keepers):
            self._stop_keeper(i)
        for i in list(self._kbm):
            self._stop_kbm(i)
        self._stop_start_watch()
        self._finish_script()
        self._stop_osk()
        self._restore_panel()
        self.allFinished.emit()

    def _cancel_autojoin_if_host_gone(self, finished_index: int):
        """If the auto-join host exits before its lobby is detected, drop the
        queued non-host instances — they have no lobby to join.
        Account for them in the running-count so allFinished still fires.
        """
        if (self._autojoin_host_index is None or
                self._autojoin_started or
                finished_index != self._autojoin_host_index or
                not self._pending_autojoin):
            return
        dropped = len(self._pending_autojoin)
        for i in self._pending_autojoin:
            if i in self._order:
                self._order.remove(i)
        self._pending_autojoin = []
        self._autojoin_host_index = None
        self._running_count -= dropped

    def _start_wayland_session(self, instances_config, hide_panel,
                               keep_above=False, accept_focus=True,
                               disable_fsp=True):
        """Apply the KWin Wayland Path A session: scale 1.0 + panel hide + one
        KWin rule per tile (positioned at its absolute native-px rect)."""
        outs = ws.get_outputs()
        primary = next((o for o in outs if o.primary), outs[0] if outs else None)
        names = {o.name for o in outs}
        tiles = [
            ws.Tile(f'janus-p{i + 1}', cfg.pos_x, cfg.pos_y)
            for i, cfg in enumerate(instances_config)
        ]
        # Flip scale on the output(s) the tiles live on (by monitor_name; else
        # the primary). Tiles span absolute desktop coords either way.
        used = [n for n in {cfg.monitor_name for cfg in instances_config
                            if cfg.monitor_name} if n in names]
        if not used and primary:
            used = [primary.name]
        self._wsession = ws.WaylandSession(
            tiles, scale_outputs=used, hide_panel=hide_panel,
            install_signal_handlers=False,
            keep_above=keep_above, accept_focus=accept_focus,
            disable_fsp=disable_fsp)
        self._wsession.setup()

    def _teardown_wayland_session(self):
        if self._wsession is not None:
            try:
                self._wsession.teardown()
            except Exception:
                pass
            self._wsession = None

    def _restore_panel(self):
        steam_input.release_layout()
        self._stop_pad_matcher()
        self._stop_pad_daemon()
        self._teardown_wayland_session()

    def _stop_pad_daemon(self):
        if self._pad_daemon is not None:
            self._pad_daemon.stop()
            self._pad_daemon = None

    def pad_status(self) -> dict:
        """Janus input: {player number (str): {attached, source, via, name}}
        from the input daemon ({} in Steam Input mode)."""
        return self._pad_daemon.status() if self._pad_daemon is not None else {}

    def _stop_pad_matcher(self):
        if self._pad_matcher is not None:
            self._pad_matcher.stop()
            self._pad_matcher = None

    def _on_instance_started_for_osk(self, instance_index: int, pid: int):
        """Collect (controller_index, pid) pairs for the OSK daemon. Spawn
        the daemon once every started instance has reported in."""
        if not self._osk_enabled:
            return
        ctrl = self._osk_index_to_controller.get(instance_index)
        if ctrl is None:
            return  # instance has no controller bound; can't summon OSK on it
        self._osk_pending[ctrl] = pid
        if len(self._osk_pending) >= self._osk_expected:
            if self._osk_proc is not None and self._osk_proc.poll() is None:
                self._respawn_osk()          # a player joined: new bindings
            else:
                self._spawn_osk()

    def _respawn_osk(self):
        """Restart the OSK daemon with the current bindings (dynamic
        players)."""
        proc, self._osk_proc = self._osk_proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        if self._osk_pending:
            self._spawn_osk()

    def _spawn_osk(self):
        """Launch the osk module as a subprocess with --bind args. Idempotent
        — does nothing if a daemon is already running."""
        if self._osk_proc is not None and self._osk_proc.poll() is None:
            return
        if ws.is_nested():
            # Game Mode (nested KWin): the OSK is X11-only (xdotool, override-
            # redirect windows); it can't see the tiles there, and it took the
            # nested KWin down with it. Steam's own keyboard (Steam + X) works.
            print('[launcher] osk: not in the nested KWin (use Steam + X)', flush=True)
            return
        args = ['python3', '-m', 'src.host.osk']
        for ctrl, pid in self._osk_pending.items():
            args.extend(['--bind', f'{ctrl}:{pid}'])
        try:
            self._osk_proc = subprocess.Popen(
                args,
                env=dict(os.environ, JANUS_OSK_VIRTUAL='1' if self._osk_virtual else '0'),
                cwd=str(Path(__file__).parent.parent.parent.parent),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,  # detach so stop_all controls it
            )
            print(f'[launcher] osk daemon started (pid {self._osk_proc.pid}); '
                  f'bindings: {self._osk_pending}', flush=True)
        except OSError as e:
            print(f'[launcher] failed to start osk daemon: {e}', flush=True)
            self._osk_proc = None

    def _stop_osk(self):
        if self._osk_proc is None:
            return
        if self._osk_proc.poll() is None:
            self._osk_proc.terminate()
            try:
                self._osk_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._osk_proc.kill()
        self._osk_proc = None
        self._osk_pending.clear()
        self._osk_expected = 0
        self._osk_enabled = False

    def stop_all(self):
        # A hook blocked on a Janus.warn/error dialog holds the script lock;
        # let it abort first so onStop/finish() can run.
        if self._script is not None:
            self._script.release_waiters()
        self._finish_script()
        self._lobby_watcher.stop()
        self._stop_osk()
        self._pending_autojoin = []
        self._autojoin_host_index = None
        self._autojoin_started = False
        # Force-kill the whole Janus-launched tree up front. terminate()/kill()
        # on each worker's tracked gamescope Popen does NOT reach gamescopereaper
        # or winedevice.exe (they double-fork and re-parent to init), so without
        # this sweep Stop All left orphaned gamescope/proton/wine processes alive.
        killed = _kill_janus_processes()
        if killed:
            print(f'[launcher] Stop All: force-killed {killed} '
                  f'Janus process(es)', flush=True)
        # Tidy up the tracked Popen objects + worker threads (their _proc.wait()
        # has now returned because the processes are dead).
        for worker in self._workers:
            worker.stop()
        for thread in self._threads:
            if thread.isRunning():
                thread.quit()
                thread.wait(5000)
        self._workers.clear()
        self._threads.clear()
        self._running_count = 0
        self._order = []
        for i in list(getattr(self, '_keepers', {})):
            self._stop_keeper(i)
        for i in list(getattr(self, '_kbm', {})):
            self._stop_kbm(i)
        self._stop_start_watch()
        self._restore_panel()

    def is_running(self) -> bool:
        return self._running_count > 0

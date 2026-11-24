#!/usr/bin/env python3
"""
Janus Split Screen Big Picture mode - fullscreen, controller-driven launcher.

Run with:  scripts/run_bigpicture.sh      (also the command to add to Steam)
    or:    python3 -m src.bigpicture.main [--windowed]
"""

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT))

# Env Steam sets for a non-Steam shortcut. Every game we launch inherits
# os.environ, so these must not leak; none of them exist when Janus runs from
# a terminal. The overlay preload/layer would be stacked under our hook, the
# shortcut's appid would shadow the game's, the SDL/Steam Input vars change how
# games enumerate pads, and the cache paths point at the shortcut's shader
# cache.
_STEAM_SHORTCUT_VARS = (
    'SteamAppId', 'SteamGameId', 'SteamOverlayGameId', 'SteamClientLaunch',
    'SteamEnv', 'SteamTenfoot', 'SteamGamepadUI', 'EnableConfiguratorSupport',
    'SteamVirtualGamepadInfo', 'SDL_GAMECONTROLLER_ALLOW_STEAM_VIRTUAL_GAMEPAD',
    'SDL_GAMECONTROLLER_IGNORE_DEVICES', 'SDL_JOYSTICK_HIDAPI_STEAMXBOX',
    'SDL_ENABLE_STEAM_SCREEN_KEYBOARD', 'SDL_VIDEO_X11_DGAMOUSE',
    'ENABLE_VK_LAYER_VALVE_steam_overlay_1', 'ENABLE_VK_LAYER_VALVE_steam_fossilize_1',
    'STEAM_COMPAT_MEDIA_PATH', 'STEAM_COMPAT_TRANSCODED_MEDIA_PATH',
    'STEAM_FOSSILIZE_DUMP_PATH', 'STEAM_FOSSILIZE_DUMP_PATH_READ_ONLY',
    'FOSSILIZE_APPLICATION_INFO_FILTER_PATH', 'DXVK_STATE_CACHE_PATH',
    '__GL_SHADER_DISK_CACHE_PATH', '__GL_SHADER_DISK_CACHE_APP_NAME',
    '__GL_SHADER_DISK_CACHE_READ_ONLY_APP_NAME', '__GL_SHADER_DISK_CACHE_SKIP_CLEANUP',
    'MESA_SHADER_CACHE_DIR', 'MESA_GLSL_CACHE_DIR', 'MESA_DISK_CACHE_READ_ONLY_FOZ_DBS',
    'MESA_DISK_CACHE_SINGLE_FILE', 'MESA_SHADER_CACHE_MAX_SIZE', 'MESA_GLSL_CACHE_MAX_SIZE',
    'AMD_VK_PIPELINE_CACHE_PATH', 'AMD_VK_PIPELINE_CACHE_FILENAME', 'AMD_VK_USE_PIPELINE_CACHE',
)


def _scrub_steam_env():
    for k in _STEAM_SHORTCUT_VARS:
        os.environ.pop(k, None)
    preload = [p for p in os.environ.get('LD_PRELOAD', '').replace(' ', ':').split(':')
               if p and 'gameoverlayrenderer' not in p]
    if preload:
        os.environ['LD_PRELOAD'] = ':'.join(preload)
    else:
        os.environ.pop('LD_PRELOAD', None)


def main():
    # SteamOS Game Mode (Steam's gamescope is the compositor, no KWin): run
    # ourselves again inside a nested KWin, which places the tiles. Before the
    # scrub, so the second run still sees the shortcut's SteamAppId.
    sys.path.insert(0, str(_ROOT / 'src' / 'host'))
    import nested_kwin
    if nested_kwin.should_wrap():
        nested_kwin.exec_wrapped([sys.executable, '-m', 'src.bigpicture.main', *sys.argv[1:]],
                                 cwd=str(_ROOT))

    # The Janus shortcut's appid, when Steam launched us (gone after the scrub).
    shortcut_appid = os.environ.get('SteamAppId', '')
    _scrub_steam_env()

    from PySide6.QtCore import QTimer, QUrl
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine

    from src.bigpicture.backend import Backend
    from src.bigpicture.gamepad import GamepadInput
    from src.bigpicture.setup_wizard import SetupWizard
    from src.bigpicture.input_mode import InputMode

    app = QGuiApplication(sys.argv)
    app.setApplicationName('Janus')
    app.setDesktopFileName('janus-bigpicture')

    backend = Backend()
    gamepad = GamepadInput()
    pad_setup = SetupWizard(gamepad)
    input_mode = InputMode()
    app.installEventFilter(input_mode)
    app.aboutToQuit.connect(backend.shutdown)
    app.aboutToQuit.connect(gamepad.shutdown)

    # Steam's "Exit game" sends SIGTERM: quit through Qt so shutdown() stops
    # the games and restores scale/panel/KWin rules. Python only runs signal
    # handlers between bytecodes, so wake the interpreter regularly.
    import signal
    # The handler can fire mid-callback: quit from the event loop instead.
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: QTimer.singleShot(0, app.quit))
    wake = QTimer()
    wake.timeout.connect(lambda: None)
    wake.start(250)

    engine = QQmlApplicationEngine()
    ctx = engine.rootContext()
    ctx.setContextProperty('backend', backend)
    ctx.setContextProperty('gamepad', gamepad)
    gamepad.startHeld.connect(backend.padHeldStart)   # join a running session
    gamepad.startInactive.connect(backend.padStartInactive)   # Start: back to Janus
    ctx.setContextProperty('padSetup', pad_setup)
    pad_setup.mappingsChanged.connect(backend.optionsChanged)   # rows show them
    ctx.setContextProperty('inputMode', input_mode)
    ctx.setContextProperty('windowed', '--windowed' in sys.argv)
    engine.load(QUrl.fromLocalFile(str(Path(__file__).parent / 'qml' / 'Main.qml')))
    if not engine.rootObjects():
        sys.exit(1)
    QTimer.singleShot(300, lambda: backend.checkStartup(
        int(shortcut_appid) if shortcut_appid.isdigit() else 0))
    if shortcut_appid:
        # Launched from Steam's Big Picture: ~0.6 s after our window appears
        # Steam activates its own window again (it can't see a Wayland window
        # as the game's), leaving the launcher behind it. Take the focus back
        # once it has done so.
        win = engine.rootObjects()[0]

        def refocus():
            if not win.isActive():
                print('[bigpicture] Steam took the focus at launch: taking it back',
                      flush=True)
                backend.activateWindow()
        QTimer.singleShot(1500, refocus)
        QTimer.singleShot(3000, refocus)
    rc = app.exec()
    # Destroy the QML first: otherwise Python may free backend/gamepad/
    # inputMode before it and every binding re-evaluates against null.
    del engine
    sys.exit(rc)


if __name__ == '__main__':
    main()

"""Session/activity detection + the Steam-Input focus warning.

Steam Input drives the (global) virtual pads only while a game window holds X input
focus (Steam polls XGetInputFocus; see NOTES.md). So we detect whether a game is the
focused X window and warn when Steam Input is in use but nothing is focused.
"""
from __future__ import annotations

import os

from .model import Activity, InputDevice, SteamController
from .steam_input_source import find_running_game_appid


def _steam_running() -> bool:
    try:
        entries = [e for e in os.listdir("/proc") if e.isdigit()]
    except OSError:
        return False
    for e in entries:
        try:
            with open(f"/proc/{e}/comm") as f:
                comm = f.read().strip()
        except OSError:
            continue
        if comm in ("steam", "steamwebhelper"):
            return True
    return False


def _pid_is_game(pid: int) -> bool:
    """Heuristic: a game process carries a real SteamAppId, or runs under steamapps, or
    is a gamescope/reaper wrapper."""
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            env = f.read()
        for tok in env.split(b"\0"):
            if tok.startswith(b"SteamAppId="):
                v = tok[len(b"SteamAppId="):].decode(errors="replace").strip()
                if v and v not in ("480", "0"):
                    return True
    except OSError:
        pass
    try:
        exe = os.readlink(f"/proc/{pid}/exe")
        if "steamapps/common" in exe or "gamescope" in exe or exe.endswith("/reaper"):
            return True
    except OSError:
        pass
    try:
        with open(f"/proc/{pid}/comm") as f:
            if f.read().strip() in ("gamescope", "gamescopereaper", "reaper"):
                return True
    except OSError:
        pass
    return False


def focused_window_is_game() -> bool | None:
    """Is the X-focused window a game/gamescope window? None if we can't tell (no X)."""
    try:
        from Xlib import display  # python-xlib (optional dep)
    except Exception:
        return None
    try:
        dpy = display.Display()
        win = dpy.get_input_focus().focus
        if not win or isinstance(win, int):
            return False
        pid_atom = dpy.intern_atom("_NET_WM_PID")
        # walk up: focus may be a child; check the window and its ancestors for a PID
        cur = win
        for _ in range(6):
            try:
                p = cur.get_full_property(pid_atom, 0)
            except Exception:
                p = None
            if p and p.value:
                return _pid_is_game(int(p.value[0]))
            try:
                cur = cur.query_tree().parent
            except Exception:
                break
            if not cur or isinstance(cur, int):
                break
        return False
    except Exception:
        return None
    finally:
        try:
            dpy.close()
        except Exception:
            pass


def compute(devices: list[InputDevice], steam_controllers: list[SteamController],
            ipc_available: bool, ipc_error: str | None) -> Activity:
    steam_running = _steam_running()
    appid = find_running_game_appid()
    has_virtual = any(d.is_virtual for d in devices)
    steam_input_active = has_virtual or bool(steam_controllers)
    focused = focused_window_is_game()

    level, msg = "ok", ""
    if not steam_running:
        level, msg = "error", "Steam is not running — start Steam and launch a game with Steam Input enabled."
    elif not steam_input_active:
        level, msg = ("warn",
                      "No active Steam Input session — Virtual/IPC columns are empty until a "
                      "game with Steam Input enabled is running.")
    elif focused is False:
        level, msg = ("warn",
                      "A game is running but NO game window is focused — Steam Input won't feed the "
                      "controllers. Click a game window to receive input.")
    elif not ipc_available:
        level, msg = ("warn",
                      f"Virtual pads present; ISteamInput column disabled ({ipc_error or 'libsteam_api.so not found'}).")

    return Activity(steam_running=steam_running, game_appid=appid, game_focused=focused,
                    steam_input_active=steam_input_active, ipc_available=ipc_available,
                    level=level, message=msg)

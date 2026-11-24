"""ISteamInput IPC client via Valve's flat C API (ctypes) — the "native game" view.

Loads the real libsteam_api.so dynamically and enumerates the controllers Steam Input
exposes. Visibility is per-AppId: we must init with the RUNNING game's appid (480 -> 0
controllers). Init is lazy — deferred until a game is detected — because SteamAPI_Init
binds the appid for the process lifetime. See NOTES.md / INPUT_PROBE.md.
"""
from __future__ import annotations

import ctypes
import os

from . import steam_locate
from .model import SteamController

STEAM_INPUT_MAX_COUNT = 16
_INPUT_TYPE = {
    0: "Unknown", 1: "SteamController", 2: "XBox360", 3: "XBoxOne",
    4: "GenericGamepad", 5: "PS4", 6: "AppleMFi", 7: "Android",
    8: "SwitchJoyConPair", 9: "SwitchJoyConSingle", 10: "SwitchPro",
    11: "MobileTouch", 12: "PS3", 13: "PS5", 14: "SteamDeck",
}


def find_running_game_appid() -> str | None:
    """A running Steam game's AppId via /proc/<pid>/environ (skip 480/0 and ourselves)."""
    self_pid = str(os.getpid())
    try:
        entries = os.listdir("/proc")
    except OSError:
        return None
    for entry in entries:
        if not entry.isdigit() or entry == self_pid:
            continue
        try:
            with open(f"/proc/{entry}/environ", "rb") as f:
                env = f.read()
        except OSError:
            continue
        for tok in env.split(b"\0"):
            if tok.startswith(b"SteamAppId="):
                val = tok[len(b"SteamAppId="):].decode(errors="replace").strip()
                if val and val not in ("480", "0"):
                    return val
    return None


class SteamInputSource:
    """Lazy ISteamInput client. `poll()` returns [] until a game is running + init OK."""

    def __init__(self) -> None:
        self.lib_path = steam_locate.find_libsteam_api()
        self.available = self.lib_path is not None
        self.inited = False
        self.appid: str | None = None
        self.error: str | None = None if self.available else "libsteam_api.so not found"
        self._lib = None
        self._inp = None
        self._handles = (ctypes.c_uint64 * STEAM_INPUT_MAX_COUNT)()

    def _try_init(self) -> bool:
        if self.inited or not self.available:
            return self.inited
        appid = find_running_game_appid()
        if not appid:
            return False  # no game yet — nothing to bind to
        os.environ["SteamAppId"] = os.environ["SteamGameId"] = appid
        try:
            lib = ctypes.CDLL(self.lib_path)
            lib.SteamAPI_InitFlat.restype = ctypes.c_int
            lib.SteamAPI_InitFlat.argtypes = [ctypes.c_char_p]
            lib.SteamAPI_SteamInput_v007.restype = ctypes.c_void_p
            lib.SteamAPI_ISteamInput_Init.argtypes = [ctypes.c_void_p, ctypes.c_bool]
            lib.SteamAPI_ISteamInput_Init.restype = ctypes.c_bool
            lib.SteamAPI_ISteamInput_RunFrame.argtypes = [ctypes.c_void_p, ctypes.c_bool]
            lib.SteamAPI_ISteamInput_GetConnectedControllers.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint64)]
            lib.SteamAPI_ISteamInput_GetConnectedControllers.restype = ctypes.c_int
            lib.SteamAPI_ISteamInput_GetInputTypeForHandle.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
            lib.SteamAPI_ISteamInput_GetInputTypeForHandle.restype = ctypes.c_int
            lib.SteamAPI_ISteamInput_GetGamepadIndexForController.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
            lib.SteamAPI_ISteamInput_GetGamepadIndexForController.restype = ctypes.c_int
            lib.SteamAPI_Shutdown.argtypes = []
            errbuf = ctypes.create_string_buffer(1024)
            rc = lib.SteamAPI_InitFlat(errbuf)
            if rc != 0:
                self.error = f"SteamAPI_InitFlat rc={rc}: {errbuf.value.decode(errors='replace')}"
                return False
            inp = lib.SteamAPI_SteamInput_v007()
            if not inp:
                self.error = "SteamInput_v007 returned NULL"
                lib.SteamAPI_Shutdown()
                return False
            lib.SteamAPI_ISteamInput_Init(inp, True)
            self._lib, self._inp, self.appid = lib, inp, appid
            self.inited = True
            self.error = None
        except OSError as e:
            self.error = f"load failed: {e}"
            return False
        return self.inited

    def poll(self) -> list[SteamController]:
        if not self._try_init():
            return []
        lib, inp = self._lib, self._inp
        lib.SteamAPI_ISteamInput_RunFrame(inp, True)
        n = lib.SteamAPI_ISteamInput_GetConnectedControllers(inp, self._handles)
        out = []
        for i in range(n):
            h = self._handles[i]
            t = lib.SteamAPI_ISteamInput_GetInputTypeForHandle(inp, h)
            gp = lib.SteamAPI_ISteamInput_GetGamepadIndexForController(inp, h)
            out.append(SteamController(handle=h, input_type=_INPUT_TYPE.get(t, str(t)),
                                       gamepad_index=gp))
        return out

    def close(self) -> None:
        if self.inited and self._lib is not None:
            try:
                self._lib.SteamAPI_Shutdown()
            except Exception:
                pass
            self.inited = False

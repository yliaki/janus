#!/usr/bin/env python3
"""SteamAppId=X si_probe.py LIB SECS: how many pads ISteamInput reports to app X."""
import ctypes, sys, time
lib = ctypes.CDLL(sys.argv[1])
P = ctypes.c_void_p
for n in ('SteamAPI_Init', 'SteamAPI_RunCallbacks', 'SteamAPI_Shutdown'):
    getattr(lib, n).restype = ctypes.c_bool if n == 'SteamAPI_Init' else None
ver = [n for n in ('SteamAPI_SteamInput_v006', 'SteamAPI_SteamInput_v005', 'SteamAPI_SteamInput_v001') if hasattr(lib, n)][0]
f = getattr(lib, ver); f.restype = P
lib.SteamAPI_ISteamInput_Init.argtypes = [P, ctypes.c_bool]; lib.SteamAPI_ISteamInput_Init.restype = ctypes.c_bool
lib.SteamAPI_ISteamInput_RunFrame.argtypes = [P, ctypes.c_bool]
lib.SteamAPI_ISteamInput_GetConnectedControllers.argtypes = [P, ctypes.POINTER(ctypes.c_uint64)]
lib.SteamAPI_ISteamInput_GetConnectedControllers.restype = ctypes.c_int
lib.SteamAPI_ISteamInput_GetInputTypeForHandle.argtypes = [P, ctypes.c_uint64]
lib.SteamAPI_ISteamInput_GetInputTypeForHandle.restype = ctypes.c_int
print('init', lib.SteamAPI_Init(), ver)
si = f(); print('Init input', lib.SteamAPI_ISteamInput_Init(si, False))
h = (ctypes.c_uint64 * 16)()
end = time.time() + float(sys.argv[2])
best = -1
while time.time() < end:
    lib.SteamAPI_RunCallbacks(); lib.SteamAPI_ISteamInput_RunFrame(si, False)
    n = lib.SteamAPI_ISteamInput_GetConnectedControllers(si, h)
    if n != best:
        best = n; print('controllers:', n, [(hex(h[i]), lib.SteamAPI_ISteamInput_GetInputTypeForHandle(si, h[i])) for i in range(n)], flush=True)
    time.sleep(0.1)
lib.SteamAPI_Shutdown()

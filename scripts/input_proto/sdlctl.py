"""Minimal ctypes SDL2 binding shared by the prototypes."""
import ctypes
import ctypes.util

SDL_INIT_GAMECONTROLLER = 0x2000
CONTROLLERAXISMOTION, CONTROLLERBUTTONDOWN, CONTROLLERBUTTONUP = 0x650, 0x651, 0x652
CONTROLLERDEVICEADDED, CONTROLLERDEVICEREMOVED = 0x653, 0x654
JOYDEVICEADDED, JOYDEVICEREMOVED = 0x605, 0x606


class Event(ctypes.Union):
    _fields_ = [('type', ctypes.c_uint32), ('raw', ctypes.c_uint8 * 56)]

    def i32(self, off):
        return int.from_bytes(bytes(self.raw[off:off + 4]), 'little', signed=True)

    def i16(self, off):
        return int.from_bytes(bytes(self.raw[off:off + 2]), 'little', signed=True)

    def u8(self, off):
        return self.raw[off]


class GUID(ctypes.Structure):
    _fields_ = [('data', ctypes.c_uint8 * 16)]


def load():
    sdl = ctypes.CDLL(ctypes.util.find_library('SDL2-2.0') or 'libSDL2-2.0.so.0')
    P, I, C, V = ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p, None
    sig = {
        'SDL_SetHint': (I, [C, C]), 'SDL_Init': (I, [ctypes.c_uint32]),
        'SDL_GetError': (C, []), 'SDL_NumJoysticks': (I, []),
        'SDL_IsGameController': (I, [I]), 'SDL_JoystickNameForIndex': (C, [I]),
        'SDL_JoystickPathForIndex': (C, [I]),
        'SDL_JoystickGetDeviceInstanceID': (ctypes.c_int32, [I]),
        'SDL_JoystickGetDeviceVendor': (ctypes.c_uint16, [I]),
        'SDL_JoystickGetDeviceProduct': (ctypes.c_uint16, [I]),
        'SDL_GameControllerOpen': (P, [I]), 'SDL_GameControllerClose': (V, [P]),
        'SDL_GameControllerName': (C, [P]), 'SDL_GameControllerMapping': (C, [P]),
        'SDL_GameControllerGetButton': (ctypes.c_uint8, [P, I]),
        'SDL_GameControllerGetAxis': (ctypes.c_int16, [P, I]),
        'SDL_GameControllerGetPlayerIndex': (I, [P]),
        'SDL_GameControllerRumble': (I, [P, ctypes.c_uint16, ctypes.c_uint16, ctypes.c_uint32]),
        'SDL_GameControllerHasRumble': (ctypes.c_int, [P]),
        'SDL_GameControllerEventState': (I, [I]),
        'SDL_WaitEventTimeout': (I, [ctypes.POINTER(Event), I]),
        'SDL_GameControllerGetJoystick': (P, [P]),
        'SDL_JoystickInstanceID': (ctypes.c_int32, [P]),
        'SDL_GameControllerFromInstanceID': (P, [ctypes.c_int32]),
    }
    for name, (res, args) in sig.items():
        f = getattr(sdl, name)
        f.restype, f.argtypes = res, args
    return sdl

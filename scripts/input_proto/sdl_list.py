#!/usr/bin/env python3
"""What a game's SDL2 sees: list pads, then print controller events for N s.
    sdl_list.py [SECONDS]"""
import ctypes, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sdlctl

sdl = sdlctl.load()
sdl.SDL_SetHint(b'SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS', b'1')
sdl.SDL_SetHint(b'SDL_NO_SIGNAL_HANDLERS', b'1')
sdl.SDL_Init(sdlctl.SDL_INIT_GAMECONTROLLER)
sdl.SDL_GameControllerEventState(1)
n = sdl.SDL_NumJoysticks()
print('joysticks:', n)
gcs = []
for i in range(n):
    print(f'  [{i}] {sdl.SDL_JoystickNameForIndex(i).decode()} path={sdl.SDL_JoystickPathForIndex(i)} '
          f'{sdl.SDL_JoystickGetDeviceVendor(i):04x}:{sdl.SDL_JoystickGetDeviceProduct(i):04x} '
          f'gamecontroller={sdl.SDL_IsGameController(i)}')
    if sdl.SDL_IsGameController(i):
        gc = sdl.SDL_GameControllerOpen(i)
        gcs.append(gc)
        print('      mapping:', sdl.SDL_GameControllerMapping(gc).decode()[:90])
NAMES = 'a b x y back guide start l3 r3 lb rb up down left right'.split()
ev = sdlctl.Event()
end = time.time() + float(sys.argv[1] if len(sys.argv) > 1 else 0)
while time.time() < end:
    if sdl.SDL_WaitEventTimeout(ctypes.byref(ev), 50):
        if ev.type in (sdlctl.CONTROLLERBUTTONDOWN, sdlctl.CONTROLLERBUTTONUP):
            b = ev.u8(12)
            print(f'  ev pad{ev.i32(8)} button {NAMES[b] if b < len(NAMES) else b} {"down" if ev.u8(13) else "up"}', flush=True)
        elif ev.type == sdlctl.CONTROLLERAXISMOTION:
            print(f'  ev pad{ev.i32(8)} axis {ev.u8(12)} = {ev.i16(16)}', flush=True)

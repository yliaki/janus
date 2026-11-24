#!/usr/bin/env python3
"""Hold REAL Steam session tickets for a Goldberg'd game (Game.goldbergRealTicket).

Runs natively on the host, next to the game: loads a Valve libsteam_api.so,
connects to the user's running Steam client as <appid>, and writes one fresh
session ticket to each --out file. The ticket proxy DLL
(src/ticketproxy/, installed as steam_api64.dll in the Goldberg mirror) hands
that ticket to the game instead of Goldberg's fake one, so backends that
check tickets with Valve (Content Warning's Photon app) accept the instance.

The process then stays up — a session ticket is only valid while the Steam
client that issued it keeps it open — and exits (cancelling the tickets) when
--watch-pid dies or on SIGTERM/SIGINT.

    python3 steam_ticket.py --appid 2881650 --watch-pid 1234 --out a.bin [--out b.bin]
"""

import argparse
import ctypes
import os
import signal
import sys
import time
from pathlib import Path

# Interface accessors across SDK versions; any one that the lib exports works.
_USER_ACCESSORS = ['SteamAPI_SteamUser_v023', 'SteamAPI_SteamUser_v022',
                   'SteamAPI_SteamUser_v021', 'SteamAPI_SteamUser_v020']


def _is_elf64(path: Path) -> bool:
    try:
        with open(path, 'rb') as fh:
            hdr = fh.read(5)
    except OSError:
        return False
    return hdr[:4] == b'\x7fELF' and hdr[4] == 2


def find_real_libsteam_api() -> Path:
    """A 64-bit Valve libsteam_api.so from any installed Steam game.

    Any version will do: it only needs the flat ISteamUser accessors, and the
    ticket is issued by the Steam client, not the library. Goldberg mirrors
    live outside the Steam libraries, so this never picks up a fake one."""
    sys.path.insert(0, str(Path(__file__).parent))
    from steam import find_all_steam_roots, parse_vdf
    libs = []
    for root in find_all_steam_roots():
        vdf = parse_vdf(root / 'steamapps' / 'libraryfolders.vdf') or {}
        libs.append(root)
        for folder in vdf.get('libraryfolders', {}).values():
            if isinstance(folder, dict) and folder.get('path'):
                libs.append(Path(folder['path']))
    seen = set()
    for lib in libs:
        common = lib / 'steamapps' / 'common'
        if common in seen or not common.is_dir():
            continue
        seen.add(common)
        for cand in sorted(common.rglob('libsteam_api.so')):
            if _is_elf64(cand):
                return cand
    raise SystemExit('steam_ticket: no 64-bit libsteam_api.so found in any '
                     'Steam library (install any native Linux Steam game, '
                     'or pass --lib)')


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--appid', required=True)
    ap.add_argument('--out', action='append', required=True,
                    help='ticket file to write (repeatable; one ticket each)')
    ap.add_argument('--watch-pid', type=int,
                    help='exit when this process is gone')
    ap.add_argument('--lib', help='Valve libsteam_api.so to use')
    a = ap.parse_args()

    # Identify as the game to the Steam client: the ticket is bound to it.
    os.environ['SteamAppId'] = os.environ['SteamGameId'] = str(a.appid)
    lib_path = Path(a.lib) if a.lib else find_real_libsteam_api()
    lib = ctypes.CDLL(str(lib_path))
    lib.SteamAPI_Init.restype = ctypes.c_bool
    if not lib.SteamAPI_Init():
        raise SystemExit('steam_ticket: SteamAPI_Init failed — is Steam '
                         'running and logged in to an account that owns '
                         f'app {a.appid}?')
    accessor = next((getattr(lib, n) for n in _USER_ACCESSORS
                     if hasattr(lib, n)), None)
    if accessor is None:
        raise SystemExit(f'steam_ticket: {lib_path} has no SteamUser accessor')
    accessor.restype = ctypes.c_void_p
    user = accessor()
    get_ticket = lib.SteamAPI_ISteamUser_GetAuthSessionTicket
    # The 5th (SteamNetworkingIdentity*) arg exists since SDK 1.57; older libs
    # ignore it, so passing NULL is safe either way.
    get_ticket.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
                           ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
    get_ticket.restype = ctypes.c_uint32
    cancel = lib.SteamAPI_ISteamUser_CancelAuthTicket
    cancel.argtypes = [ctypes.c_void_p, ctypes.c_uint32]

    pending = []
    for out in a.out:
        buf = (ctypes.c_ubyte * 2048)()
        n = ctypes.c_uint32()
        handle = get_ticket(user, buf, len(buf), ctypes.byref(n), None)
        if not handle or not n.value:
            raise SystemExit('steam_ticket: GetAuthSessionTicket failed')
        pending.append((Path(out), bytes(buf[:n.value]), handle))

    # Steam registers a new ticket with its backend asynchronously
    # (GetAuthSessionTicketResponse_t); a ticket validated before that lands
    # is rejected. ctypes can't easily register the callback, so pump
    # callbacks for a moment before publishing the tickets.
    for _ in range(20):
        lib.SteamAPI_RunCallbacks()
        time.sleep(0.1)
    for out, data, handle in pending:
        tmp = out.with_suffix('.tmp')
        tmp.write_bytes(data)
        tmp.replace(out)
        print(f'[janus] steam_ticket: real ticket ({len(data)} bytes, '
              f'handle {handle}) -> {out}', flush=True)

    stop = False

    def _stop(*_):
        nonlocal stop
        stop = True
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    while not stop:
        lib.SteamAPI_RunCallbacks()
        if a.watch_pid:
            try:
                os.kill(a.watch_pid, 0)
            except ProcessLookupError:
                break
        time.sleep(1)

    for out, _, handle in pending:
        cancel(user, handle)
        out.unlink(missing_ok=True)
    lib.SteamAPI_Shutdown()


if __name__ == '__main__':
    main()

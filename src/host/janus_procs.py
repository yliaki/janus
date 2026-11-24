"""Find and kill every process a Janus session launched.

gamescopereaper and winedevice.exe double-fork and re-parent to init, so a
tracked Popen (terminate(), killpg) misses them. Every process Janus launches
carries a marker in its environment instead, and the sweep goes by that.
"""

import os
import signal
import time

# Marker present in the LD_PRELOAD / WINE_LD_PRELOAD of *every* process Janus
# launches (gamescope, the pressure-vessel + Proton + wine tree, winedevice.exe,
# and the game itself). We kill by this marker rather than by the tracked Popen
# pid because gamescopereaper and winedevice.exe double-fork and re-parent to
# init — they escape terminate()/kill() on the Popen and os.killpg() on its
# group, which is why "Stop All" used to leave orphaned gamescope/proton/wine
# processes running.
JANUS_PROC_MARKER = b'libjanus'


def janus_launched_pids(marker: bytes = JANUS_PROC_MARKER) -> list[int]:
    """Return the pids of every live process whose environment contains *marker*
    (i.e. every process Janus injected its hook into). Robust to re-parenting.

    Zombies are skipped automatically: the kernel clears a dead task's environ,
    so the marker won't be found and they won't be re-listed."""
    me = os.getpid()
    pids: list[int] = []
    try:
        entries = os.listdir('/proc')
    except OSError:
        return pids
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == me:
            continue
        try:
            with open(f'/proc/{pid}/environ', 'rb') as fh:
                if marker in fh.read():
                    pids.append(pid)
        except OSError:
            # process gone, or not ours (permission) — skip
            continue
    return pids


def kill_janus_processes(marker: bytes = JANUS_PROC_MARKER,
                          timeout: float = 2.0) -> int:
    """SIGKILL every Janus-launched process, looping until none remain (or
    *timeout*) so a child that gets re-parented mid-sweep can't slip through.

    Returns the number of SIGKILLs sent. All matching pids in a pass are killed
    before re-scanning, so there's no parent-then-child window for escape."""
    killed = 0
    deadline = time.monotonic() + timeout
    while True:
        pids = janus_launched_pids(marker)
        if not pids:
            break
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
                killed += 1
            except OSError:
                pass  # already gone
        if time.monotonic() > deadline:
            break
        time.sleep(0.1)  # let the kernel reap before re-scanning
    return killed


def kill_instance_processes(wmclass: str, timeout: float = 2.0) -> int:
    """SIGKILL one instance's processes: the Janus-launched ones whose
    environment also names its tile (SDL_VIDEO_X11_WMCLASS=janus-p<N>, set on
    its gamescope and inherited by everything under it). Returns how many."""
    tile = f'SDL_VIDEO_X11_WMCLASS={wmclass}\0'.encode()
    me = os.getpid()
    killed = 0
    deadline = time.monotonic() + timeout
    while True:
        pids = []
        for entry in os.listdir('/proc'):
            if not entry.isdigit() or int(entry) == me:
                continue
            try:
                with open(f'/proc/{entry}/environ', 'rb') as fh:
                    env = fh.read()
            except OSError:
                continue
            if JANUS_PROC_MARKER in env and tile in env:
                pids.append(int(entry))
        if not pids:
            break
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
                killed += 1
            except OSError:
                pass
        if time.monotonic() > deadline:
            break
        time.sleep(0.1)
    return killed

"""Start on a playing controller brings the game tiles back.

When another window has taken the screen or the keyboard (an accidental
Alt+Tab, a notification, the launcher), pressing Start on any controller of
the session raises every game tile again and gives the keyboard focus to the
presser's tile, so nobody needs the keyboard to get back. While a tile is the
active window, Start does nothing here (it is only the game's Start).

The session's pads are read through evdev without grabbing them: in Janus
input each player's virtual Xbox pad (padd.py), in Steam Input mode the
physical pad. The press still reaches the game (usually its pause menu).
"""

import os
import select
import struct
import threading
import time
from typing import Callable, Dict

BTN_START = 0x13b
EV_KEY = 1
_EVENT = struct.Struct('llHHi')

# One-shot KWin script: if the active window isn't a tile, un-minimize and
# raise every tile (activating raises), the presser's tile last so it gets
# the keyboard.
_REFOCUS_JS = """\
function isTile(w) { return ("" + w.resourceClass).indexOf("janus-p") === 0; }
var act = workspace.activeWindow;
if (!(act && isTile(act))) {
    var tiles = [], mine = null;
    var ws = workspace.stackingOrder;
    for (var i = 0; i < ws.length; i++) {
        if (!isTile(ws[i])) continue;
        if (("" + ws[i].resourceClass) === "%(mine)s") mine = ws[i];
        else tiles.push(ws[i]);
    }
    if (mine) tiles.push(mine);
    // Activating alone doesn't raise a window above the one that had the
    // focus (seen with a terminal over a tile): raise explicitly as well.
    for (var j = 0; j < tiles.length; j++) {
        tiles[j].minimized = false;
        workspace.raiseWindow(tiles[j]);
        workspace.activeWindow = tiles[j];
    }
    print("janus: Start pressed, " + tiles.length + " tile(s) brought back");
}
"""


def refocus_tiles(wmclass: str) -> bool:
    """Bring the tiles back (see _REFOCUS_JS); *wmclass* = the presser's."""
    import wayland_session as ws
    return ws.kwin_run_script(_REFOCUS_JS % {'mine': wmclass}, name='janus-refocus')


class StartWatcher:
    """Watches the session's pads for Start; calls on_start(key) for the pad
    registered under *key*. Pads come and go with set_pad()/remove_pad()."""

    def __init__(self, on_start: Callable[[object], None]):
        self.on_start = on_start
        self._pads: Dict[object, str] = {}       # key -> /dev/input/eventN
        self._fds: Dict[int, object] = {}        # open fd -> key
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._last = 0.0
        self._thread = threading.Thread(target=self._run, daemon=True, name='start-watch')
        self._thread.start()

    def set_pad(self, key, node: str):
        with self._lock:
            self._pads[key] = node

    def remove_pad(self, key):
        with self._lock:
            self._pads.pop(key, None)

    def stop(self):
        self._stop.set()

    def _sync(self):
        """Open the pads that aren't open yet, close the ones removed."""
        with self._lock:
            want = dict(self._pads)
        for fd, key in list(self._fds.items()):
            if want.get(key) is None:
                os.close(fd)
                del self._fds[fd]
        open_keys = set(self._fds.values())
        for key, node in want.items():
            if key in open_keys or not node:
                continue
            try:
                fd = os.open(node, os.O_RDONLY | os.O_NONBLOCK)
            except OSError:
                continue                  # not there yet / no access: next time
            self._fds[fd] = key

    def _run(self):
        next_sync = 0.0
        while not self._stop.is_set():
            if time.monotonic() >= next_sync:
                self._sync()
                next_sync = time.monotonic() + 1.0
            if not self._fds:
                self._stop.wait(0.5)
                continue
            try:
                ready, _, _ = select.select(list(self._fds), [], [], 0.5)
            except (OSError, ValueError):
                next_sync = 0.0
                continue
            for fd in ready:
                try:
                    data = os.read(fd, _EVENT.size * 32)
                except BlockingIOError:
                    continue
                except OSError:               # pad gone: reopened by _sync if it's back
                    os.close(fd)
                    self._fds.pop(fd, None)
                    continue
                for off in range(0, len(data) - _EVENT.size + 1, _EVENT.size):
                    _, _, t, code, val = _EVENT.unpack_from(data, off)
                    if t == EV_KEY and code == BTN_START and val == 1:
                        now = time.monotonic()
                        if now - self._last > 1.0:
                            self._last = now
                            try:
                                self.on_start(self._fds.get(fd))
                            except Exception as e:  # noqa: BLE001
                                print(f'[janus] Start watcher: {e}', flush=True)
        for fd in list(self._fds):
            os.close(fd)
        self._fds.clear()

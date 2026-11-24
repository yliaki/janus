"""Lobby ID watcher for gbe_fork (Goldberg) debug logs.

The gbe_fork debug build periodically writes lines like:

    [<ms> ms, <us> us] [tid <N>] Steam_Matchmaking::send_lobby_data lobby 109775245384293432

for every lobby the local instance OWNS (i.e. is hosting). We tail each
instance's STEAM_LOG_*.log file, extract the lobby ID, and emit a signal
when it changes. The line repeats every SEND_LOBBY_RATE so detection is
robust to launch races and to the user re-hosting later in the session.

Release builds of gbe_fork have no logging (dbg_log is
compiled out via EMU_RELEASE_BUILD), so the user MUST be running the
debug build for this to produce anything. The watcher silently does
nothing if no log file appears.

The watcher uses QTimer polling rather than inotify because the log file
is created lazily inside a pressure-vessel container, the random suffix
isn't known up-front, and we have to glob for it.
"""

import re
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal


# Matches the gbe_fork debug log line emitted periodically (every
# SEND_LOBBY_RATE ~5s) for every lobby this instance OWNS:
#
#   [56816 ms, ...] [tid 308] Steam_Matchmaking::send_lobby_data() lobby 109775241779135130
#
# Note the literal `()` after the function name — EMU_FUNC_NAME on Linux
# includes the parentheses. The leading timestamp and tid prefix are
# unconstrained; we just anchor on the function name + "lobby <id>".
_LOBBY_LINE_RE = re.compile(
    r'Steam_Matchmaking::send_lobby_data\(\)\s+lobby\s+(\d{6,})'
)

# Lines from CreateLobby ack are noisier (no lobby_id in them), so we rely
# on send_lobby_data exclusively. send_lobby_data is called every
# SEND_LOBBY_RATE (~5s in gbe_fork) so worst-case detection latency is ~5s.

_POLL_INTERVAL_MS = 1000


class _InstanceTail:
    """State for tailing a single instance's gbe_fork log file."""

    def __init__(self, mirror_dir: Path):
        self.mirror_dir = mirror_dir
        self._log_path: Path | None = None
        self._offset: int = 0
        self._last_lobby_id: str | None = None

    def _find_log_path(self) -> Path | None:
        """Locate the active STEAM_LOG_*.log file under the mirror directory.

        gbe_fork writes one log per emulator load. We always set GseAppPath
        to <mirror>/_janus_gbe_logs/ in launch_steam_game so the file lands
        there; on relaunch a new random-suffixed log appears alongside the
        old one. Pick the newest mtime so we follow the active emulator.
        """
        pinned = self.mirror_dir / '_janus_gbe_logs'
        if not pinned.exists():
            return None
        candidates = list(pinned.glob('STEAM_LOG_*.log'))
        if not candidates:
            return None

        latest = max(candidates, key=lambda p: p.stat().st_mtime)
        if self._log_path != latest:
            # New log file (game was relaunched mid-session). Restart offset
            # so we re-scan from the start; clear last_lobby_id so the first
            # lobby line in the new file emits a fresh signal even if the ID
            # happens to match the previous one.
            self._log_path = latest
            self._offset = 0
            self._last_lobby_id = None
        return self._log_path

    def poll(self) -> str | None:
        """Read new bytes from the log; return new lobby ID iff it changed."""
        path = self._find_log_path()
        if path is None:
            return None

        try:
            size = path.stat().st_size
        except OSError:
            return None

        if size < self._offset:
            # Log was truncated / replaced. Restart.
            self._offset = 0
            self._last_lobby_id = None

        if size == self._offset:
            return None

        try:
            with open(path, 'rb') as f:
                f.seek(self._offset)
                chunk = f.read(size - self._offset)
        except OSError:
            return None

        self._offset = size

        # Parse the latest matching line in this chunk. If host re-hosts
        # mid-chunk, we want the newest (last) lobby ID.
        latest_match: str | None = None
        try:
            text = chunk.decode('utf-8', errors='replace')
        except UnicodeDecodeError:
            return None

        for m in _LOBBY_LINE_RE.finditer(text):
            latest_match = m.group(1)

        if latest_match is None:
            return None
        if latest_match == self._last_lobby_id:
            return None

        self._last_lobby_id = latest_match
        return latest_match


class LobbyWatcher(QObject):
    """Watches each running instance's gbe_fork debug log for lobby IDs.

    Emits ``lobbyDetected(instance_index, lobby_id)`` when an instance's
    log first reveals a hosted lobby — or when the host re-hosts and a new
    lobby ID supersedes the previous one. Designed to be safe to start
    even when only some instances host (non-host instances just never
    emit a signal).
    """

    lobbyDetected = Signal(int, str)  # instance_index, lobby_id

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tails: dict[int, _InstanceTail] = {}
        self._timer = QTimer(self)
        self._timer.setInterval(_POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._tick)

    def start(self, mirror_dirs: dict[int, Path]):
        """Begin polling. ``mirror_dirs`` maps instance_index → mirror Path.

        Stale logs from a PREVIOUS session are deleted first. The watcher is
        started before the host worker wipes+rebuilds its mirror (that
        happens seconds later, in the worker thread), so without this the
        very first poll reads last session's STEAM_LOG, "detects" a lobby
        that no longer exists, and the non-host instances get launched with
        a dead ``+connect_lobby`` ID (seen with How to Fish; NOTES.md).
        """
        self.stop()
        for mirror in mirror_dirs.values():
            logs = mirror / '_janus_gbe_logs'
            if logs.is_dir():
                for stale in logs.glob('STEAM_LOG_*.log'):
                    try:
                        stale.unlink()
                    except OSError:
                        pass
        self._tails = {i: _InstanceTail(p) for i, p in mirror_dirs.items()}
        if self._tails:
            self._timer.start()

    def add(self, index: int, mirror: Path):
        """Watch one more instance (a player who joined mid-session)."""
        logs = mirror / '_janus_gbe_logs'
        if logs.is_dir():
            for stale in logs.glob('STEAM_LOG_*.log'):
                try:
                    stale.unlink()
                except OSError:
                    pass
        self._tails[index] = _InstanceTail(mirror)
        if not self._timer.isActive():
            self._timer.start()

    def stop(self):
        self._timer.stop()
        self._tails.clear()

    def _tick(self):
        for i, tail in list(self._tails.items()):
            new_id = tail.poll()
            if new_id is not None:
                self.lobbyDetected.emit(i, new_id)

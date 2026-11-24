"""Qt model for connected controllers with auto-refresh."""

import sys
from pathlib import Path
from PySide6.QtCore import Qt, QAbstractListModel, QModelIndex, QTimer

sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'host'))
from controllers import list_controllers


class ControllerModel(QAbstractListModel):
    """List model of connected controllers, auto-refreshes every 3 seconds."""

    IndexRole = Qt.UserRole + 1
    NameRole = Qt.UserRole + 2
    VidRole = Qt.UserRole + 3
    PidRole = Qt.UserRole + 4

    def __init__(self, parent=None):
        super().__init__(parent)
        self._controllers = []
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(3000)

    def refresh(self):
        new_controllers = list_controllers()
        # Only reset if the list actually changed
        new_key = [(c.index, c.name) for c in new_controllers]
        old_key = [(c.index, c.name) for c in self._controllers]
        if new_key != old_key:
            self.beginResetModel()
            self._controllers = new_controllers
            self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return len(self._controllers)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or index.row() >= len(self._controllers):
            return None
        ctrl = self._controllers[index.row()]
        if role == Qt.DisplayRole:
            return f"{ctrl.name} ({ctrl.index})"
        if role == self.IndexRole:
            return ctrl.index
        if role == self.NameRole:
            return ctrl.name
        if role == self.VidRole:
            return ctrl.vendor_id
        if role == self.PidRole:
            return ctrl.product_id
        return None

    def get_controller_index(self, row: int) -> int | None:
        if 0 <= row < len(self._controllers):
            return self._controllers[row].index
        return None

    def find_row_by_index(self, controller_index: int) -> int | None:
        for i, ctrl in enumerate(self._controllers):
            if ctrl.index == controller_index:
                return i
        return None

    def find_by_identity(self, name: str, vid: int = 0, pid: int = 0,
                         exclude: set[int] | None = None,
                         uniq: str = "", port: str = "") -> int | None:
        """Find a controller by stable identity, returning its kernel index.

        Preference: uniq (serial/MAC, unique) > USB port path (stable per port, the
        identical-pad tie-breaker) > VID:PID > name. Falls back through the chain so old
        configs (VID:PID only) still resolve. `exclude` skips already-assigned indices."""
        skip = exclude or set()

        def _pick(pred):
            for ctrl in self._controllers:
                if ctrl.index not in skip and pred(ctrl):
                    return ctrl.index
            return None

        if uniq:
            hit = _pick(lambda c: c.uniq == uniq)
            if hit is not None:
                return hit
        if port:
            hit = _pick(lambda c: c.port == port
                        and (not vid or c.vendor_id == vid)
                        and (not pid or c.product_id == pid))
            if hit is not None:
                return hit
        if vid and pid:
            hit = _pick(lambda c: c.vendor_id == vid and c.product_id == pid)
            if hit is not None:
                return hit
        if name:
            return _pick(lambda c: c.name == name)
        return None

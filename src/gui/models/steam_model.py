"""Qt model for installed Steam games."""

import sys
from pathlib import Path
from PySide6.QtCore import Qt, QAbstractListModel, QModelIndex, QSortFilterProxyModel

sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'host'))
from steam import get_installed_games
import scripting


class SteamGameModel(QAbstractListModel):
    """List model of installed Steam games, plus the non-Steam games that
    have a handler (Game.id: Janus sets them up itself)."""

    AppidRole = Qt.UserRole + 1
    NameRole = Qt.UserRole + 2

    def __init__(self, parent=None):
        super().__init__(parent)
        self._games = []

    def refresh(self):
        self.beginResetModel()
        try:
            self._games = get_installed_games()
        except FileNotFoundError:
            self._games = []
        self._games += [{'appid': gid, 'name': scripting.handler_name(p) or gid}
                        for gid, p in scripting.non_steam_handlers().items()]
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return len(self._games)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or index.row() >= len(self._games):
            return None
        game = self._games[index.row()]
        if role == Qt.DisplayRole:
            return f"{game['name']}  ({game['appid']})"
        if role == self.AppidRole:
            return game['appid']
        if role == self.NameRole:
            return game['name']
        return None

    def get_appid(self, index: QModelIndex) -> int | str | None:
        if not index.isValid():
            return None
        return self._games[index.row()]['appid']

    def get_name(self, index: QModelIndex) -> str | None:
        if not index.isValid():
            return None
        return self._games[index.row()]['name']

    def find_index_by_appid(self, appid: int) -> QModelIndex | None:
        for i, game in enumerate(self._games):
            if game['appid'] == appid:
                return self.index(i)
        return None


class SteamGameFilterModel(QSortFilterProxyModel):
    """Filterable proxy for steam games - filters by name or appid."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFilterCaseSensitivity(Qt.CaseInsensitive)

    def filterAcceptsRow(self, source_row, source_parent):
        model = self.sourceModel()
        index = model.index(source_row, 0, source_parent)
        text = self.filterRegularExpression().pattern()
        if not text:
            return True
        display = model.data(index, Qt.DisplayRole) or ''
        return text.lower() in display.lower()

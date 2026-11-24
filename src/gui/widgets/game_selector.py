"""Game selector widget - shows configured games with Add/Remove buttons."""

from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QPushButton, QLabel, QDialog, QLineEdit, QListView, QDialogButtonBox,
)

from ..models.steam_model import SteamGameModel, SteamGameFilterModel
from ..models.presets import PresetManager


class AddGameDialog(QDialog):
    """Dialog to pick a Steam game from all installed games."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Game")
        self.resize(400, 500)

        layout = QVBoxLayout(self)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search installed games...")
        self._search.setClearButtonEnabled(True)
        layout.addWidget(self._search)

        self._model = SteamGameModel(self)
        self._filter = SteamGameFilterModel(self)
        self._filter.setSourceModel(self._model)

        self._list = QListView()
        self._list.setModel(self._filter)
        layout.addWidget(self._list)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._search.textChanged.connect(self._filter.setFilterFixedString)
        self._list.doubleClicked.connect(self.accept)

        self._model.refresh()

        self.appid = None
        self.game_name = None

    def accept(self):
        index = self._list.currentIndex()
        if index.isValid():
            source = self._filter.mapToSource(index)
            self.appid = self._model.get_appid(source)
            self.game_name = self._model.get_name(source)
            super().accept()

    @staticmethod
    def pick_game(parent=None):
        """Show dialog and return (appid, name) or (None, None)."""
        dlg = AddGameDialog(parent)
        if dlg.exec() == QDialog.Accepted and dlg.appid is not None:
            return dlg.appid, dlg.game_name
        return None, None


class GameSelector(QWidget):
    """List of configured games with Add/Remove. Emits gameSelected(appid, name)."""

    # appid: a Steam appid, or a non-Steam game's id (str)
    gameSelected = Signal(object, str)  # appid, name
    gameAdded = Signal(object, str)     # appid, name — after adding a new game
    gameRemoved = Signal()           # current game was removed
    hubRequested = Signal()          # "Get handlers…": open the handler hub

    def __init__(self, presets: PresetManager, parent=None):
        super().__init__(parent)
        self._presets = presets

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        layout.addWidget(QLabel("Games"))

        self._list = QListWidget()
        layout.addWidget(self._list, stretch=1)

        btn_layout = QHBoxLayout()
        btn_layout.setContentsMargins(0, 0, 0, 0)
        self._add_btn = QPushButton("+ Add")
        self._remove_btn = QPushButton("- Remove")
        btn_layout.addWidget(self._add_btn)
        btn_layout.addWidget(self._remove_btn)
        layout.addLayout(btn_layout)
        self._hub_btn = QPushButton("Get handlers…")
        self._hub_btn.setToolTip("Search the handler hub, get handlers for your Steam "
                                 "library, update the ones you have")
        self._hub_btn.clicked.connect(self.hubRequested)
        layout.addWidget(self._hub_btn)

        self._list.currentItemChanged.connect(self._on_item_changed)
        self._add_btn.clicked.connect(self._on_add)
        self._remove_btn.clicked.connect(self._on_remove)

        self._refresh_list()

    def set_update_count(self, n: int):
        """Say on the hub button how many handler updates the hub has."""
        self._hub_btn.setText(f"Get handlers… ({n} update{'s' if n != 1 else ''})"
                              if n else "Get handlers…")

    def _refresh_list(self):
        """Reload list from config directory."""
        self._list.clear()
        for game in self._presets.list_games():
            item = QListWidgetItem(f"{game['name']}  ({game['appid']})")
            item.setData(Qt.UserRole, game['appid'])
            item.setData(Qt.UserRole + 1, game['name'])
            self._list.addItem(item)

    def _on_item_changed(self, current, _previous):
        if current is None:
            return
        appid = current.data(Qt.UserRole)
        name = current.data(Qt.UserRole + 1)
        if appid is not None:
            self.gameSelected.emit(appid, name)

    def _on_add(self):
        appid, name = AddGameDialog.pick_game(self)
        if appid is None:
            return

        # Check if already configured
        if self._presets.game_has_config(appid):
            # Just select existing entry
            self._select_appid(appid)
            return

        # Create default config with game_name
        from ..models.config import SessionConfig
        config = SessionConfig(appid=appid, game_name=name, overrides=set())
        self._presets.save_game(appid, config)

        # Add to list and select
        item = QListWidgetItem(f"{name}  ({appid})")
        item.setData(Qt.UserRole, appid)
        item.setData(Qt.UserRole + 1, name)
        self._list.addItem(item)
        self._list.setCurrentItem(item)

        self.gameAdded.emit(appid, name)

    def _on_remove(self):
        item = self._list.currentItem()
        if item is None:
            return
        appid = item.data(Qt.UserRole)
        self._presets.delete_game(appid)
        # Block signals so the auto-save in _on_game_selected doesn't
        # recreate the just-deleted config file
        self._list.blockSignals(True)
        self._list.takeItem(self._list.row(item))
        self._list.blockSignals(False)
        # Clear main window's appid so auto-save doesn't recreate the deleted file
        self.gameRemoved.emit()
        # Then select next item if any
        if self._list.count() > 0:
            self._list.setCurrentRow(0)

    def _select_appid(self, appid: int):
        """Programmatically select a game by appid."""
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item.data(Qt.UserRole) == appid:
                self._list.setCurrentItem(item)
                return

    def select_appid(self, appid: int):
        """Public API - select a game by appid without emitting gameSelected."""
        self._list.blockSignals(True)
        self._select_appid(appid)
        self._list.blockSignals(False)

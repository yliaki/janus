"""Lobby ID display panel.

Shows the most recently detected gbe_fork lobby ID for each running instance,
with a Copy-to-clipboard button. Hidden when there are no instances or when
Goldberg is disabled. Detection requires the gbe_fork debug build —
release builds emit no logs.
"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QLabel, QPushButton, QFrame,
)


class _InstanceLobbyRow(QWidget):
    """One row: 'Inst N — Lobby <id> [Copy]' or 'Inst N — (no lobby yet)'."""

    def __init__(self, index: int, parent=None):
        super().__init__(parent)
        self._index = index
        self._lobby_id: str | None = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(6)

        self._label = QLabel(f"Inst {index + 1}: (no lobby yet)")
        self._label.setStyleSheet("color: #888;")
        self._label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self._label, stretch=1)

        self._copy_btn = QPushButton("Copy")
        self._copy_btn.setFixedWidth(60)
        self._copy_btn.setEnabled(False)
        self._copy_btn.setToolTip("Copy the lobby ID to clipboard")
        self._copy_btn.clicked.connect(self._on_copy)
        layout.addWidget(self._copy_btn)

    def set_lobby_id(self, lobby_id: str):
        self._lobby_id = lobby_id
        self._label.setText(f"Inst {self._index + 1}: Lobby {lobby_id}")
        self._label.setStyleSheet("color: #4CAF50; font-family: monospace;")
        self._copy_btn.setEnabled(True)

    def clear(self):
        self._lobby_id = None
        self._label.setText(f"Inst {self._index + 1}: (no lobby yet)")
        self._label.setStyleSheet("color: #888;")
        self._copy_btn.setEnabled(False)

    def _on_copy(self):
        if self._lobby_id is None:
            return
        QGuiApplication.clipboard().setText(self._lobby_id)
        original = self._copy_btn.text()
        self._copy_btn.setText("Copied")
        # Reset label after a short delay (without requiring a QTimer member).
        from PySide6.QtCore import QTimer
        QTimer.singleShot(900, lambda: self._copy_btn.setText(original))


class LobbyPanel(QFrame):
    """Container showing one row per instance with detected lobby IDs."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.StyledPanel)
        self._rows: list[_InstanceLobbyRow] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 4, 6, 4)
        outer.setSpacing(2)

        self._header = QLabel("Goldberg lobby IDs")
        self._header.setStyleSheet("font-weight: bold; color: #ccc;")
        outer.addWidget(self._header)

        self._hint = QLabel(
            "Detection needs the debug gbe_fork build "
            "(vendor/goldberg/debug/, 'Use debug gbe_fork build' option)."
        )
        self._hint.setStyleSheet("color: #888; font-size: 10px;")
        self._hint.setWordWrap(True)
        outer.addWidget(self._hint)

        self._rows_layout = QVBoxLayout()
        self._rows_layout.setSpacing(2)
        outer.addLayout(self._rows_layout)

        self.hide()

    def set_instances(self, n: int, debug_active: bool):
        """Rebuild rows for ``n`` instances. ``debug_active`` toggles the hint
        about needing the debug build (greyed when satisfied)."""
        for row in self._rows:
            self._rows_layout.removeWidget(row)
            row.deleteLater()
        self._rows.clear()

        for i in range(n):
            row = _InstanceLobbyRow(i)
            self._rows_layout.addWidget(row)
            self._rows.append(row)

        if n > 0:
            self.show()
            if debug_active:
                self._hint.setText(
                    "Detection active. The host's lobby ID will appear here "
                    "within ~5 s of starting a lobby in the game."
                )
                self._hint.setStyleSheet("color: #4CAF50; font-size: 10px;")
            else:
                self._hint.setText(
                    "Detection idle — enable 'Use debug gbe_fork build' to "
                    "capture lobby IDs from gbe_fork's log."
                )
                self._hint.setStyleSheet("color: #888; font-size: 10px;")
        else:
            self.hide()

    def set_lobby_id(self, instance_index: int, lobby_id: str):
        if 0 <= instance_index < len(self._rows):
            self._rows[instance_index].set_lobby_id(lobby_id)

    def clear_all(self):
        for row in self._rows:
            row.clear()

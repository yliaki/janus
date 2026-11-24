"""Process bar - running instance status + Launch/Stop buttons."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QPushButton, QLabel, QFrame, QMenu,
)


class ProcessBar(QWidget):
    """Bottom bar showing running instances with Launch All / Stop All."""

    launchRequested = Signal()
    dryRunRequested = Signal()
    stopRequested = Signal()
    # Right-click an instance's label → Remove (dynamic players): its index.
    removeRequested = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        self._launch_btn = QPushButton("Launch All")
        self._launch_btn.setFixedWidth(100)
        self._launch_btn.setStyleSheet("QPushButton { background: #4CAF50; color: white; font-weight: bold; padding: 6px; }")
        layout.addWidget(self._launch_btn)

        self._dryrun_btn = QPushButton("Dry Run")
        self._dryrun_btn.setFixedWidth(80)
        layout.addWidget(self._dryrun_btn)

        self._status_area = QHBoxLayout()
        layout.addLayout(self._status_area)

        self._status_labels: list[QLabel] = []

        layout.addStretch()

        hotkey_hint = QLabel("Alt+1: release cursor · Alt+2: stop all")
        hotkey_hint.setStyleSheet("color: #888; padding-right: 8px;")
        hotkey_hint.setToolTip(
            "Global hotkeys active while a session is running:\n"
            "  Alt+1 — release mouse cursor from the focused gamescope window\n"
            "  Alt+2 — stop all instances"
        )
        layout.addWidget(hotkey_hint)

        self._stop_btn = QPushButton("Stop All  (Alt+2)")
        self._stop_btn.setFixedWidth(130)
        self._stop_btn.setStyleSheet("QPushButton { background: #f44336; color: white; font-weight: bold; padding: 6px; }")
        self._stop_btn.setToolTip("Stop all running instances (global hotkey: Alt+2)")
        self._stop_btn.setEnabled(False)
        layout.addWidget(self._stop_btn)

        self._launch_btn.clicked.connect(self.launchRequested)
        self._dryrun_btn.clicked.connect(self.dryRunRequested)
        self._stop_btn.clicked.connect(self.stopRequested)

    def set_instance_count(self, n: int):
        # Clear existing labels
        for lbl in self._status_labels:
            self._status_area.removeWidget(lbl)
            lbl.deleteLater()
        self._status_labels.clear()

        for i in range(n):
            self._add_label(i)

    def _add_label(self, i: int, text: str = ''):
        lbl = QLabel(text or f"Inst {i+1}: idle")
        lbl.setStyleSheet("padding: 2px 8px; border: 1px solid #555; border-radius: 3px;")
        lbl.setContextMenuPolicy(Qt.CustomContextMenu)
        lbl.customContextMenuRequested.connect(
            lambda pos, i=i, lbl=lbl: self._label_menu(i, lbl, pos))
        lbl.setToolTip("Right-click to remove this player from the session")
        self._status_area.addWidget(lbl)
        self._status_labels.append(lbl)

    def _label_menu(self, i, lbl, pos):
        if not self._stop_btn.isEnabled():     # no session running
            return
        menu = QMenu(self)
        act = menu.addAction(f"Remove player {i + 1} (closes their game)")
        if menu.exec(lbl.mapToGlobal(pos)) is act:
            self.removeRequested.emit(i)

    def add_instance(self, index: int, number: int):
        """A player joined mid-session: a label for instance *index*."""
        while len(self._status_labels) <= index:
            self._add_label(len(self._status_labels), f"P{number}: starting")

    def set_instance_started(self, index: int, pid: int):
        if 0 <= index < len(self._status_labels):
            self._status_labels[index].setText(f"Inst {index+1}: PID {pid}")
            self._status_labels[index].setStyleSheet(
                "padding: 2px 8px; border: 1px solid #4CAF50; border-radius: 3px; color: #4CAF50;"
            )

    def set_instance_finished(self, index: int, return_code: int):
        if 0 <= index < len(self._status_labels):
            self._status_labels[index].setText(f"Inst {index+1}: exited ({return_code})")
            self._status_labels[index].setStyleSheet(
                "padding: 2px 8px; border: 1px solid #888; border-radius: 3px; color: #888;"
            )

    def set_instance_error(self, index: int, msg: str):
        if 0 <= index < len(self._status_labels):
            self._status_labels[index].setText(f"Inst {index+1}: error")
            self._status_labels[index].setToolTip(msg)
            self._status_labels[index].setStyleSheet(
                "padding: 2px 8px; border: 1px solid #f44336; border-radius: 3px; color: #f44336;"
            )

    def set_running(self, running: bool):
        self._launch_btn.setEnabled(not running)
        self._stop_btn.setEnabled(running)

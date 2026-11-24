#!/usr/bin/env python3
"""
Janus GUI - entry point.

Run with:  python -m src.gui.main   (from project root)
    or:    python src/gui/main.py
"""

import sys
from pathlib import Path

# Make sure the project root and host modules are importable
_project_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_project_root))
sys.path.insert(0, str(_project_root / 'src' / 'host'))

from PySide6.QtWidgets import QApplication, QMessageBox

from src.gui.main_window import MainWindow
import prereqs


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Janus")
    app.setStyle("Fusion")

    window = MainWindow()
    window.show()

    # Nothing can be launched without gamescope; say so straight away.
    problem = prereqs.gamescope_problem()
    if problem:
        QMessageBox.critical(window, "gamescope is needed", problem)
    if not prereqs.steam_running():
        QMessageBox.warning(window, "Steam not running", prereqs.STEAM_NOT_RUNNING)

    sys.exit(app.exec())


if __name__ == '__main__':
    main()

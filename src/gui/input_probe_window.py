"""GUI frontend for the input probe (PySide6), over the shared ProbeSession.

Owns only the event loop + rendering: a QSocketNotifier per evdev fd feeds live input,
a QTimer polls Steam Input, re-enumerates on hot-plug, and repaints. All device/Steam
logic lives in src.host.input_probe. Run:  python3 -m src.gui.input_probe_window
"""
from __future__ import annotations

import html
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from PySide6.QtCore import QSocketNotifier, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QFont  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QGroupBox, QHBoxLayout, QLabel, QVBoxLayout, QWidget)

from src.host.input_probe import ProbeSession  # noqa: E402

_LEVEL_STYLE = {
    "ok": "background:#1b5e20; color:#e8f5e9;",
    "warn": "background:#e65100; color:#fff3e0;",
    "error": "background:#b71c1c; color:#ffebee;",
}
_PHYS_COL = "#4fc3f7"
_VIRT_COL = "#ffd54f"
_HELD_COL = "#ff5252"


def _device_html(dev, col) -> str:
    name = html.escape(dev.name[:26])
    if dev.pressed:
        held = " ".join(f'<span style="color:{_HELD_COL}">{html.escape(b)}</span>'
                         for b in sorted(dev.pressed))
    else:
        held = '<span style="color:#666">—</span>'
    last = html.escape(dev.last_label) if dev.last_label else ""
    return (f'<div style="margin:2px 0">'
            f'<span style="color:{col}"><b>{dev.event}/{dev.js}</b></span> '
            f'<span style="color:#aaa">{dev.vid}:{dev.pid}</span> {name}<br>'
            f'&nbsp;&nbsp;held: [{held}] <span style="color:#777">{last}</span></div>')


class InputProbeWindow(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Janus — Input Probe")
        self.resize(920, 520)
        self.session = ProbeSession()
        self._notifiers: dict[int, QSocketNotifier] = {}

        root = QVBoxLayout(self)
        self.banner = QLabel()
        self.banner.setWordWrap(True)
        self.banner.setMargin(8)
        f = self.banner.font()
        f.setBold(True)
        self.banner.setFont(f)
        root.addWidget(self.banner)

        cols = QHBoxLayout()
        mono = QFont("monospace")
        mono.setStyleHint(QFont.StyleHint.TypeWriter)
        self.phys_lbl, phys_box = self._column("Physical evdev (our view)", mono)
        self.virt_lbl, virt_box = self._column("Virtual evdev (Steam)", mono)
        self.ipc_lbl, ipc_box = self._column("ISteamInput (IPC)", mono)
        for b in (phys_box, virt_box, ipc_box):
            cols.addWidget(b)
        root.addLayout(cols)

        self.map_lbl, map_box = self._column(
            "Mapping — expected controller → Steam virtual → IPC handle "
            "(press each pad to verify)", mono)
        root.addWidget(map_box)

        self._sync_notifiers()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(100)
        self._ticks = 0
        self._repaint()

    def _column(self, title, font):
        box = QGroupBox(title)
        lay = QVBoxLayout(box)
        lbl = QLabel()
        lbl.setFont(font)
        lbl.setTextFormat(Qt.TextFormat.RichText)
        lbl.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        lbl.setWordWrap(True)
        lay.addWidget(lbl)
        lay.addStretch(1)
        return lbl, box

    def _sync_notifiers(self) -> None:
        fds = self.session.fds()
        for fd in list(self._notifiers):
            if fd not in fds:
                n = self._notifiers.pop(fd)
                n.setEnabled(False)
                n.deleteLater()
        for fd in fds:
            if fd not in self._notifiers:
                n = QSocketNotifier(fd, QSocketNotifier.Type.Read, self)
                n.activated.connect(lambda *_a, fd=fd: self.session.pump(fd))
                self._notifiers[fd] = n

    def _tick(self) -> None:
        self._ticks += 1
        if self._ticks % 2 == 0:          # ~5 Hz Steam poll
            self.session.poll_steam()
        if self._ticks % 10 == 0:         # ~1 Hz hot-plug re-enumerate
            self.session.refresh()
            self._sync_notifiers()
        self._repaint()

    def _repaint(self) -> None:
        snap = self.session.snapshot()
        a = snap.activity
        style = _LEVEL_STYLE.get(a.level, "")
        head = (f"Steam: {'running' if a.steam_running else 'not running'}  |  "
                f"game: {a.game_appid or '—'}  |  "
                f"focused: {'yes' if a.game_focused else ('no' if a.game_focused is False else '?')}  |  "
                f"Steam Input: {'active' if a.steam_input_active else 'idle'}")
        self.banner.setStyleSheet(style)
        self.banner.setText(head + (f"\n⚠ {a.message}" if a.message else ""))

        phys = [d for d in snap.devices if not d.is_virtual]
        virt = [d for d in snap.devices if d.is_virtual]
        self.phys_lbl.setText("".join(_device_html(d, _PHYS_COL) for d in phys)
                              or '<span style="color:#666">(none)</span>')
        self.virt_lbl.setText("".join(_device_html(d, _VIRT_COL) for d in virt)
                              or '<span style="color:#666">(none — start a Steam-Input game)</span>')
        if snap.steam_controllers:
            self.ipc_lbl.setText("".join(
                f'<div style="margin:2px 0">handle=0x{c.handle:016x}<br>'
                f'&nbsp;&nbsp;type={html.escape(c.input_type)} '
                f'gamepad_index={c.gamepad_index}</div>'
                for c in snap.steam_controllers))
        else:
            reason = "" if a.ipc_available else f"<br><span style='color:#777'>{html.escape(self.session.steam.error or '')}</span>"
            self.ipc_lbl.setText(f'<span style="color:#666">(no controllers){reason}</span>')

        self.map_lbl.setText(self._mapping_html(snap))

    @staticmethod
    def _mapping_html(snap) -> str:
        if not snap.mapping:
            return '<span style="color:#666">(no physical controllers)</span>'
        marks = {
            "ok": '<span style="color:#69f0ae">✓ verified</span>',
            "mismatch": '<span style="color:#ff5252"><b>✗ MISMATCH — order guess is wrong</b></span>',
            "unverified": '<span style="color:#888">? press pad to verify</span>',
        }
        rows = []
        for m in snap.mapping:
            hnd = f"0x{m.handle:016x}" if m.handle is not None else "—"
            gp = m.gamepad_index if m.gamepad_index is not None else "—"
            rows.append(
                f'<div style="margin:3px 0">'
                f'<span style="color:{_PHYS_COL}"><b>{m.phys_js}/{m.phys_event}</b></span> '
                f'{m.phys_id} <span style="color:#888">port {html.escape(m.phys_port)}</span> '
                f'&nbsp;→ expected <b>{m.expected_virtual or "—"}</b>, '
                f'detected <span style="color:{_VIRT_COL}"><b>{m.detected_virtual or "—"}</b></span>'
                f' <span style="color:#888">(votes {m.votes})</span> '
                f'&nbsp;→ gp_index {gp}, handle {hnd} &nbsp; {marks[m.status]}</div>')
        return "".join(rows)

    def closeEvent(self, ev):
        self.timer.stop()
        self.session.close()
        super().closeEvent(ev)


def main() -> None:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    w = InputProbeWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

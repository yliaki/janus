"""Handler hub browser for the desktop GUI (src/host/hub.py).

Search the hub (live, as you type), sort it (most installed first), show
only the games in your Steam library, and install one or several handlers,
every missing handler for your library ("Get all my games") or every update.
Every install is confirmed first with the version, its uploader and the hub
page where the source is readable. Network calls run on a worker thread;
results come back through a queued signal.
"""

import threading
import webbrowser

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMessageBox, QPushButton, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout,
)

import hub

WHERE = {'installed': 'Installed', 'library': 'In library, not installed',
         'store': 'Not in your library', 'janus': 'Not on Steam'}


def update_keys() -> list:
    """Keys of the games whose hub handler has a newer version (raises
    hub.HubError). Nothing to ask the hub when it installed nothing."""
    local = hub.installed()
    if not local:
        return []
    return [hub.game_key(g) for g in hub.list_games() if hub.status(g, local) == 'update']


def _status_text(r: dict) -> str:
    needs = f" · v{r['latest_version']} needs Janus {r['needs']}" if r.get('needs') else ''
    if r['status'] == 'current':
        return f"v{r['version']}" + (needs or " (latest)")
    if r['status'] == 'update':
        return f"Update v{r['version']} → v{r['latest_version']}"
    if r['handler']:
        return 'Has a handler'
    return (f"Needs Janus {r['needs']}" if r.get('needs') else f"Get v{r['latest_version']}")


class HubDialog(QDialog):
    handlersChanged = Signal()      # something was installed
    _done = Signal(object)          # worker -> GUI thread: a callable to run

    def __init__(self, parent=None, search: str = ''):
        super().__init__(parent)
        self.setWindowTitle("Get handlers — Janus handler hub")
        self.resize(820, 560)
        self._rows: list[dict] = []
        self._busy = False
        self._again = False
        self._notice = ''           # last install's result, shown when idle
        self._done.connect(lambda fn: fn())

        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self._search = QLineEdit(search)
        self._search.setPlaceholderText("Search the hub (name or Steam appid)…")
        self._search.setClearButtonEnabled(True)
        top.addWidget(self._search, stretch=1)
        self._sort = QComboBox()
        for key, label in hub.ORDERINGS.items():
            self._sort.addItem(label, key)
        top.addWidget(self._sort)
        self._mine = QCheckBox("Only my Steam library")
        self._mine.setToolTip("Games you own on Steam (installed or not), and games "
                              "with a handler from the hub")
        top.addWidget(self._mine)
        lay.addLayout(top)

        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["Game", "Installs", "Steam", "Handler"])
        self._tree.setRootIsDecorated(False)
        self._tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._tree.setSortingEnabled(False)       # the hub's order
        h = self._tree.header()
        h.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in (1, 2, 3):
            h.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        lay.addWidget(self._tree, stretch=1)

        self._status = QLabel()
        self._status.setWordWrap(True)
        lay.addWidget(self._status)

        btns = QHBoxLayout()
        self._get_btn = QPushButton("Get selected")
        self._all_btn = QPushButton("Get all my games")
        self._all_btn.setToolTip("Every missing handler for the games in your Steam "
                                 "library (installed or not), and every update")
        self._upd_btn = QPushButton("Update all")
        self._rm_btn = QPushButton("Remove")
        self._rm_btn.setToolTip("Remove the selected handlers installed from the hub")
        self._page_btn = QPushButton("Open hub page")
        self._page_btn.setToolTip("The handler's page on the hub, where its source is readable")
        close = QPushButton("Close")
        for b in (self._get_btn, self._all_btn, self._upd_btn, self._rm_btn, self._page_btn):
            btns.addWidget(b)
        btns.addStretch()
        btns.addWidget(close)
        lay.addLayout(btns)

        self._timer = QTimer(self, singleShot=True, interval=350)
        self._timer.timeout.connect(self.reload)
        self._search.textChanged.connect(self._typed)
        self._sort.currentIndexChanged.connect(lambda _i: self.reload())
        self._mine.toggled.connect(lambda _c: self._fill())
        self._tree.itemSelectionChanged.connect(self._update_buttons)
        self._tree.itemDoubleClicked.connect(lambda *_: self._get_selected())
        self._get_btn.clicked.connect(self._get_selected)
        self._all_btn.clicked.connect(lambda: self._ask([r for r in self._every if r['wanted']]))
        self._upd_btn.clicked.connect(
            lambda: self._ask([r for r in self._every if r['status'] == 'update']))
        self._page_btn.clicked.connect(self._open_page)
        self._rm_btn.clicked.connect(self._remove_selected)
        close.clicked.connect(self.accept)

        self._every: list[dict] = []    # the whole hub (for the totals)
        self._update_buttons()
        self.reload()

    # --- worker ------------------------------------------------------------

    def _run(self, text, work, then):
        if self._busy:
            self._again = True
            return
        self._busy = True
        self._status.setText(text)
        self._update_buttons()

        def worker():
            try:
                res, err = work(), ''
            except hub.HubError as e:
                res, err = None, str(e)

            def finish():
                self._busy = False
                self._status.setText(f"<span style='color:#cc4444'>{err}</span>" if err
                                     else self._notice)
                if not err:
                    then(res)
                self._update_buttons()
                if self._again:
                    self._again = False
                    self.reload()
            self._done.emit(finish)
        threading.Thread(target=worker, name='janus-hub', daemon=True).start()

    # --- listing -----------------------------------------------------------

    def _typed(self, _text):
        self._notice = ''
        self._timer.start()

    def reload(self):
        query, sort = self._search.text().strip(), self._sort.currentData()

        def work():
            every = hub.catalog('', sort, offline_ok=True)
            return every, (hub.catalog(query, sort, offline_ok=True) if query else every)

        def then(res):
            self._every, self._rows = res
            if self._every and self._every[0].get('offline'):
                import time
                t = hub.cache_time()
                self._notice = ("The hub can't be reached: showing its list"
                                + (time.strftime(' from %d %b', time.localtime(t)) if t else '')
                                + ". Installing needs the hub.")
            self._fill()
        self._run("Loading…", work, then)

    def _fill(self):
        self._tree.clear()
        rows = self._rows
        if self._mine.isChecked():
            rows = [r for r in rows if r['status'] or r['where'] in ('installed', 'library')]
        for r in rows:
            it = QTreeWidgetItem([r['name'], str(r['installs']), WHERE[r['where']],
                                  _status_text(r)])
            it.setData(0, Qt.UserRole, r)
            it.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            if r['status'] == 'update':
                it.setForeground(3, Qt.darkYellow)
            elif r['status'] == 'current' or r['handler']:
                it.setForeground(3, Qt.gray)
            self._tree.addTopLevelItem(it)
        if not rows and not self._busy and not self._notice:
            self._status.setText("No handler matches." if self._search.text().strip()
                                 else "The hub has no handlers yet.")
        self._update_buttons()

    def _selected(self) -> list[dict]:
        return [it.data(0, Qt.UserRole) for it in self._tree.selectedItems()]

    def _update_buttons(self):
        idle = not self._busy
        sel = self._selected()
        self._get_btn.setEnabled(idle and bool(sel))
        self._get_btn.setText(
            "Update selected" if sel and all(r['status'] == 'update' for r in sel)
            else "Get selected")
        self._page_btn.setEnabled(len(sel) == 1)
        # Only handlers the hub installed (status current/update) can go.
        self._rm_btn.setEnabled(idle and bool(sel) and all(r['status'] for r in sel))
        wanted = sum(1 for r in self._every if r['wanted'])
        updates = sum(1 for r in self._every if r['status'] == 'update')
        self._all_btn.setText(f"Get all my games ({wanted})" if wanted else "Get all my games")
        self._all_btn.setEnabled(idle and wanted > 0)
        self._upd_btn.setText(f"Update all ({updates})" if updates else "Update all")
        self._upd_btn.setEnabled(idle and updates > 0)

    def _remove_selected(self):
        rows = [r for r in self._selected() if r['status']]
        if not rows or self._busy:
            return
        names = ', '.join(r['name'] for r in rows)
        if QMessageBox.question(
                self, "Remove handlers",
                f"Remove the handler{'s' if len(rows) != 1 else ''} of {names}?\n\n"
                "Janus can't play a game without a handler. You can get it again from "
                "this window at any time; the games' settings and saves stay.",
                QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel) != QMessageBox.Yes:
            return

        def work():
            return [r['name'] for r in rows if hub.uninstall(r['key'])]

        def then(gone):
            if gone:
                self.handlersChanged.emit()
            self._notice = f"Removed {len(gone)} handler{'s' if len(gone) != 1 else ''}." if gone else ''
            self.reload()
        self._run("Removing…", work, then)

    def _open_page(self):
        sel = self._selected()
        if sel:
            webbrowser.open(hub.page_url(sel[0]['id']))

    # --- install (always confirmed first) --------------------------------

    def _get_selected(self):
        self._ask(self._selected())

    def _ask(self, rows: list[dict]):
        if not rows or self._busy:
            return

        def work():
            return [(r, hub.game_detail(r['id'])) for r in rows]
        self._run("Loading…", work, self._confirm)

    def _confirm(self, pairs):
        pairs = [(r, d) for r, d in pairs if d.get('versions')]
        # The newest version this Janus can run (Game.minimumJanusVersion).
        too_new = [d for r, d in pairs if hub.pick_version(d) is None]
        if too_new:
            QMessageBox.information(self, "Needs a newer Janus", '\n'.join(
                f"{d['name']}: every version of its handler needs Janus "
                f"{d['versions'][-1].get('min_janus_version') or '?'} or newer."
                for d in too_new) + "\n\nUpdate Janus to get "
                + ("it." if len(too_new) == 1 else "them."))
        pairs = [(r, d) for r, d in pairs if hub.pick_version(d) is not None]
        if not pairs:
            return
        lines = []
        for r, d in pairs:
            v = hub.pick_version(d)
            line = (f"<b>{d['name']}</b> v{v['number']} by {v['uploader'] or '(deleted account)'}"
                    f"<br><a href='{hub.page_url(d['id'])}'>{hub.page_url(d['id'])}</a>")
            if v['changelog']:
                line += f"<br><i>{v['changelog']}</i>"
            if v is not d['versions'][0]:
                line += (f"<br>(v{d['versions'][0]['number']} needs Janus "
                         f"{d['versions'][0].get('min_janus_version')} or newer: this is the "
                         "newest version this Janus can run)")
            if r['handler']:
                line += "<br>(you already have a handler for it; the one Janus uses may not change)"
            lines.append(line)
        box = QMessageBox(self)
        box.setWindowTitle("Install handlers")
        box.setTextFormat(Qt.RichText)
        box.setText(("Install this handler?" if len(pairs) == 1
                     else f"Install {len(pairs)} handlers?") + "<br><br>"
                    + "<br><br>".join(lines)
                    + "<br><br><span style='color:#d08a00'>Handlers are scripts that run with "
                      "access to your files and programs. Read their source on the hub "
                      "page first.</span>")
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
        box.button(QMessageBox.Yes).setText("Install")
        box.setDefaultButton(QMessageBox.Yes)
        if box.exec() != QMessageBox.Yes:
            return

        def work():
            ok, errors = [], []
            for r, d in pairs:
                try:
                    hub.install(d['id'], detail=d)
                    ok.append(r)
                except hub.HubError as e:
                    errors.append(f"{d['name']}: {e}")
            return ok, errors

        def then(res):
            ok, errors = res
            if ok:
                self.handlersChanged.emit()
            missing = [r['name'] for r in ok if r['where'] in ('library', 'store')]
            msg = f"Installed {len(ok)} handler{'s' if len(ok) != 1 else ''}." if ok else ''
            if missing:
                msg += (f" Install {', '.join(missing)} in Steam to play "
                        f"{'it' if len(missing) == 1 else 'them'}.")
            self._notice = msg
            if errors:
                QMessageBox.warning(self, "Some handlers weren't installed", '\n'.join(errors))
            self.reload()
        self._run("Installing…", work, then)

"""Handler hub browser for Big Picture mode (Y on the games screen).

Lists the hub's handlers (src/host/hub.py), searches them, and installs one
or every handler for the games in the Steam library, after the player has
seen what they are about to install: for one game a details view (cover,
notes, recent changes, contributors, and a QR code of the hub page, where
the source is readable, to open on a phone). The version installed is the
newest this Janus can run (Game.minimumJanusVersion). Without the hub, the
list comes from the last one fetched (hub.cached_games). Network calls run
on a worker thread; results come back to the GUI thread through queued
signals.
"""

import threading

from PySide6.QtCore import QObject, Property, Signal, Slot

import hub

SORTS = list(hub.ORDERINGS)          # popular first: the default


def _version_info(d: dict) -> dict:
    """What the confirmation shows for one game: the version that would be
    installed (the newest this Janus runs; `version` 0 = none) and details."""
    latest = d['versions'][0]
    v = hub.pick_version(d) or {}
    counts: dict = {}
    for x in d['versions']:
        name = x['uploader'] or '(deleted account)'
        counts[name] = counts.get(name, 0) + 1
    return {'id': d['id'], 'name': d['name'], 'version': v.get('number', 0),
            'latest': latest['number'],
            'needs': latest.get('min_janus_version', '') if v is not latest else '',
            'uploader': v.get('uploader') or '(deleted account)',
            'changelog': v.get('changelog', ''), 'description': d.get('description', ''),
            'contributors': sorted(counts, key=lambda n: -counts[n]),
            'history': [{'number': x['number'], 'changelog': x['changelog'],
                         'date': x.get('uploaded_at', '')[:10],
                         'needs': x.get('min_janus_version', '')}
                        for x in d['versions'][:5]],
            'cover': d.get('cover_url') or d.get('portrait_url') or '',
            'qr': d.get('qr_url', ''),
            'url': hub.page_url(d['id'])}


def _offline_note() -> str:
    import time
    t = hub.cache_time()
    when = time.strftime(' from %d %b', time.localtime(t)) if t else ''
    return f"The hub can't be reached: its list{when}. Installing needs the hub."


def _list_rows(query: str, ordering: str) -> list[dict]:
    """The hub's games matching *query* in the hub's *ordering*, with what
    this machine has (hub.catalog)."""
    keep = ('id', 'key', 'name', 'status', 'version', 'where', 'handler', 'wanted',
            'installs')
    return [{**{k: r[k] for k in keep}, 'latest': r['latest_version'],
             'needs': r['needs'], 'offline': r['offline']}
            for r in hub.catalog(query, ordering, offline_ok=True)]


def _hub_state(games=None) -> tuple[list, list]:
    """(keys of every game the hub has a handler for, keys of the games
    whose installed hub handler has a newer version): one list call, or
    from *games* (e.g. the cached list)."""
    local = hub.installed()
    games = hub.list_games() if games is None else games
    return ([k for k in map(hub.game_key, games) if k],
            [hub.game_key(g) for g in games if hub.status(g, local) == 'update'])


class HubBrowser(QObject):
    changed = Signal()
    _done = Signal(object)           # worker -> GUI thread: a callable to run

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[dict] = []
        self._query = ''
        self._sort = SORTS[0]
        self._mine = False           # only games in the Steam library / from the hub
        self._updates: list = []     # keys with a handler update on the hub
        self._available: list = []   # keys of every game with a handler on the hub
        self._wanted = 0             # "All my games" count, from the unfiltered list
        self._again = False          # a reload was asked for while busy
        self._busy = ''              # what the worker is doing, '' = idle
        self._error = ''
        self._notice = ''
        self._confirm = None         # {'all': bool, 'items': [version info]}
        self._details: list[dict] = []
        self._done.connect(lambda fn: fn())

    def _run(self, busy, work, then):
        """Run work() on a thread, then then(result) on the GUI thread; a
        HubError lands in `error`."""
        if self._busy:
            return
        self._busy, self._error, self._notice = busy, '', ''
        self.changed.emit()

        def worker():
            try:
                res, err = work(), ''
            except hub.HubError as e:
                res, err = None, str(e)

            def finish():
                self._busy, self._error = '', err
                if not err:
                    then(res)
                self.changed.emit()
                if self._again:
                    self._again = False
                    self.reload()
            self._done.emit(finish)
        threading.Thread(target=worker, daemon=True).start()

    # --- listing -------------------------------------------------------

    @Slot()
    def reload(self):
        if self._busy:
            self._again = True       # e.g. typing a search: run once it's done
            return
        query, sort = self._query, self._sort
        self._run('Loading…', lambda: _list_rows(query, sort), self._set_rows)

    def _set_rows(self, rows):
        self._rows = rows
        if rows and rows[0].get('offline'):
            self._notice = _offline_note()
        if not self._query:          # the whole hub: refresh the totals too
            self._set_totals(rows)

    def _set_totals(self, every):
        """Update count and "All my games" count from the unfiltered list."""
        self._updates = [r['key'] for r in every if r['status'] == 'update']
        self._wanted = sum(1 for r in every if r['wanted'])
        self._available = [r['key'] for r in every]

    @Slot(str)
    def search(self, text):
        if text.strip() == self._query:
            return
        self._query = text.strip()
        self.reload()

    @Property(str, notify=changed)
    def query(self):
        return self._query

    @Slot(int)
    def cycleSort(self, step):
        self._sort = SORTS[(SORTS.index(self._sort) + step) % len(SORTS)]
        self.reload()

    @Property(str, notify=changed)
    def sortLabel(self):
        return hub.ORDERINGS[self._sort]

    @Slot()
    def toggleMine(self):
        self._mine = not self._mine
        self.changed.emit()

    @Property(bool, notify=changed)
    def mine(self):
        return self._mine

    @Property('QVariantList', notify=changed)
    def rows(self):
        if self._mine:
            return [r for r in self._rows if r['status'] or r['where'] in ('installed', 'library')]
        return self._rows

    @Property(int, notify=changed)
    def missingCount(self):
        """Hub handlers for games in the Steam library that are missing, or
        outdated here (what "All my games" fetches)."""
        return self._wanted

    # --- updates (checked at startup, shown on the games screen) ----------

    @Slot()
    def checkUpdates(self):
        """Ask the hub, on a thread, which games it has handlers for and
        which installed handlers have newer versions; quietly does nothing
        when the hub can't be reached. The cached list answers at once."""
        cached = hub.cached_games()
        if cached:
            self._available, self._updates = _hub_state(cached)
            self.changed.emit()

        def worker():
            try:
                available, updates = _hub_state()
            except hub.HubError as e:
                print(f'[bigpicture] hub check: {e}', flush=True)
                return

            def finish():
                self._available, self._updates = available, updates
                self.changed.emit()
            self._done.emit(finish)
        threading.Thread(target=worker, name='janus-hub-updates', daemon=True).start()

    @Property('QVariantList', notify=changed)
    def updates(self):
        return self._updates

    def available(self) -> set:
        """Keys of the games the hub has a handler for (as last seen)."""
        return set(self._available)

    @Property(int, notify=changed)
    def updateCount(self):
        return len(self._updates)

    @Slot()
    def askUpdates(self):
        """Confirm every handler update at once."""
        def work():
            local = hub.installed()
            return [hub.game_detail(g['id']) for g in hub.list_games()
                    if hub.status(g, local) == 'update']

        def then(details):
            if not details:
                self._updates = []
                self._notice = 'Every handler is up to date.'
                return
            self._ask_details(details, True)
        self._run('Checking for updates…', work, then)

    @Property(str, notify=changed)
    def busy(self):
        return self._busy

    @Property(str, notify=changed)
    def error(self):
        return self._error

    @Property(str, notify=changed)
    def notice(self):
        return self._notice

    # --- install (always confirmed first) --------------------------------

    def _ask_details(self, details: list, every: bool):
        """Show *details* for confirmation, leaving out games none of whose
        versions this Janus can run (said in the notice)."""
        infos = [_version_info(d) for d in details]
        local = hub.installed()
        for d, i in zip(details, infos):     # the hub's version installed here, 0 = none
            rec = local.get(hub.game_key(d)) or {}
            i['have'] = rec.get('version', 0) if rec.get('id') == d['id'] else 0
        ok = [(d, i) for d, i in zip(details, infos) if i['version']]
        too_new = [i for i in infos if not i['version']]
        if too_new:
            self._notice = ' '.join(
                f"{i['name']} needs Janus {i['needs'] or '?'} or newer." for i in too_new) \
                + ' Update Janus to get ' + ('it.' if len(too_new) == 1 else 'them.')
        if not ok:
            return
        self._details = [d for d, _ in ok]
        self._confirm = {'all': every, 'items': [i for _, i in ok]}

    @Slot(int)
    def ask(self, game_id):
        """Fetch one game's latest version and show it for confirmation."""
        self._run('Loading…', lambda: hub.game_detail(game_id),
                  lambda d: self._ask_details([d], False))

    @Slot('QVariant', str)
    def askFor(self, key, name):
        """A game without a handler was picked (Steam appid or non-Steam id):
        list the hub and offer that game's handler, or say the hub has none
        for it yet."""
        query, sort = self._query, self._sort
        if isinstance(key, float):
            key = int(key)

        def work():
            g = hub.find_game(key)
            return _list_rows(query, sort), hub.game_detail(g['id']) if g else None

        def then(res):
            self._rows, d = res
            if d is None or not d.get('versions'):
                self._notice = (f'The hub has no handler for {name} yet, so it '
                                "can't be played with Janus.")
                return
            self._ask_details([d], False)
        self._run('Looking for a handler…', work, then)

    @Slot()
    def askAll(self):
        """Confirm every missing/outdated handler for the games in the Steam
        library, installed or not (hub.catalog's `wanted`). Searches
        everything, not just the current filter."""
        def work():
            return [hub.game_detail(r['id']) for r in hub.catalog() if r['wanted']]

        def then(details):
            if not details:
                self._notice = 'Every game you have already has its latest handler.'
                return
            self._ask_details(details, True)
        self._run('Checking your games…', work, then)

    @Property('QVariant', notify=changed)
    def confirm(self):
        return self._confirm

    @Slot()
    def cancel(self):
        self._confirm, self._details = None, []
        self.changed.emit()

    @Slot()
    def askRemove(self):
        """From one game's details: ask to remove the handler the hub
        installed for it (A removes, B goes back to the details)."""
        c = self._confirm
        if not c or c.get('all') or c.get('remove') or not c['items'][0].get('have'):
            return
        self._confirm = {**c, 'remove': True}
        self.changed.emit()

    @Slot()
    def back(self):
        """B: from the remove question back to the details, else cancel."""
        if self._confirm and self._confirm.get('remove'):
            self._confirm = {k: v for k, v in self._confirm.items() if k != 'remove'}
            self.changed.emit()
        else:
            self.cancel()

    def _remove(self):
        d, query, sort = self._details[0], self._query, self._sort
        self._confirm, self._details = None, []

        def work():
            gone = hub.uninstall(hub.game_key(d))
            try:
                every = _list_rows('', sort)
                rows = _list_rows(query, sort) if query else every
            except hub.HubError:
                every = rows = None
            return gone, rows, every

        def then(res):
            gone, rows, every = res
            if every is not None:
                self._set_totals(every)
                self._rows = rows
            self._notice = (f"Removed the {d['name']} handler." if gone
                            else f"{d['name']} has no handler from the hub to remove.")
        self._run('Removing…', work, then)

    @Slot()
    def accept(self):
        if self._confirm and self._confirm.get('remove'):
            self._remove()
            return
        details, query, sort = self._details, self._query, self._sort
        self._confirm, self._details = None, []

        def work():
            ok, errors = [], []
            for d in details:
                try:
                    hub.install(d['id'], detail=d)
                    ok.append(d)
                except hub.HubError as e:
                    errors.append(f"{d['name']}: {e}")
            try:
                every = _list_rows('', sort)
                rows = _list_rows(query, sort) if query else every
            except hub.HubError:
                every = rows = None  # installed anyway; keep the old list
            return ok, errors, rows, every

        def then(res):
            ok, errors, rows, every = res
            where = {r['id']: r['where'] for r in (rows or self._rows)}
            if every is not None:
                self._set_totals(every)
                self._rows = rows
            missing = [d['name'] for d in ok if where.get(d['id']) in ('library', 'store')]
            msg = [f"Installed {len(ok)} handler{'s' if len(ok) != 1 else ''}."] if ok else []
            if missing:
                msg.append(f"Install {', '.join(missing)} in Steam to play "
                           f"{'it' if len(missing) == 1 else 'them'}.")
            self._notice = ' '.join(msg)
            self._error = '\n'.join(errors)
        self._run('Installing…', work, then)

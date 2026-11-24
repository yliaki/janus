"""Session-wide matching of identical pads to their Steam virtual pads.

Under Steam Input each game reads its pad as a Steam virtual pad, and the hook
picks the virtual pad from virtualgamepadinfo.txt by Steam's per-device handle.
For pads Steam can't tell apart (same VID:PID, no serial: two MACHENIKEs, the
two halves of a "Twin USB" PS1 adapter) nothing on disk ties a kernel device to
its handle: Steam opens them as `sdl://N` and numbered the adapter's halves the
opposite way round from the kernel (2026-10-17). A wrong guess only swaps
players when every pad of the group is in use; with a spare (an idle identical
pad, the adapter's empty port) a player can get the spare's virtual pad and no
input at all.

So one matcher per session watches the physical pads of every ambiguous group
and all Steam virtual pads. A button press on a physical pad followed within
~0.25 s by a press on exactly one of that group's virtual pads (and no other
pad of the group pressed meanwhile) is a vote; two votes confirm the pair. The
assignment physical -> handle is kept one-to-one for the whole group (the last
unconfirmed pad follows by elimination), so two games never get the same
virtual pad, and every instance in a group gets its handle written to
``pad_map_path(appid, instance)`` (``handle=0x…``). The hook reads that file
(JANUS_PAD_MAP), prefers it over its own guess, and re-resolves when it
changes. Steam only feeds the virtual pads while it sees a game focused (see
steam_window.py), so votes only come in then.
"""

import os
import re
import select
import struct
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

VALVE_VID, VIRTUAL_PID = 0x28de, 0x11ff
JS_EVENT_BUTTON, JS_EVENT_AXIS, JS_EVENT_INIT = 0x01, 0x02, 0x80
AXIS_PRESS = 24000  # a hat/stick pushed past this from rest counts as a press
WINDOW = 0.25       # s from a physical press to Steam's virtual press
VOTES_NEEDED = 2


def pad_map_path(appid: int, instance: int) -> Path:
    return Path.home() / '.local/share/janus/run' / str(appid) / f'i{instance}.pad'


def _vgi_path() -> Path:
    return Path.home() / '.steam/steam/config/virtualgamepadinfo.txt'


def _read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ''


def _js_info(js: int) -> Optional[dict]:
    dev = f'/sys/class/input/js{js}/device'
    if not os.path.isdir(dev):
        return None
    try:
        vid = int(_read(f'{dev}/id/vendor') or '0', 16)
        pid = int(_read(f'{dev}/id/product') or '0', 16)
    except ValueError:
        return None
    ev = next((int(n[5:]) for n in os.listdir(dev)
               if n.startswith('event') and n[5:].isdigit()), -1)
    return {'js': js, 'event': ev, 'vid': vid, 'pid': pid,
            'name': _read(f'{dev}/name')}


def _all_js() -> List[dict]:
    out = []
    for n in os.listdir('/sys/class/input'):
        if n.startswith('js') and n[2:].isdigit():
            info = _js_info(int(n[2:]))
            if info:
                out.append(info)
    return out


def _parse_vgi() -> List[dict]:
    """Slots of virtualgamepadinfo.txt: slot, vid, pid, handle, steam index."""
    slots, cur = [], None
    for line in _read(str(_vgi_path())).splitlines():
        m = re.match(r'\[slot (\d+)\]', line.strip())
        if m:
            cur = {'slot': int(m.group(1)), 'vid': 0, 'pid': 0, 'handle': ''}
            slots.append(cur)
            continue
        if cur is None or '=' not in line:
            continue
        k, v = line.strip().split('=', 1)
        if k == 'VID':
            cur['vid'] = int(v, 16)
        elif k == 'PID':
            cur['pid'] = int(v, 16)
        elif k == 'handle':
            cur['handle'] = v
    for s in slots:
        s['index'] = _steam_index(s['handle'], s['vid'], s['pid'])
    return [s for s in slots if s['handle']]


def _steam_index(handle: str, vid: int, pid: int) -> int:
    """Steam's controller index: the hex digit before "<vid><pid>" in the
    handle (same rule as the hook's detect_steam_input())."""
    key = f'{vid:x}{pid:x}'
    at = handle.find(key)
    if at <= 0:
        return 0
    c = handle[at - 1]
    return int(c, 16) if c in '0123456789abcdef' else 0


class PadMatcher:
    """See the module docstring. ``instances`` = [(instance number, js index
    of its assigned physical pad)]."""

    def __init__(self, appid: int, instances: List[Tuple[int, int]],
                 verbose: bool = False):
        self.appid = appid
        self.verbose = verbose
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # instance -> physical pad, identified by its event node (stable while
        # the device stays connected; js numbers get reused).
        self.inst_event: Dict[int, int] = {}
        for inst, js in instances:
            info = _js_info(js) if js is not None else None
            if info and info['vid'] != VALVE_VID:
                self.inst_event[inst] = info['event']
        self.confirmed: Dict[int, str] = {}        # phys event -> handle
        self.votes: Dict[int, Dict[str, int]] = {}
        self.written: Dict[int, str] = {}
        for inst in self.inst_event:
            try:
                pad_map_path(appid, inst).unlink()
            except OSError:
                pass

    def log(self, msg: str) -> None:
        print(f'[janus] pad matcher: {msg}', flush=True)

    # -- state ---------------------------------------------------------------

    def _groups(self, phys: List[dict], slots: List[dict]):
        """{(vid, pid): (physical pads by event order, slots by Steam index)}
        for models with more than one handle (the only ambiguous ones)."""
        groups = {}
        for s in slots:
            groups.setdefault((s['vid'], s['pid']), ([], []))[1].append(s)
        for key in list(groups):
            p = sorted((x for x in phys if (x['vid'], x['pid']) == key),
                       key=lambda x: x['event'])
            s = sorted(groups[key][1], key=lambda x: x['index'])
            if len(s) < 2 or not p:
                del groups[key]
            else:
                groups[key] = (p, s)
        return groups

    def _assignment(self, pads: List[dict], slots: List[dict]) -> Dict[int, str]:
        """One-to-one physical event -> handle for a group: confirmed pairs,
        the rest in rank order over what's left. Steam's numbering of
        identical pads follows no usable pattern (the Twin adapter's halves
        came reversed; three uinput test pads as 6, 4, 5), so the rank order
        is only a guess until presses confirm a pad, or elimination places
        the last one (2026-10-17)."""
        handles = [s['handle'] for s in slots]
        out = {p['event']: self.confirmed[p['event']] for p in pads
               if self.confirmed.get(p['event']) in handles}
        free_h = [h for h in handles if h not in out.values()]
        for p in pads:
            if p['event'] not in out and free_h:
                out[p['event']] = free_h.pop(0)
        return out

    def _write(self, groups) -> None:
        for inst, ev in self.inst_event.items():
            handle = None
            for pads, slots in groups.values():
                if any(p['event'] == ev for p in pads):
                    handle = self._assignment(pads, slots).get(ev)
            path = pad_map_path(self.appid, inst)
            # (Re)written when missing: each instance's launch clears its file.
            if not handle or (self.written.get(inst) == handle and path.exists()):
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix('.tmp')
            tmp.write_text(f'handle={handle}\n')
            os.replace(tmp, path)
            self.written[inst] = handle
            how = 'confirmed' if self.confirmed.get(ev) == handle else 'by elimination/rank'
            self.log(f'instance {inst} (event{ev}) -> {handle} ({how})')

    # -- matching ------------------------------------------------------------

    def _vote(self, ev: int, handle: str, groups) -> None:
        v = self.votes.setdefault(ev, {})
        v[handle] = v.get(handle, 0) + 1
        best = max(v, key=v.get)
        if v[best] < VOTES_NEEDED or self.confirmed.get(ev) == best:
            return
        if any(n >= v[best] for h, n in v.items() if h != best):
            return  # contradictory votes: keep waiting
        # A handle belongs to one pad: drop it from whoever had it.
        for other, h in list(self.confirmed.items()):
            if h == best and other != ev:
                del self.confirmed[other]
                self.votes.pop(other, None)
        self.confirmed[ev] = best
        self.log(f'event{ev} is Steam handle {best} ({v[best]} matching presses)')
        self._write(groups)

    def _run(self) -> None:
        fds: Dict[int, Tuple[str, dict]] = {}     # fd -> ('phys'|'virt', info)
        presses: List[list] = []    # [time, 'phys'|'virt', info, judged]
        axes: Dict[Tuple[int, int], int] = {}   # (fd, axis) -> last value
        groups, slots, last_scan, vgi_mtime = {}, [], 0.0, None
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                if now - last_scan > 2:
                    last_scan = now
                    try:
                        mt = _vgi_path().stat().st_mtime
                    except OSError:
                        mt = None
                    if mt != vgi_mtime:
                        vgi_mtime, slots = mt, _parse_vgi()
                    js = _all_js()
                    phys = [x for x in js if x['vid'] != VALVE_VID]
                    virt = [x for x in js if (x['vid'], x['pid']) == (VALVE_VID, VIRTUAL_PID)]
                    groups = self._groups(phys, slots)
                    want = {('phys', x['js']): x for g in groups.values() for x in g[0]}
                    want.update({('virt', x['js']): x for x in virt})
                    for fd, (kind, info) in list(fds.items()):
                        if (kind, info['js']) not in want or want[(kind, info['js'])] != info:
                            os.close(fd)
                            del fds[fd]
                            for k in [k for k in axes if k[0] == fd]:
                                del axes[k]
                    have = {(k, i['js']) for k, i in fds.values()}
                    for (kind, j), info in want.items():
                        if (kind, j) in have:
                            continue
                        try:
                            fds[os.open(f'/dev/input/js{j}', os.O_RDONLY | os.O_NONBLOCK)] = (kind, info)
                        except OSError:
                            pass
                    self._write(groups)
                r, _, _ = select.select(list(fds), [], [], 0.1)
                now = time.monotonic()
                for fd in r:
                    kind, info = fds[fd]
                    try:
                        data = os.read(fd, 8 * 64)
                    except OSError:
                        os.close(fd)
                        del fds[fd]
                        continue
                    for i in range(0, len(data) - 7, 8):
                        _t, val, typ, num = struct.unpack('IhBB', data[i:i + 8])
                        if typ == JS_EVENT_BUTTON and val == 1:   # not INIT
                            presses.append([now, kind, info, False])
                        elif typ & ~JS_EVENT_INIT == JS_EVENT_AXIS:
                            # D-pad (hat) and sticks: menus are often driven
                            # with these alone. Count rest -> full deflection.
                            # INIT events only record the resting value
                            # (triggers rest at full negative deflection).
                            prev = axes.get((fd, num), 0)
                            axes[(fd, num)] = val
                            if not typ & JS_EVENT_INIT and abs(val) > AXIS_PRESS >= abs(prev):
                                presses.append([now, kind, info, False])
                self._settle(presses, groups, slots, now)
        finally:
            for fd in fds:
                os.close(fd)

    def _settle(self, presses, groups, slots, now) -> None:
        """Judge each physical press once its window has passed; drop old ones."""
        by_slot = {s['slot']: s for s in slots}
        for rec in presses:
            t, kind, info, judged = rec
            if kind != 'phys' or judged or now - t < WINDOW:
                continue
            rec[3] = True
            key = (info['vid'], info['pid'])
            if key not in groups:
                continue
            others = any(k2 == 'phys' and i2 is not info and
                         (i2['vid'], i2['pid']) == key and abs(t2 - t) < WINDOW
                         for t2, k2, i2, _ in presses)
            handles = set()
            for t2, k2, v, _ in presses:
                if k2 != 'virt' or not (t - 0.03 <= t2 <= t + WINDOW):
                    continue
                m = re.search(r'pad (\d+)$', v['name'])
                s = by_slot.get(int(m.group(1))) if m else None
                if s and (s['vid'], s['pid']) == key:
                    handles.add(s['handle'])
            if len(handles) == 1 and not others:
                self._vote(info['event'], handles.pop(), groups)
        presses[:] = [r for r in presses if now - r[0] <= 3 * WINDOW]

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> 'PadMatcher':
        if self.inst_event:
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name='janus-pad-matcher')
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        for inst in self.inst_event:
            try:
                pad_map_path(self.appid, inst).unlink()
            except OSError:
                pass

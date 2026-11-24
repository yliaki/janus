"""ProbeSession — the single core both frontends drive.

Frontends own only their event loop + rendering; all enumeration/reading/Steam logic
lives here. Terminal: select() on fds() + periodic poll_steam(). GUI: QSocketNotifier
per fd + QTimer for poll_steam(). Neither duplicates the other.
"""
from __future__ import annotations

import time

from . import activity
from .correlate import Correlator
from .evdev_source import EvdevSource
from .model import InputDevice, MappingRow, ProbeSnapshot
from .steam_input_source import SteamInputSource


def _name_num(dev: InputDevice) -> int | None:
    """Trailing integer in the virtual pad name ('Microsoft X-Box 360 pad 2' -> 2)."""
    tail = dev.name.rsplit(" ", 1)[-1]
    return int(tail) if tail.isdigit() else None


def _js_num(dev: InputDevice) -> int:
    return int(dev.js[2:]) if dev.js[2:].isdigit() else 0


class ProbeSession:
    def __init__(self) -> None:
        self.evdev = EvdevSource()
        self.steam = SteamInputSource()
        self.correlator = Correlator()
        self._steam_controllers = []
        self.refresh()

    # ---- evdev (live input) ----
    def refresh(self) -> None:
        self.evdev.refresh()

    def fds(self):
        return self.evdev.fds()

    def pump(self, fd: int) -> None:
        presses = self.evdev.pump(fd)
        if presses:
            self.correlator.feed(time.monotonic(), presses)

    # ---- Steam Input (IPC) ----
    def poll_steam(self) -> None:
        self._steam_controllers = self.steam.poll()

    # ---- snapshot for rendering ----
    def _build_mapping(self, devices) -> list[MappingRow]:
        phys = sorted((d for d in devices if not d.is_virtual), key=_js_num)
        virt = [d for d in devices if d.is_virtual]
        virt_by_event = {d.event: d for d in virt}
        # order-based expectation: js index i -> Steam slot i (sorted by pad-name number)
        virt_ordered = sorted(virt, key=lambda d: (_name_num(d) if _name_num(d) is not None
                                                    else 99, d.event))
        handle_by_gp = {c.gamepad_index: c for c in self._steam_controllers}

        rows = []
        for i, p in enumerate(phys):
            expected = virt_ordered[i] if i < len(virt_ordered) else None
            det_event, votes = self.correlator.best(p.event)
            detected = virt_by_event.get(det_event) if det_event else None
            mapped = detected or expected
            gp = _name_num(mapped) if mapped else None
            ctrl = handle_by_gp.get(gp) if gp is not None else None
            if detected is None:
                status = "unverified"
            elif expected is not None and detected.event == expected.event:
                status = "ok"
            else:
                status = "mismatch"
            rows.append(MappingRow(
                phys_event=p.event, phys_js=p.js, phys_id=f"{p.vid}:{p.pid}",
                phys_port=p.port or "(no port id)",
                expected_virtual=expected.event if expected else None,
                detected_virtual=detected.event if detected else None,
                votes=votes,
                gamepad_index=gp if ctrl else None,
                handle=ctrl.handle if ctrl else None,
                status=status))
        return rows

    def snapshot(self) -> ProbeSnapshot:
        devices = list(self.evdev.devices)
        act = activity.compute(devices, self._steam_controllers,
                               self.steam.available, self.steam.error)
        return ProbeSnapshot(devices=devices,
                             steam_controllers=list(self._steam_controllers),
                             activity=act, mapping=self._build_mapping(devices))

    def close(self) -> None:
        self.evdev.close()
        self.steam.close()

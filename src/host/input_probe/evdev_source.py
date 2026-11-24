"""Enumerate pad nodes (physical + Steam virtual) and live-read their evdev events.

Physical vs virtual is decided by sysfs origin: uinput devices resolve under
/sys/devices/virtual/, real USB pads under a pci/usb path. See NOTES.md.
"""
from __future__ import annotations

import os
import struct

from .model import InputDevice

# struct input_event (64-bit): __kernel_ulong_t tv_sec, tv_usec; __u16 type, code; __s32 value
_FMT = "llHHi"
_SIZE = struct.calcsize(_FMT)
_EV_KEY, _EV_ABS = 0x01, 0x03
_BTN = {
    0x130: "A", 0x131: "B", 0x133: "X", 0x134: "Y",
    0x136: "LB", 0x137: "RB", 0x138: "LT", 0x139: "RT",
    0x13a: "Back", 0x13b: "Start", 0x13c: "Guide", 0x13d: "L3", 0x13e: "R3",
}
_HAT = {0x10: "DPadX", 0x11: "DPadY"}


def enumerate_pads() -> list[InputDevice]:
    """Parse /proc/bus/input/devices for joystick nodes, tagging physical vs virtual."""
    pads: list[InputDevice] = []
    try:
        with open("/proc/bus/input/devices") as f:
            blocks = f.read().split("\n\n")
    except OSError:
        return pads
    for block in blocks:
        name = vid = pid = event = js = None
        for line in block.splitlines():
            if line.startswith("N: Name="):
                name = line.split("=", 1)[1].strip().strip('"')
            elif line.startswith("I: "):
                for tok in line.split():
                    if tok.startswith("Vendor="):
                        vid = tok.split("=")[1]
                    elif tok.startswith("Product="):
                        pid = tok.split("=")[1]
            elif line.startswith("H: Handlers="):
                for tok in line.split("=", 1)[1].split():
                    if tok.startswith("event"):
                        event = tok
                    elif tok.startswith("js"):
                        js = tok
        if not event or js is None:  # joystick nodes only
            continue
        link = os.path.realpath(f"/sys/class/input/{event}/device")
        origin = "virtual" if "/devices/virtual/" in link else "physical"
        phys = _read(f"/sys/class/input/{event}/device/phys")
        uniq = _read(f"/sys/class/input/{event}/device/uniq")
        pads.append(InputDevice(event=event, js=js, name=name or "?",
                                vid=vid or "0000", pid=pid or "0000", origin=origin,
                                phys=phys, uniq=uniq))
    return pads


def _read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


class EvdevSource:
    """Owns the open evdev fds; frontends drive their own event loop via fds()/pump()."""

    def __init__(self) -> None:
        self.devices: list[InputDevice] = []
        self._fds: dict[int, InputDevice] = {}
        self._by_event: dict[str, InputDevice] = {}
        self._frame = 0

    def refresh(self) -> None:
        """Re-enumerate; open new nodes, close vanished ones, preserve live state."""
        pads = enumerate_pads()
        new_by_event = {p.event: p for p in pads}
        # carry live state forward for surviving devices
        for ev, dev in new_by_event.items():
            old = self._by_event.get(ev)
            if old and old.vid == dev.vid and old.pid == dev.pid:
                dev.pressed = old.pressed
                dev.last_label = old.last_label
                dev.last_ns = old.last_ns
        # close fds for devices that disappeared
        for fd, dev in list(self._fds.items()):
            if dev.event not in new_by_event:
                try:
                    os.close(fd)
                except OSError:
                    pass
                del self._fds[fd]
        # open fds for new devices
        open_events = {d.event for d in self._fds.values()}
        for dev in pads:
            if dev.event in open_events:
                continue
            try:
                fd = os.open(f"/dev/input/{dev.event}", os.O_RDONLY | os.O_NONBLOCK)
                self._fds[fd] = dev
            except OSError:
                pass  # permission (need 'input' group) or race; skip
        self.devices = pads
        self._by_event = new_by_event

    def fds(self) -> dict[int, InputDevice]:
        return dict(self._fds)

    def pump(self, fd: int) -> list[tuple[str, str, str]]:
        """Drain pending events on one fd; update device state.

        Returns button-PRESS events as (event_node, origin, label) so the session can
        correlate physical<->virtual by coincidence.
        """
        dev = self._fds.get(fd)
        if dev is None:
            return []
        try:
            data = os.read(fd, _SIZE * 128)
        except (BlockingIOError, OSError):
            return []
        presses: list[tuple[str, str, str]] = []
        for off in range(0, len(data) - _SIZE + 1, _SIZE):
            _, _, etype, code, value = struct.unpack(_FMT, data[off:off + _SIZE])
            if etype == _EV_KEY:
                label = _BTN.get(code, f"btn0x{code:x}")
                if value == 1:
                    dev.pressed.add(label)
                    dev.last_label = f"{label} press"
                    presses.append((dev.event, dev.origin, label))
                elif value == 0:
                    dev.pressed.discard(label)
                    dev.last_label = f"{label} release"
                else:
                    continue
            elif etype == _EV_ABS and code in _HAT and value != 0:
                dev.last_label = f"{_HAT[code]} {value}"
            else:
                continue
            self._frame += 1
            dev.last_ns = self._frame
        return presses

    def close(self) -> None:
        for fd in list(self._fds):
            try:
                os.close(fd)
            except OSError:
                pass
        self._fds.clear()

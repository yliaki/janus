"""Controller enumeration using kernel /dev/input/js* as canonical ordering.

Gamepads the kernel gives no js node (Nintendo Pro Controller over Bluetooth
via hid-nintendo: event node only) are listed too, after the js pads, with
index EVDEV_INDEX_BASE + their event number, so an index never collides with a
js number. Use sysfs_device()/event_node() rather than building js paths.
"""

import os
from dataclasses import dataclass
from typing import List, Optional

def _read_sysfs(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


# Index of an event-only gamepad = EVDEV_INDEX_BASE + N for /dev/input/eventN.
EVDEV_INDEX_BASE = 100
_GAMEPAD_KEYS = range(0x120, 0x140)    # BTN_JOYSTICK..BTN_THUMBR, udev's ID_INPUT_JOYSTICK test
_INPUT_PROP_ACCELEROMETER = 6
# phys of the virtual Xbox pads of Janus input mode (padd.py): not controllers.
JANUS_PHYS_PREFIX = "janus/"


def sysfs_device(index: int) -> str:
    """sysfs input-device dir of controller *index* (js or event-only)."""
    if index >= EVDEV_INDEX_BASE:
        return f"/sys/class/input/event{index - EVDEV_INDEX_BASE}/device"
    return f"/sys/class/input/js{index}/device"


def event_node(index: int) -> str:
    """/dev/input/eventN of controller *index*, "" if it has none."""
    if index >= EVDEV_INDEX_BASE:
        return f"/dev/input/event{index - EVDEV_INDEX_BASE}"
    try:
        for child in sorted(os.listdir(sysfs_device(index))):
            if child.startswith("event") and child[5:].isdigit():
                return f"/dev/input/{child}"
    except OSError:
        pass
    return ""


def index_for_event_node(path: str) -> Optional[int]:
    """Controller index of /dev/input/eventN: its js number, or the
    event-only index when it has no js node; None if it's no gamepad."""
    node = os.path.basename(path or "")
    if not node.startswith("event") or not node[5:].isdigit():
        return None
    dev = f"/sys/class/input/{node}/device"
    try:
        for child in os.listdir(dev):
            if child.startswith("js") and child[2:].isdigit():
                return int(child[2:])
    except OSError:
        return None
    n = int(node[5:])
    return EVDEV_INDEX_BASE + n if _is_evdev_gamepad(n) else None


def index_for_device_node(path: str) -> Optional[int]:
    """index_for_event_node(), also for /dev/hidrawN (SDL's HIDAPI devices):
    the gamepad event node of the same HID device."""
    node = os.path.basename(path or "")
    if node.startswith("hidraw") and node[6:].isdigit():
        base = f"/sys/class/hidraw/{node}/device/input"
        try:
            for inp in sorted(os.listdir(base)):
                for ev in sorted(os.listdir(f"{base}/{inp}")):
                    if ev.startswith("event") and ev[5:].isdigit():
                        idx = index_for_event_node(f"/dev/input/{ev}")
                        if idx is not None:
                            return idx
        except OSError:
            pass
        return None
    return index_for_event_node(path)


def hidraw_node(index: int) -> str:
    """/dev/hidrawN of the HID device behind controller *index*, "" if none."""
    try:
        dev = os.path.realpath(sysfs_device(index))   # .../hidN/input/inputM
        hid = os.path.dirname(os.path.dirname(dev))
        for h in sorted(os.listdir(f"{hid}/hidraw")):
            if h.startswith("hidraw"):
                return f"/dev/{h}"
    except OSError:
        pass
    return ""


def _bit(words: List[str], b: int) -> bool:
    bpl = 64
    w = b // bpl
    if w >= len(words):
        return False
    return bool(int(words[len(words) - 1 - w], 16) & (1 << (b % bpl)))


def _is_evdev_gamepad(n: int) -> bool:
    """eventN is a gamepad (gamepad/joystick buttons + an X axis, not an
    accelerometer, not Steam's virtual pad) with no js node."""
    dev = f"/sys/class/input/event{n}/device"
    try:
        if any(c.startswith("js") for c in os.listdir(dev)):
            return False
    except OSError:
        return False
    keys = _read_sysfs(f"{dev}/capabilities/key").split()
    absb = _read_sysfs(f"{dev}/capabilities/abs").split()
    props = _read_sysfs(f"{dev}/properties").split() or ["0"]
    if _bit(props, _INPUT_PROP_ACCELEROMETER):
        return False
    if not any(_bit(keys, b) for b in _GAMEPAD_KEYS) or not _bit(absb, 0):
        return False
    vid = _read_sysfs(f"{dev}/id/vendor")
    return (vid.lower() != "28de"
            and not _read_sysfs(f"{dev}/phys").startswith(JANUS_PHYS_PREFIX))


@dataclass
class ControllerInfo:
    """Information about a connected controller."""
    index: int          # js number (js0 = 0, ...), or EVDEV_INDEX_BASE + N for an
                        # event-only pad — ordinal, NOT identity
    name: str
    device_path: str    # e.g., "/dev/input/js0", or "/dev/input/event23" (event-only)
    vendor_id: int = 0  # USB VID (from sysfs)
    product_id: int = 0 # USB PID (from sysfs)
    uniq: str = ""      # serial/MAC when exposed (unique when present; often empty)
    phys: str = ""      # USB port path, e.g. "usb-0000:2a:00.1-2.1/input0" (stable per port)

    @property
    def port(self) -> str:
        """Stable USB port id from phys ('...-2.1'); the identical-pad tie-breaker."""
        if not self.phys:
            return ""
        return self.phys.split("-", 1)[-1].split("/", 1)[0]

    def stable_id(self) -> str:
        """Best available stable identity, in preference order. js index is NOT used —
        it reorders on replug. uniq (unique) > port path (stable per port) > vid:pid."""
        if self.uniq:
            return f"uniq:{self.uniq}"
        if self.port:
            return f"port:{self.vendor_id:04x}:{self.product_id:04x}:{self.port}"
        return f"vidpid:{self.vendor_id:04x}:{self.product_id:04x}"


def list_controllers() -> List[ControllerInfo]:
    """
    List all connected controllers using kernel /dev/input/js* devices.
    This is the canonical ordering used by the hook.
    """
    controllers = []

    # Every js node, not just js0-7: Steam's virtual pads take js numbers too
    # (4 pads + 4 virtuals = js0-7), so a pad connected after them lands at
    # js8+ and was invisible. Same limit as the hook's MAX_CONTROLLERS.
    for i in range(32):
        path = f"/dev/input/js{i}"

        if not os.path.exists(path):
            continue

        try:
            # Name from sysfs, the same string JSIOCGNAME returns. Opening the
            # js node just to ask costs ~40 ms per pad on close() (joydev
            # teardown), which stalls any UI thread that polls this.
            name = _read_sysfs(f"/sys/class/input/js{i}/device/name") or "Unknown"

            # Read VID:PID from sysfs
            vid, pid = 0, 0
            try:
                with open(f"/sys/class/input/js{i}/device/id/vendor") as f:
                    vid = int(f.read().strip(), 16)
            except (OSError, ValueError):
                pass
            try:
                with open(f"/sys/class/input/js{i}/device/id/product") as f:
                    pid = int(f.read().strip(), 16)
            except (OSError, ValueError):
                pass

            # Filter out Steam Input virtual controllers (Valve vendor, virtual Xbox 360 pad)
            if vid == 0x28de and pid == 0x11ff:
                continue

            # Stable identity attributes (see stable_id()): serial/MAC and USB port path
            uniq = _read_sysfs(f"/sys/class/input/js{i}/device/uniq")
            phys = _read_sysfs(f"/sys/class/input/js{i}/device/phys")
            if phys.startswith(JANUS_PHYS_PREFIX):
                continue    # Janus input mode's own virtual pad (padd.py)

            controllers.append(ControllerInfo(
                index=i,
                name=name,
                device_path=path,
                vendor_id=vid,
                product_id=pid,
                uniq=uniq,
                phys=phys,
            ))
        except OSError:
            continue

    # Gamepads without a js node (event node only), after the js pads.
    try:
        events = sorted(int(e[5:]) for e in os.listdir("/sys/class/input")
                        if e.startswith("event") and e[5:].isdigit())
    except OSError:
        events = []
    for n in events:
        if not _is_evdev_gamepad(n):
            continue
        dev = f"/sys/class/input/event{n}/device"
        try:
            vid = int(_read_sysfs(f"{dev}/id/vendor") or "0", 16)
            pid = int(_read_sysfs(f"{dev}/id/product") or "0", 16)
        except ValueError:
            vid = pid = 0
        controllers.append(ControllerInfo(
            index=EVDEV_INDEX_BASE + n,
            name=_read_sysfs(f"{dev}/name") or "Unknown",
            device_path=f"/dev/input/event{n}",
            vendor_id=vid,
            product_id=pid,
            uniq=_read_sysfs(f"{dev}/uniq"),
            phys=_read_sysfs(f"{dev}/phys"),
        ))

    return controllers


def format_controller_list(controllers: List[ControllerInfo]) -> str:
    """Format controller list for display."""
    if not controllers:
        return "No controllers connected."

    lines = []
    for c in controllers:
        lines.append(f"  [{c.index}] {c.name}")
        lines.append(f"      Device: {c.device_path}")

    return '\n'.join(lines)


def get_controller_by_index(index: int) -> Optional[ControllerInfo]:
    """Get a specific controller by kernel index."""
    controllers = list_controllers()
    for c in controllers:
        if c.index == index:
            return c
    return None


def get_controller_by_name(name_pattern: str) -> Optional[ControllerInfo]:
    """Find a controller whose name contains the given pattern."""
    controllers = list_controllers()
    for c in controllers:
        if name_pattern.lower() in c.name.lower():
            return c
    return None


if __name__ == '__main__':
    # Quick test
    controllers = list_controllers()
    print("Connected controllers (kernel ordering):")
    print(format_controller_list(controllers))
    print(f"\nTotal: {len(controllers)} controller(s)")

"""
Focus-release hotkey daemon for Janus.

Two backends: evdev (/dev/input/event*, needs 'input' group membership) and,
failing that, X RECORD on XWayland (python3-xlib) — which is what runs on a
default install and sees keys sent to the gamescope tiles. On the release hotkey
it asks KWin (D-Bus scripting) to activate another window, which takes focus
— and with it gamescope's pointer grab — away from the game tile. An optional
second hotkey invokes a callback (the GUI's stop-all).

Usage:
    python focus_release.py                    # Default: Alt+1
    python focus_release.py --key super+Escape # Custom key combo
"""

import glob as _glob
import os
import select
import struct
import sys


# ── evdev constants ──────────────────────────────────────────────────

EV_KEY = 0x01
_EVENT_FMT = '@llHHi'
_EVENT_SIZE = struct.calcsize(_EVENT_FMT)

# modifier name -> (left keycode, right keycode)
_MOD_GROUPS = {
    'ctrl':    (29, 97),    # KEY_LEFTCTRL, KEY_RIGHTCTRL
    'control': (29, 97),
    'alt':     (56, 100),   # KEY_LEFTALT, KEY_RIGHTALT
    'shift':   (42, 54),    # KEY_LEFTSHIFT, KEY_RIGHTSHIFT
    'super':   (125, 126),  # KEY_LEFTMETA, KEY_RIGHTMETA
    'mod4':    (125, 126),
}

# key name -> evdev keycode
_KEY_CODES = {
    '0': 11, '1': 2, '2': 3, '3': 4, '4': 5,
    '5': 6, '6': 7, '7': 8, '8': 9, '9': 10,
    'escape': 1, 'esc': 1, 'return': 28, 'enter': 28,
    'space': 57, 'tab': 15, 'backspace': 14, 'delete': 111,
    'pause': 119,
    'f1': 59, 'f2': 60, 'f3': 61, 'f4': 62,
    'f5': 63, 'f6': 64, 'f7': 65, 'f8': 66,
    'f9': 67, 'f10': 68, 'f11': 87, 'f12': 88,
    'a': 30, 'b': 48, 'c': 46, 'd': 32, 'e': 18,
    'f': 33, 'g': 34, 'h': 35, 'i': 23, 'j': 36,
    'k': 37, 'l': 38, 'm': 50, 'n': 49, 'o': 24,
    'p': 25, 'q': 16, 'r': 19, 's': 31, 't': 20,
    'u': 22, 'v': 47, 'w': 17, 'x': 45, 'y': 21,
    'z': 44,
}


# ── helpers ──────────────────────────────────────────────────────────

def _find_keyboards(required_keys=()):
    """Find keyboard event devices via sysfs capabilities.

    EV_KEY + EV_REP alone also matches some gamepads (a Switch-style "Pro
    Controller"), which get a uaccess ACL and so are readable without the
    'input' group — the daemon then listened to the pad only and never fell
    back to X RECORD. So the device must also have every key in
    *required_keys* (the hotkeys' keycodes)."""
    keyboards = []
    for dev_path in sorted(_glob.glob('/dev/input/event*')):
        dev_num = dev_path.rsplit('event', 1)[1]
        caps_dir = f'/sys/class/input/event{dev_num}/device/capabilities'
        try:
            with open(f'{caps_dir}/ev') as f:
                caps = int(f.read().strip(), 16)
            # EV_KEY (bit 1) + EV_REP (bit 20) = keyboard
            if not (caps & (1 << 1) and caps & (1 << 0x14)):
                continue
            with open(f'{caps_dir}/key') as f:
                words = f.read().split()  # hex words, most significant first
            key_bits = 0
            for word in words:
                key_bits = (key_bits << 64) | int(word, 16)
            if all(key_bits >> code & 1 for code in required_keys):
                keyboards.append(dev_path)
        except (FileNotFoundError, ValueError, PermissionError):
            continue
    return keyboards


def _parse_evdev_combo(combo_str):
    """Parse 'alt+1' -> (list of modifier code pairs, target keycode)."""
    parts = combo_str.lower().split('+')
    key_name = parts[-1]
    mod_names = parts[:-1]

    mod_groups = []
    for mod in mod_names:
        if mod not in _MOD_GROUPS:
            raise ValueError(f"Unknown modifier '{mod}'. Valid: {', '.join(_MOD_GROUPS)}")
        mod_groups.append(_MOD_GROUPS[mod])

    if key_name not in _KEY_CODES:
        raise ValueError(f"Unknown key '{key_name}'. Try: escape, f1-f12, pause, 1-9, a-z, etc.")

    return mod_groups, _KEY_CODES[key_name]


def _try_defocus_kwin():
    """Defocus via KWin scripting API."""

    script = """\
// Focus the desktop (Plasma's desktop window): the tile loses keyboard and
// pointer focus, which ends a mouse-look game's pointer grab, and nothing is
// raised over the tiles (an activated fullscreen window, e.g. Big Picture,
// would cover them even after re-raising). Clicking a tile gives it the mouse
// again. Without a desktop window: the topmost other window that isn't a
// tile (WM class janus-pN) and can take focus (helper windows such as the
// invisible Xwayland Video Bridge are "normal" too; activating one left focus
// and the grab on the tile), with the tiles raised again afterwards.
// In a function: KWin rejects "return" at the top level of a script
// ("Return statement not allowed outside of Function declaration"), and a
// script that doesn't load did nothing while the daemon reported success.
(function () {
var active = workspace.activeWindow;
if (!active) return;
var windows = workspace.windowList();
var isTile = function (w) { return String(w.resourceClass).indexOf("janus-p") === 0; };
var target = null;
for (var d = 0; d < windows.length; d++)
    if (windows[d].desktopWindow && windows[d].output === active.output) target = windows[d];
if (target) { workspace.activeWindow = target; return; }
for (var i = windows.length - 1; i >= 0; i--) {
    var w = windows[i];
    if (w === active || w.minimized || !w.normalWindow || !w.wantsInput ||
        w.skipTaskbar || w.skipSwitcher || isTile(w))
        continue;
    target = w;
    break;
}
if (!target) return;
workspace.activeWindow = target;
for (var j = 0; j < windows.length; j++)
    if (isTile(windows[j]) && !windows[j].minimized) workspace.raiseWindow(windows[j]);
})();
"""
    try:
        from wayland_session import kwin_run_script
    except ImportError:
        from .wayland_session import kwin_run_script
    return kwin_run_script(script, name='janus-defocus')


def release_soon(delay: float = 5.0):
    """Focus the desktop *delay* seconds from now, once (background thread).

    Called when a session's instances have all started: the tile KWin
    focused last would otherwise lock the pointer as soon as its game hides
    the cursor for mouse-look, and with one mouse nobody could reach the other
    tiles without knowing about Alt+1. Unfocused, no tile takes the mouse
    until it's clicked. Pads don't depend on focus."""
    import threading

    def run():
        if _try_defocus_kwin():
            print("[janus] Mouse released: click a game to give it the mouse, "
                  "Alt+1 to take it back", flush=True)
    t = threading.Timer(delay, run)
    t.daemon = True
    t.start()
    return t


# ── evdev backend ────────────────────────────────────────────────────

def _run_evdev(key_combo, stop_combo=None, stop_callback=None):
    """Read keyboard events from /dev/input/event* and dispatch hotkeys."""
    mod_groups, target_key = _parse_evdev_combo(key_combo)
    stop_mods = stop_target = None
    if stop_combo and stop_callback:
        stop_mods, stop_target = _parse_evdev_combo(stop_combo)

    required = {target_key} | {grp[0] for grp in mod_groups}
    if stop_target is not None:
        required |= {stop_target} | {grp[0] for grp in stop_mods}
    keyboards = _find_keyboards(required)
    if not keyboards:
        raise RuntimeError("No keyboard devices found in /dev/input/")

    fds = {}
    for kb in keyboards:
        try:
            fd = os.open(kb, os.O_RDONLY | os.O_NONBLOCK)
            fds[fd] = kb
        except (PermissionError, OSError):
            continue

    if not fds:
        raise RuntimeError(
            "Cannot open keyboard devices (permission denied). "
            "Try: sudo usermod -aG input $USER (then re-login)"
        )

    print(f"[janus] Focus-release daemon started via evdev (hotkey: {key_combo})")
    if stop_target is not None:
        print(f"[janus] Stop-all hotkey: {stop_combo}")
    print(f"[janus] Monitoring {len(fds)} keyboard device(s)")
    sys.stdout.flush()

    pressed = set()

    while True:
        try:
            readable, _, _ = select.select(list(fds.keys()), [], [], 1.0)
        except (ValueError, OSError):
            # An fd became invalid (device disconnected); rebuild set
            new_fds = {}
            for fd, path in fds.items():
                try:
                    os.fstat(fd)
                    new_fds[fd] = path
                except OSError:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            fds = new_fds
            if not fds:
                raise RuntimeError("All keyboard devices disconnected")
            continue

        for fd in readable:
            try:
                data = os.read(fd, _EVENT_SIZE * 64)
            except OSError:
                fds.pop(fd, None)
                try:
                    os.close(fd)
                except OSError:
                    pass
                continue

            offset = 0
            while offset + _EVENT_SIZE <= len(data):
                _, _, ev_type, ev_code, ev_value = struct.unpack(
                    _EVENT_FMT, data[offset:offset + _EVENT_SIZE]
                )
                offset += _EVENT_SIZE

                if ev_type != EV_KEY:
                    continue

                if ev_value == 1:  # key press
                    pressed.add(ev_code)
                    if ev_code == target_key and all(
                        any(c in pressed for c in grp) for grp in mod_groups
                    ):
                        if _try_defocus_kwin():
                            print("[janus] Defocused game window", flush=True)
                        else:
                            print("[janus] KWin defocus failed", flush=True)
                    elif stop_target is not None and ev_code == stop_target and all(
                        any(c in pressed for c in grp) for grp in stop_mods
                    ):
                        print(f"[janus] {stop_combo} pressed — invoking stop callback", flush=True)
                        try:
                            stop_callback()
                        except Exception as e:
                            print(f"[janus] stop_callback error: {e}", flush=True)
                elif ev_value == 0:  # key release
                    pressed.discard(ev_code)


# ── X RECORD backend (fallback) ─────────────────────────────────────

def _run_xrecord(key_combo, stop_combo=None, stop_callback=None):
    """Fallback backend: X RECORD on the (X)Wayland display.

    Needs no 'input' group membership. Under KWin Wayland it sees keys
    delivered to XWayland clients — which includes the gamescope tiles (their
    SDL backend runs on XWayland), so the hotkeys work while a tile is focused.
    """
    from Xlib import X, XK
    from Xlib.display import Display
    from Xlib.ext import record
    from Xlib.protocol import rq

    MODIFIER_MAP = {
        'ctrl': X.ControlMask, 'control': X.ControlMask,
        'alt': X.Mod1Mask, 'shift': X.ShiftMask,
        'super': X.Mod4Mask, 'mod4': X.Mod4Mask,
    }
    LOCK_MASKS = X.Mod2Mask | X.LockMask

    KEY_MAP = {
        'escape': 'Escape', 'esc': 'Escape',
        'return': 'Return', 'enter': 'Return',
        'space': 'space', 'tab': 'Tab',
        'backspace': 'BackSpace', 'delete': 'Delete', 'pause': 'Pause',
        'f1': 'F1', 'f2': 'F2', 'f3': 'F3', 'f4': 'F4',
        'f5': 'F5', 'f6': 'F6', 'f7': 'F7', 'f8': 'F8',
        'f9': 'F9', 'f10': 'F10', 'f11': 'F11', 'f12': 'F12',
    }

    def _parse(combo):
        parts = combo.lower().split('+')
        key_name = parts[-1]
        mod_names = parts[:-1]
        m = 0
        for mod in mod_names:
            if mod not in MODIFIER_MAP:
                raise ValueError(f"Unknown modifier '{mod}'")
            m |= MODIFIER_MAP[mod]
        xk_name = KEY_MAP.get(key_name, key_name)
        keysym = XK.string_to_keysym(xk_name)
        if keysym == 0:
            raise ValueError(f"Unknown key '{key_name}'")
        return m, keysym

    mask, keysym = _parse(key_combo)
    stop_mask = stop_keycode = None
    if stop_combo and stop_callback:
        stop_mask, stop_keysym = _parse(stop_combo)

    record_display = Display()
    target_keycode = record_display.keysym_to_keycode(keysym)
    if target_keycode == 0:
        raise RuntimeError(f"Could not map '{key_combo}' to a keycode")
    if stop_mask is not None:
        stop_keycode = record_display.keysym_to_keycode(stop_keysym)
        if stop_keycode == 0:
            raise RuntimeError(f"Could not map '{stop_combo}' to a keycode")

    ctx = record_display.record_create_context(
        0, [record.AllClients],
        [{'core_requests': (0, 0), 'core_replies': (0, 0),
          'ext_requests': (0, 0, 0, 0), 'ext_replies': (0, 0, 0, 0),
          'delivered_events': (0, 0),
          'device_events': (X.KeyPress, X.KeyRelease),
          'errors': (0, 0), 'client_started': False, 'client_died': False}],
    )

    print(f"[janus] Focus-release daemon started via X RECORD (hotkey: {key_combo})")
    sys.stdout.flush()

    def callback(reply):
        try:
            if reply.category != record.FromServer or reply.client_swapped:
                return
            data = reply.data
            while len(data):
                event, data = rq.EventField(None).parse_binary_value(
                    data, record_display.display, None, None,
                )
                if event.type == X.KeyPress:
                    state = event.state & ~LOCK_MASKS
                    if event.detail == target_keycode and state == mask:
                        if _try_defocus_kwin():
                            print("[janus] Defocused game window", flush=True)
                        else:
                            print("[janus] KWin defocus failed", flush=True)
                    elif stop_keycode is not None and event.detail == stop_keycode and state == stop_mask:
                        print(f"[janus] {stop_combo} pressed — invoking stop callback", flush=True)
                        try:
                            stop_callback()
                        except Exception as e:
                            print(f"[janus] stop_callback error: {e}", flush=True)
        except Exception as e:
            print(f"[janus] Error in X RECORD callback: {e}", flush=True)

    record_display.record_enable_context(ctx, callback)


# ── public API ───────────────────────────────────────────────────────

def run_daemon(key_combo='alt+1', stop_combo='alt+2', stop_callback=None):
    """
    Run the focus-release hotkey daemon (blocks; run it in a thread).

    stop_combo + stop_callback: optional second hotkey that invokes the
    callback in-thread (caller is responsible for thread-safety, e.g.
    emitting a Qt signal that's delivered to the main thread).
    """
    print(f"[janus] Press {key_combo} to release focus from gamescope windows")
    sys.stdout.flush()
    try:
        _run_evdev(key_combo, stop_combo, stop_callback)
        return
    except (RuntimeError, ValueError) as e:
        # Expected without 'input' group membership (keyboards are root:input).
        print(f"[janus] evdev backend unavailable: {e}", file=sys.stderr)
        print("[janus] Trying X RECORD fallback...", file=sys.stderr)
        sys.stderr.flush()
    try:
        _run_xrecord(key_combo, stop_combo, stop_callback)
    except Exception as e:
        print(f"[janus] X RECORD backend failed: {e}", file=sys.stderr)
        print("[janus] Focus-release hotkey DISABLED (no backend available)",
              file=sys.stderr)
        sys.stderr.flush()


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description='Focus-release hotkey daemon for Janus',
    )
    parser.add_argument(
        '--key', default='alt+1',
        help='Hotkey combo (default: alt+1). Format: mod1+mod2+key',
    )
    args = parser.parse_args()

    run_daemon(args.key)


if __name__ == '__main__':
    main()

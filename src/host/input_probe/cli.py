"""Terminal renderer for the input probe. `python3 -m src.host.input_probe`.

Owns a select() loop over the session's evdev fds; polls Steam Input and redraws on a
timer. Debug-friendly (raw handles, sysfs origin, event codes).
"""
from __future__ import annotations

import select
import sys
import time

from .session import ProbeSession

_RESET, _DIM, _BOLD = "\033[0m", "\033[2m", "\033[1m"
_GREEN, _YELLOW, _RED, _CYAN = "\033[32m", "\033[33m", "\033[31m", "\033[36m"
_CLEAR = "\033[2J\033[H"


def _render(session: ProbeSession) -> str:
    snap = session.snapshot()
    a = snap.activity
    color = {"ok": _GREEN, "warn": _YELLOW, "error": _RED}.get(a.level, "")
    out = [f"{_BOLD}input probe{_RESET}   "
           f"steam={'yes' if a.steam_running else 'no'}  "
           f"game={a.game_appid or '-'}  "
           f"focused={'yes' if a.game_focused else ('no' if a.game_focused is False else '?')}  "
           f"steam_input={'active' if a.steam_input_active else 'idle'}"]
    if a.message:
        out.append(f"{color}⚠ {a.message}{_RESET}" if a.level != "ok" else f"{color}{a.message}{_RESET}")
    out.append("")

    phys = [d for d in snap.devices if not d.is_virtual]
    virt = [d for d in snap.devices if d.is_virtual]

    def block(title, devs, col):
        out.append(f"{_BOLD}{col}{title}{_RESET}")
        if not devs:
            out.append(f"  {_DIM}(none){_RESET}")
        for d in devs:
            held = " ".join(sorted(d.pressed)) or f"{_DIM}—{_RESET}"
            out.append(f"  {col}{d.event}/{d.js:<4}{_RESET} {d.vid}:{d.pid} "
                       f"{_DIM}{d.name[:24]:<24}{_RESET}  held:[{held}]  "
                       f"{_DIM}last:{d.last_label}{_RESET}")

    block("PHYSICAL evdev", phys, _CYAN)
    out.append("")
    block("VIRTUAL evdev (Steam)", virt, _YELLOW)
    out.append("")
    out.append(f"{_BOLD}ISteamInput (IPC){_RESET}")
    if not snap.steam_controllers:
        reason = "" if a.ipc_available else f" {_DIM}({session.steam.error}){_RESET}"
        out.append(f"  {_DIM}(no controllers){_RESET}{reason}")
    for c in snap.steam_controllers:
        out.append(f"  handle=0x{c.handle:016x}  type={c.input_type}  gamepad_index={c.gamepad_index}")

    out.append("")
    out.append(f"{_BOLD}MAPPING  expected controller → Steam virtual → IPC handle{_RESET}"
               f"  {_DIM}(press each pad to verify){_RESET}")
    if not snap.mapping:
        out.append(f"  {_DIM}(no physical controllers){_RESET}")
    for m in snap.mapping:
        smark = {"ok": f"{_GREEN}✓ verified{_RESET}",
                 "mismatch": f"{_RED}✗ MISMATCH (order guess wrong!){_RESET}",
                 "unverified": f"{_DIM}? press pad to verify{_RESET}"}[m.status]
        hnd = f"0x{m.handle:016x}" if m.handle is not None else "-"
        out.append(f"  {_CYAN}{m.phys_js}/{m.phys_event}{_RESET} {m.phys_id} "
                   f"{_DIM}port {m.phys_port}{_RESET}")
        out.append(f"      expected→{m.expected_virtual or '-':<8}  "
                   f"detected→{(m.detected_virtual or '-'):<8}(votes {m.votes})  "
                   f"gp_index={m.gamepad_index if m.gamepad_index is not None else '-'}  "
                   f"handle={hnd}  {smark}")
    out.append(f"\n{_DIM}Ctrl-C to quit{_RESET}")
    return "\n".join(out)


def main() -> None:
    session = ProbeSession()
    last_steam = 0.0
    last_refresh = 0.0
    try:
        while True:
            fds = session.fds()
            ready, _, _ = select.select(list(fds), [], [], 0.1)
            for fd in ready:
                session.pump(fd)
            now = time.monotonic()
            if now - last_steam >= 0.2:
                session.poll_steam()
                last_steam = now
            if now - last_refresh >= 1.0:
                session.refresh()   # hot-plug / virtual-pad appearance
                last_refresh = now
            sys.stdout.write(_CLEAR + _render(session) + "\n")
            sys.stdout.flush()
    except KeyboardInterrupt:
        pass
    finally:
        session.close()
        sys.stdout.write(_RESET + "\n")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Summarize per-instance janus-hook logs from a verbose session.

Reads ~/.local/share/janus/logs/<appid>/i<N>.<pid> files (written when a session
runs with verbose logging) and prints, per instance:
  - the assigned controller + Steam-Input mapping
  - the FINAL evdev/js/hidraw ALLOW/BLOCK decision per device
  - a LEAK warning if an instance allowed a device that isn't its assigned one

Usage:
    python3 scripts/hooklog.py <appid>
    python3 scripts/hooklog.py            # newest appid dir under logs/
"""
import os, re, sys, glob
from pathlib import Path
from collections import defaultdict

LOGROOT = Path.home() / '.local' / 'share' / 'janus' / 'logs'


def newest_appid_dir():
    dirs = [d for d in LOGROOT.glob('*') if d.is_dir()]
    return max(dirs, key=lambda d: d.stat().st_mtime) if dirs else None


def main():
    if len(sys.argv) > 1:
        appdir = LOGROOT / sys.argv[1]
    else:
        appdir = newest_appid_dir()
    if not appdir or not appdir.is_dir():
        print(f"No logs found under {LOGROOT}. Run a session with verbose logging first.")
        return

    # Scope to the MOST RECENT session only — the log dir accumulates a file per pid
    # across every run, and mixing an old session's files in silently shows stale results
    # (e.g. a leak that's since been fixed). A session's processes all run within a short
    # window, and sessions are separated by longer gaps, so keep files within SESSION_WINDOW
    # of the newest file. Pass --all to include every run.
    SESSION_WINDOW = 180.0  # seconds
    all_files = [f for f in appdir.glob('i*.*') if re.match(r'i\d+\.\d+$', f.name)]
    if not all_files:
        print("No i<N>.<pid> files found.")
        return
    if '--all' not in sys.argv:
        newest = max(f.stat().st_mtime for f in all_files)
        kept = [f for f in all_files if f.stat().st_mtime >= newest - SESSION_WINDOW]
        dropped = len(all_files) - len(kept)
        print(f"== hook logs: {appdir} == (latest session: {len(kept)} files"
              f"{f', {dropped} older files hidden — use --all' if dropped else ''})\n")
        all_files = kept
    else:
        print(f"== hook logs: {appdir} == (--all: {len(all_files)} files across all runs)\n")

    # Group pid files by instance (filename i<N>.<pid>)
    inst_files = defaultdict(list)
    for f in all_files:
        m = re.match(r'i(\d+)\.\d+$', f.name)
        if m:
            inst_files[int(m.group(1))].append(f)

    if not inst_files:
        print("No i<N>.<pid> files found.")
        return

    for inst in sorted(inst_files):
        lines = []
        for f in inst_files[inst]:
            try:
                lines += f.read_text(errors='replace').splitlines()
            except OSError:
                pass

        assigned = steam_active = assigned_vjs = None
        banner = None
        decisions = {}  # device -> last "ALLOWED"/"BLOCKED (reason)"
        for ln in lines:
            if 'Janus hook loaded' in ln and banner is None:
                banner = ln.split('=== ', 1)[-1]
                m = re.search(r'JANUS_CONTROLLER=(\S+)', ln)
                if m:
                    assigned = m.group(1)
            if 'Steam Input: ACTIVE' in ln:
                steam_active = True
            if 'Steam Input: No virtual' in ln or 'Steam Input: disabled' in ln:
                steam_active = steam_active or False
            m = re.search(r'Assigned physical \d+ maps to virtual js(\d+)', ln)
            if m:
                assigned_vjs = int(m.group(1))
            m = re.search(r'(?:evdev|hidraw): (\S+) -> (ALLOWED|BLOCKED)(.*)', ln)
            if m:
                dev, verdict, reason = m.group(1), m.group(2), m.group(3).strip()
                decisions[dev] = f"{verdict} {reason}".strip()

        print(f"--- instance {inst}  (assigned JANUS_CONTROLLER={assigned}, "
              f"steam_input_active={steam_active}, assigned_virtual_js={assigned_vjs}) ---")
        if banner:
            print(f"    {banner}")
        allowed = [d for d, v in decisions.items() if v.startswith('ALLOWED')]
        blocked = [d for d, v in decisions.items() if v.startswith('BLOCKED')]
        for d in sorted(allowed):
            print(f"    ALLOW  {d:24} {decisions[d]}")
        for d in sorted(blocked):
            print(f"    block  {d:24} {decisions[d]}")
        # Heuristic leak flag: an allowed joystick/event device is fine only if it
        # is the assigned virtual pad or the pass-through physical. Surface every
        # allowed *joystick* device so cross-instance overlap is obvious at a glance.
        allowed_js = [d for d in allowed if re.search(r'(js|event)\d+$', d)]
        print(f"    >>> ALLOWED joystick devices: {allowed_js}\n")

    print("Compare 'ALLOWED joystick devices' across instances: if two instances "
          "allow the same js/event node, that's the leak.")


if __name__ == '__main__':
    main()

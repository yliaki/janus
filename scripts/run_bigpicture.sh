#!/usr/bin/env bash
#
# run_bigpicture.sh — launch Janus's fullscreen, controller-driven UI.
#
# This is also the command to add to Steam as a non-Steam game. Steam runs
# shortcuts with its runtime's LD_LIBRARY_PATH/PATH, which can break the
# system Python/Qt; it keeps the originals in SYSTEM_*, so restore them
# before Python starts. (main.py scrubs the rest of Steam's env.)
#
# Usage:
#   scripts/run_bigpicture.sh             # fullscreen
#   scripts/run_bigpicture.sh --windowed  # windowed, for testing
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

if [ -n "${SYSTEM_LD_LIBRARY_PATH+x}" ]; then
    export LD_LIBRARY_PATH="$SYSTEM_LD_LIBRARY_PATH"
    [ -z "$LD_LIBRARY_PATH" ] && unset LD_LIBRARY_PATH
fi
if [ -n "${SYSTEM_PATH:-}" ]; then
    export PATH="$SYSTEM_PATH"
fi
# Steam preloads its overlay into every shortcut, overlay setting on or off.
# Loaded into Python it breaks EGL for the Qt bundled in the zip ("qt.qpa.wayland:
# EGL not available", then no OpenGL and an instant exit), so keep it out of
# this process altogether, not just out of the games' environment (main.py).
if [ -n "${LD_PRELOAD:-}" ]; then
    LD_PRELOAD="$(printf '%s' "$LD_PRELOAD" | tr ': ' '\n\n' | grep -v 'gameoverlayrenderer\.so' | paste -sd: - || true)"
    if [ -n "$LD_PRELOAD" ]; then export LD_PRELOAD; else unset LD_PRELOAD; fi
fi

# Installed from the zip (scripts/package.py): use the bundled PySide6.
[ -d "$PWD/pylib" ] && export PYTHONPATH="$PWD/pylib${PYTHONPATH:+:$PYTHONPATH}"

exec python3 -m src.bigpicture.main "$@"

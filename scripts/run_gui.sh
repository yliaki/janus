#!/usr/bin/env bash
#
# run_gui.sh — launch the Janus GUI from anywhere.
#
# cds to the repo root, runs a quick dependency preflight (required items only),
# and execs the PySide6 GUI. Any extra args are passed through to main.py.
#
# Usage:
#   scripts/run_gui.sh                 # preflight + launch
#   scripts/run_gui.sh --skip-checks   # launch immediately, no preflight
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# Installed from the zip (scripts/package.py): use the bundled PySide6.
[ -d "$REPO_ROOT/pylib" ] && export PYTHONPATH="$REPO_ROOT/pylib${PYTHONPATH:+:$PYTHONPATH}"

SKIP_CHECKS=0
if [ "${1:-}" = "--skip-checks" ]; then
    SKIP_CHECKS=1
    shift
fi

if [ "$SKIP_CHECKS" -eq 0 ]; then
    # --quiet prints only problems; non-zero exit means a REQUIRED dep is missing.
    # gamescope and Steam are checked by the GUI itself (with install hints).
    if ! report="$("$SCRIPT_DIR/check_gui_deps.sh" --quiet --startup)"; then
        echo "$report"
        echo
        echo "Required dependencies are missing (see above)." >&2
        echo "Fix them, or re-run with --skip-checks to launch anyway." >&2
        # Started from the menu: nobody sees the terminal.
        if [ ! -t 1 ]; then
            msg="Janus can't start, required programs are missing:

$(printf '%s\n' "$report" | sed 's/\x1b\[[0-9;]*m//g' | grep '✗')"
            if command -v kdialog >/dev/null 2>&1; then
                kdialog --title "Janus Split Screen" --error "$msg"
            elif command -v zenity >/dev/null 2>&1; then
                zenity --error --title "Janus Split Screen" --text "$msg"
            fi
        fi
        exit 1
    fi
fi

exec python3 src/gui/main.py "$@"

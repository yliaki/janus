#!/usr/bin/env bash
#
# run_cli.sh — the Janus command line (src/host/main.py) from anywhere.
# Installed copies call this as `janus-cli`.
#
# Usage:
#   scripts/run_cli.sh list
#   scripts/run_cli.sh steam-split <appid> --tile 0:0,0,1920,2160 ...
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Installed from the zip (scripts/package.py): use the bundled PySide6.
[ -d "$REPO_ROOT/pylib" ] && export PYTHONPATH="$REPO_ROOT/pylib${PYTHONPATH:+:$PYTHONPATH}"

exec python3 "$REPO_ROOT/src/host/main.py" "$@"

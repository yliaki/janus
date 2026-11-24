#!/usr/bin/env bash
#
# dev_uninstall.sh — remove the per-user install (~/.local) made by the zip's
# install.sh or scripts/dev_install.sh. Uses the repo's packaging/uninstall.sh,
# so it also cleans up after a broken install. Keeps your data (settings,
# profiles, saves, handlers) unless --purge.
#
# Usage:
#   scripts/dev_uninstall.sh            # remove the program, no questions
#   scripts/dev_uninstall.sh --purge    # also ~/.config/janus + ~/.local/share/janus (asks)
#
# The .deb is removed with: sudo apt remove janus-split-screen
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$SCRIPT_DIR/../packaging/uninstall.sh" --yes "$@"

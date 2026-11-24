#!/usr/bin/env bash
#
# uninstall.sh — remove a per-user Janus Split Screen install (packaging/install.sh).
#
# Copied into the installed program as uninstall.sh. Removes the program, its
# commands, menu entries, icon and cache (~/.cache/janus: the handler hub's
# list), and asks whether to also remove the Steam shortcut and your data
# (settings, profiles, saves, the handlers you got from the hub). Asks through dialog
# boxes (scripts/dialogs.sh), the terminal only when there's no desktop session.
# (For the .deb: sudo apt remove janus-split-screen.)
#
# Usage:
#   uninstall.sh                   # asks
#   uninstall.sh --yes             # no questions: keeps the Steam shortcut and your data
#   uninstall.sh --steam-shortcut  # also remove the Steam shortcut, no question
#   uninstall.sh --purge           # also delete your data (still asks to confirm)
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_HOME="$HOME/.local/share"
CONFIG_HOME="$HOME/.config"
CACHE_HOME="${XDG_CACHE_HOME:-$HOME/.cache}"
APP_DIR="$HOME/.local/opt/janus"
BIN_DIR="$HOME/.local/bin"

YES=0
PURGE=0
SHORTCUT=ask
for a in "$@"; do
    case "$a" in
        -y|--yes) YES=1 ;;
        --purge) PURGE=1 ;;
        --steam-shortcut) SHORTCUT=yes ;;
        --no-steam-shortcut) SHORTCUT=no ;;
        -h|--help) sed -n '2,/^set /p' "$0" | sed '$d;s/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $a (see --help)" >&2; exit 2 ;;
    esac
done
[ "$YES" -eq 1 ] && [ "$SHORTCUT" = ask ] && SHORTCUT=no

# Installed: $HERE is the program dir; from the repo: packaging/.
[ -d "$APP_DIR/pylib" ] && export PYTHONPATH="$APP_DIR/pylib${PYTHONPATH:+:$PYTHONPATH}"
for d in "$HERE/scripts" "$HERE/../scripts"; do
    if [ -f "$d/dialogs.sh" ]; then
        . "$d/dialogs.sh"
        break
    fi
done
if ! declare -F ask >/dev/null; then
    UI=terminal
    ask() { [ -t 0 ] || return 1; read -r -p "$1 [y/N] " r; [[ "${r,,}" =~ ^y(es)?$ ]]; }
    say() { echo "$2"; }
fi

# --- questions first, then do it ---------------------------------------------

if [ "$YES" -eq 0 ] && ! ask "Uninstall Janus Split Screen?

You'll be asked next whether to keep your settings, player profiles and saves." \
        "Uninstall" "Cancel"; then
    exit 1
fi

shortcut_script="$APP_DIR/scripts/install_steam_shortcut.sh"
if [ "$SHORTCUT" = ask ]; then
    SHORTCUT=no
    if [ -x "$shortcut_script" ] && ask "Also remove Janus from your Steam library?

Steam will be closed and started again. Choose No if you never added it." \
            "Remove from Steam" "No"; then
        SHORTCUT=yes
    fi
fi

DELETE_DATA=0
if [ "$PURGE" -eq 1 ] || [ "$YES" -eq 0 ]; then
    if ask "Also delete all your Janus data?

This deletes the settings, player profiles, the handlers you got from the handler hub and the game saves of every player except your Steam account's own:

    $CONFIG_HOME/janus
    $DATA_HOME/janus

This can't be undone." "Delete my data" "Keep my data" warning \
    && ask "Are you sure?

The saves of every player profile except your Steam account's own will be permanently deleted, along with all Janus settings and profiles." \
        "Yes, delete it" "Keep my data" warning \
    && ask "Are you really sure? This is the last question.

Everything in $CONFIG_HOME/janus and $DATA_HOME/janus will be deleted for good." \
        "Delete everything" "Keep my data" warning; then
        DELETE_DATA=1
    fi
fi

# The Steam shortcut points into the program dir, so remove it first.
if [ "$SHORTCUT" = yes ] && [ -x "$shortcut_script" ]; then
    "$shortcut_script" --remove -y || true
fi

rm -rf "$APP_DIR"
rm -f "$BIN_DIR/janus" "$BIN_DIR/janus-bigpicture" "$BIN_DIR/janus-cli" \
      "$BIN_DIR/janus-steam-shortcut" \
      "$DATA_HOME/applications/janus-split-screen.desktop" \
      "$DATA_HOME/applications/janus-split-screen-bigpicture.desktop" \
      "$DATA_HOME/icons/hicolor/scalable/apps/janus-split-screen.svg"
# Only a cache (the handler hub's list): nothing to keep.
rm -rf "$CACHE_HOME/janus"
command -v update-desktop-database >/dev/null 2>&1 \
    && update-desktop-database "$DATA_HOME/applications" >/dev/null 2>&1 || true

if [ "$DELETE_DATA" -eq 1 ]; then
    rm -rf "$CONFIG_HOME/janus" "$DATA_HOME/janus"
    msg="Janus Split Screen and all its data were removed."
else
    msg="Janus Split Screen was removed.

Your settings, profiles and saves are kept in $CONFIG_HOME/janus and $DATA_HOME/janus, and the handlers you got from the handler hub in $CONFIG_HOME/janus/games."
fi
echo "$msg"
# A PySide6 dialog may have gone with the program; the line above covers that.
[ "$YES" -eq 1 ] || [ "$UI" = terminal ] || say info "$msg"

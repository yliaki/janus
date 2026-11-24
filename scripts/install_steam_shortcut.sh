#!/usr/bin/env bash
#
# install_steam_shortcut.sh — add Janus (Big Picture launcher) to Steam as a
# non-Steam game, with the Steam overlay off and artwork from
# src/bigpicture/assets/steam/ (see the README there).
#
# Steam only reads shortcuts at startup and overwrites them on exit, so a
# running Steam is shut down first (after asking) and started again after.
# Re-running updates the existing shortcut instead of adding another.
#
# Talks through dialog boxes when there's a desktop session (scripts/dialogs.sh).
#
# Usage:
#   scripts/install_steam_shortcut.sh             # add / update
#   scripts/install_steam_shortcut.sh --remove    # remove it
#   scripts/install_steam_shortcut.sh -y          # don't ask before closing Steam
#   scripts/install_steam_shortcut.sh --name "Janus Co-op"
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

# Installed from the zip (scripts/package.py): use the bundled PySide6.
[ -d "$PWD/pylib" ] && export PYTHONPATH="$PWD/pylib${PYTHONPATH:+:$PYTHONPATH}"

YES=0
REMOVE=0
ARGS=()
for a in "$@"; do
    case "$a" in
        -y|--yes) YES=1 ;;
        --remove) REMOVE=1; ARGS+=("$a") ;;
        *) ARGS+=("$a") ;;
    esac
done

. scripts/dialogs.sh

STEAM=()
if command -v steam >/dev/null 2>&1; then
    STEAM=(steam)
elif command -v flatpak >/dev/null 2>&1 && flatpak info com.valvesoftware.Steam >/dev/null 2>&1; then
    STEAM=(flatpak run com.valvesoftware.Steam)
fi

RESTART=0
if pgrep -x steam >/dev/null; then
    if [ "$YES" -eq 0 ] && ! ask "Steam has to be closed to change its shortcuts. It will be closed now and started again afterwards."; then
        say info "Nothing was changed."
        exit 1
    fi
    [ "$UI" = terminal ] && echo "Closing Steam…"
    [ ${#STEAM[@]} -gt 0 ] && "${STEAM[@]}" -shutdown >/dev/null 2>&1 || true
    for _ in $(seq 60); do
        pgrep -x steam >/dev/null || break
        sleep 1
    done
    if pgrep -x steam >/dev/null; then
        say error "Steam didn't close within 60 seconds. Close it yourself and run this again."
        exit 1
    fi
    RESTART=1
fi

status=0
output="$(python3 -m src.bigpicture.install_shortcut ${ARGS[@]+"${ARGS[@]}"} 2>&1)" || status=$?

restart_note=""
if [ "$RESTART" -eq 1 ]; then
    if [ ${#STEAM[@]} -gt 0 ]; then
        setsid -f "${STEAM[@]}" >/dev/null 2>&1 </dev/null
    else
        restart_note=$'\n\nCouldn\'t find the steam command; start Steam yourself.'
    fi
fi

if [ "$status" -ne 0 ]; then
    say error "Changing the Janus shortcut in Steam failed:"$'\n\n'"$output$restart_note"
    exit "$status"
fi
if [ "$REMOVE" -eq 1 ]; then
    done_msg="Janus was removed from Steam."
else
    done_msg="Done! Janus is now in your Steam library."
fi
[ "$UI" = terminal ] && done_msg="$output"$'\n'"$done_msg"
say info "$done_msg$restart_note"

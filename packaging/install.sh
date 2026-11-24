#!/usr/bin/env bash
#
# install.sh — install Janus Split Screen for the current user.
#
# Ships at the top of the zip made by scripts/package.py, next to app/. Meant
# to be double-clicked: asks and reports through dialog boxes
# (scripts/dialogs.sh), the terminal only when there's no desktop session.
# Nothing needs root: the program goes to ~/.local/opt/janus, the commands to
# ~/.local/bin, menu entries to ~/.local/share/applications. Re-running it
# (e.g. from a newer zip) replaces the installed program; your settings,
# profiles, saves and handlers live elsewhere and are kept.
#
# Usage:
#   ./install.sh                    # asks
#   ./install.sh --yes              # no questions (no Steam shortcut)
#   ./install.sh --steam-shortcut   # also add Janus to Steam without asking
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/app"

DATA_HOME="$HOME/.local/share"
APP_DIR="$HOME/.local/opt/janus"
BIN_DIR="$HOME/.local/bin"
APPS_DIR="$DATA_HOME/applications"
ICON_DIR="$DATA_HOME/icons/hicolor/scalable/apps"
HANDLER_DIR="$HOME/.config/janus/games"

YES=0
SHORTCUT=ask
for a in "$@"; do
    case "$a" in
        -y|--yes) YES=1 ;;
        --steam-shortcut) SHORTCUT=yes ;;
        --no-steam-shortcut) SHORTCUT=no ;;
        -h|--help) sed -n '2,/^set /p' "$0" | sed '$d;s/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $a (see --help)" >&2; exit 2 ;;
    esac
done
[ "$YES" -eq 1 ] && [ "$SHORTCUT" = ask ] && SHORTCUT=no

# Dialogs; the PySide6 fallback can use the Qt bundled in the zip.
if [ -f "$SRC/scripts/dialogs.sh" ]; then
    export PYTHONPATH="$SRC/pylib${PYTHONPATH:+:$PYTHONPATH}"
    . "$SRC/scripts/dialogs.sh"
else
    UI=terminal
    say() { echo "$2" >&2; }
fi
die() { busy_stop 2>/dev/null || true; say error "$1"; exit 1; }
trap 'die "Installing Janus failed unexpectedly (install.sh line $LINENO). Run it from a terminal to see the details."' ERR

# --- sanity checks -----------------------------------------------------------

[ -f "$SRC/VERSION" ] || die "Run install.sh from the unpacked zip: the app folder wasn't found next to it."
[ "$(id -u)" -ne 0 ] || die "Don't run the installer as root (or with sudo): it installs Janus for your user only."
[ "$(uname -m)" = x86_64 ] || die "Janus needs an x86_64 (64-bit PC) system."
command -v python3 >/dev/null 2>&1 || die "Python 3 wasn't found. Install Python 3.10 or newer, then run the installer again."
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' \
    || die "Janus needs Python 3.10 or newer, this system has $(python3 --version 2>&1)."

VERSION="$(cat "$SRC/VERSION")"
OLD_VERSION="$(cat "$APP_DIR/VERSION" 2>/dev/null || true)"

intro="Install Janus Split Screen $VERSION?

It's installed for your user only (no password needed) and added to your application menu."
if [ -n "$OLD_VERSION" ]; then
    intro+="

This replaces the installed version $OLD_VERSION. Your settings, player profiles and saves are kept."
fi
if [ "$YES" -eq 0 ] && ! ask "$intro" "Install" "Cancel"; then
    exit 1
fi
echo "Installing Janus Split Screen $VERSION into $APP_DIR ..."
busy_start "Installing Janus Split Screen…"

# --- program files -----------------------------------------------------------

# Copy beside the old install, then swap, so a failed copy leaves it intact.
mkdir -p "$(dirname "$APP_DIR")"
tmp="$(mktemp -d "$APP_DIR.new.XXXXXX")"
trap 'rm -rf "$tmp"' EXIT
cp -a "$SRC/." "$tmp/"
cp "$HERE/uninstall.sh" "$tmp/uninstall.sh"
# Unzip tools don't always keep the executable bit.
chmod +x "$tmp"/scripts/*.sh "$tmp"/scripts/*.py "$tmp/uninstall.sh" 2>/dev/null || true
rm -rf "$APP_DIR"
mv "$tmp" "$APP_DIR"
trap - EXIT

# --- commands, menu entries, icon -------------------------------------------

mkdir -p "$BIN_DIR" "$APPS_DIR" "$ICON_DIR"
wrapper() {  # NAME SCRIPT
    printf '#!/bin/sh\nexec "%s/scripts/%s" "$@"\n' "$APP_DIR" "$2" > "$BIN_DIR/$1"
    chmod +x "$BIN_DIR/$1"
}
wrapper janus run_gui.sh
wrapper janus-bigpicture run_bigpicture.sh
wrapper janus-cli run_cli.sh
wrapper janus-steam-shortcut install_steam_shortcut.sh

cp "$APP_DIR/src/gui/assets/gamepad.svg" "$ICON_DIR/janus-split-screen.svg"
for f in "$APP_DIR"/packaging/*.desktop; do
    sed "s|@BINDIR@|$BIN_DIR/|g" "$f" > "$APPS_DIR/$(basename "$f")"
done
command -v update-desktop-database >/dev/null 2>&1 \
    && update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true

# --- your own handlers ------------------------------------------------------

mkdir -p "$HANDLER_DIR"
cp "$APP_DIR/packaging/handlers-README.txt" "$HANDLER_DIR/README.txt"

# --- dependency report -------------------------------------------------------

deps="$(PYTHONPATH="$APP_DIR/pylib" "$APP_DIR/scripts/check_gui_deps.sh" 2>&1 || true)"
busy_stop
echo "Installed."
[ "$UI" = terminal ] && printf '\n%s\n' "$deps" | sed '/dependency check/d'
# "✗" = required and missing, "○" = optional and missing (check_gui_deps.sh).
required="$(printf '%s\n' "$deps" | grep '✗' | grep -v '✗ gamescope ' | sed 's/^ *✗ */• /;s/ *\[[A-Z]*\] */: /' || true)"
# gamescope's install hint spans several lines (per distribution): show it whole.
gamescope_msg="$(python3 "$APP_DIR/src/host/prereqs.py" gamescope 2>&1)" && gamescope_msg=""
optional="$(printf '%s\n' "$deps" | grep '○' | sed 's/^ *○ */• /;s/ *\[[A-Z]*\] */: /' || true)"

# --- Steam shortcut ----------------------------------------------------------

if [ "$SHORTCUT" = ask ]; then
    SHORTCUT=no
    if ask "Add Janus Big Picture to your Steam library?

Then you can start it from Steam with a controller. Steam will be closed and started again for this." \
            "Add to Steam" "Not now"; then
        SHORTCUT=yes
    fi
fi
shortcut_note="You can add Janus Big Picture to Steam later by running janus-steam-shortcut."
if [ "$SHORTCUT" = yes ]; then
    # It reports success or failure itself.
    "$APP_DIR/scripts/install_steam_shortcut.sh" -y || true
    shortcut_note=""
fi

# --- done ----------------------------------------------------------------------

msg="Janus Split Screen is installed.

Start it from your application menu: \"Janus Split Screen\" (desktop) or \"Janus Big Picture\" (controller).

Your own game handlers go in $HANDLER_DIR.
To uninstall, run $APP_DIR/uninstall.sh."
[ -n "$shortcut_note" ] && msg+="

$shortcut_note"
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) [ "$UI" = terminal ] && msg+="

Note: $BIN_DIR is not in your PATH, so 'janus' won't work in a terminal yet (the menu entries do). Log out and in again, or add it to your PATH." ;;
esac
if [ -n "$gamescope_msg" ]; then
    say warning "$msg

$gamescope_msg${required:+

These are needed as well:
$required}${optional:+

Optional, some features need them:
$optional}"
elif [ -n "$required" ]; then
    say warning "$msg

Janus won't start until these are installed:
$required${optional:+

Optional, some features need them:
$optional}"
else
    say info "$msg${optional:+

Optional, some features need them:
$optional}"
fi

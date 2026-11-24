#!/usr/bin/env bash
#
# check_gui_deps.sh — detect everything the Janus GUI needs to run.
#
# Checks four layers:
#   1. Python packages   (PySide6 for the GUI, python-xlib for the focus-release daemon)
#   2. System binaries   (gamescope, kscreen-doctor, qdbus6, dbus-send, xdotool, xprop, killall)
#   3. Steam + its Linux Runtimes (the containers games run in)
#   4. Build artifacts    (build/libjanus*.so) + the toolchain to build them
# gamescope and the runtimes are judged by src/host/prereqs.py, which the GUIs
# also use, so the install hints are the same everywhere.
#
# Each dependency is tagged REQUIRED / RECOMMENDED / OPTIONAL. The script never
# installs anything — it reports what is missing and how to install it, then
# exits non-zero if a REQUIRED dependency is absent.
#
# Usage:
#   scripts/check_gui_deps.sh          # full report
#   scripts/check_gui_deps.sh --quiet  # only print problems
#   ... --startup                      # skip what the GUIs check themselves
#                                      # (gamescope, Steam): run_gui.sh's preflight
#
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BUILD_DIR="$REPO_ROOT/build"

QUIET=0
STARTUP=0
for a in "$@"; do
    case "$a" in
        --quiet) QUIET=1 ;;
        --startup) STARTUP=1 ;;
    esac
done
PREREQS="$REPO_ROOT/src/host/prereqs.py"

# Colours (disabled when not a tty).
if [ -t 1 ]; then
    G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; B=$'\e[1m'; N=$'\e[0m'
else
    G=''; Y=''; R=''; B=''; N=''
fi

# An installed copy (scripts/package.py) ships prebuilt hooks and has no
# toolchain to check; its users may not be on Debian either.
INSTALLED=0
[ -f "$REPO_ROOT/VERSION" ] && INSTALLED=1
HAVE_APT=0
command -v apt-get >/dev/null 2>&1 && HAVE_APT=1

MISSING_REQUIRED=0
MISSING_OPTIONAL=0

# report TIER NAME STATUS DETAIL
#   STATUS: ok | missing
report() {
    local tier="$1" name="$2" status="$3" detail="$4"
    if [ "$status" = ok ]; then
        [ "$QUIET" -eq 1 ] && return
        printf "  ${G}✓${N} %-14s %-12s %s\n" "$name" "[$tier]" "$detail"
    else
        if [ "$tier" = REQUIRED ]; then
            MISSING_REQUIRED=$((MISSING_REQUIRED + 1))
            printf "  ${R}✗${N} %-14s ${R}%-12s${N} %s\n" "$name" "[$tier]" "$detail"
        else
            MISSING_OPTIONAL=$((MISSING_OPTIONAL + 1))
            printf "  ${Y}○${N} %-14s ${Y}%-12s${N} %s\n" "$name" "[$tier]" "$detail"
        fi
    fi
}

# check_py TIER MODULE PIP_PKG APT_PKG PURPOSE
#   APT_PKG may be empty (no Debian package) or hold one-or-more apt packages.
check_py() {
    local tier="$1" mod="$2" pip_pkg="$3" apt_pkg="$4" purpose="$5" ver fix
    if ver=$(python3 -c "import $mod, sys; print(getattr($mod,'__version__',''))" 2>/dev/null); then
        report "$tier" "$mod" ok "${ver:+v$ver }— $purpose"
    else
        fix="pip install $pip_pkg"
        [ -n "$apt_pkg" ] && [ "$HAVE_APT" -eq 1 ] && fix="$fix   |   sudo apt install $apt_pkg"
        report "$tier" "$mod" missing "$purpose  →  $fix"
    fi
}

# check_bin TIER BIN PKG PURPOSE
#   PKG is an apt package name, OR a literal fix hint when prefixed with "!"
#   (for tools not packaged in Debian, e.g. gamescope).
check_bin() {
    local tier="$1" bin="$2" pkg="$3" purpose="$4" path fix
    if path=$(command -v "$bin" 2>/dev/null); then
        report "$tier" "$bin" ok "$path"
    else
        if [ "$HAVE_APT" -eq 0 ]; then
            fix="install $bin with your distribution's package manager"
        elif [ "${pkg:0:1}" = "!" ]; then
            fix="${pkg:1}"
        else
            fix="sudo apt install $pkg"
        fi
        report "$tier" "$bin" missing "$purpose  →  $fix"
    fi
}

echo "${B}Janus GUI dependency check${N}  (repo: $REPO_ROOT)"

# --- 1. Python packages -----------------------------------------------------
[ "$QUIET" -eq 0 ] && echo && echo "${B}Python packages${N}"
# Debian splits PySide6 per Qt module; the GUI uses QtCore/QtGui/QtWidgets.
check_py REQUIRED    PySide6 PySide6 \
    "python3-pyside6.qtcore python3-pyside6.qtgui python3-pyside6.qtwidgets" \
    "GUI framework (src/gui)"
check_py RECOMMENDED Xlib    python-xlib python3-xlib \
    "Alt+1/Alt+2 hotkeys (X RECORD on XWayland; evdev needs the input group)"

# --- 2. System binaries -----------------------------------------------------
[ "$QUIET" -eq 0 ] && echo && echo "${B}System binaries${N}"
# gamescope: found in /usr/games too (Debian/Ubuntu), checked for --backend
# support; the install hint is per distribution (multi-line, printed below).
if [ "$STARTUP" -eq 0 ]; then
    if gs="$(python3 "$PREREQS" gamescope 2>&1)"; then
        report REQUIRED gamescope ok "$gs"
    else
        report REQUIRED gamescope missing "$(printf '%s\n' "$gs" | head -n1)"
        printf '%s\n' "$gs" | tail -n +2 | sed '/^$/d;s/^/                    /'
    fi
fi
check_bin REQUIRED    kscreen-doctor libkscreen-bin "monitor layout + scale flip (KWin Wayland)"
check_bin REQUIRED    qdbus6    qdbus-qt6        "KWin window rules + panel hide"
check_bin RECOMMENDED dbus-send dbus-bin         "Alt+1 focus release (KWin scripting)"
check_bin OPTIONAL    xdotool   xdotool          "on-screen keyboard (X11 only for now)"
check_bin OPTIONAL    xprop     x11-utils        "on-screen keyboard (X11 only for now)"
check_bin OPTIONAL    killall   psmisc           "gamescope cleanup on stop"

# --- 3. Steam + Steam Linux Runtimes -----------------------------------------
if [ "$STARTUP" -eq 0 ]; then
    [ "$QUIET" -eq 0 ] && echo && echo "${B}Steam${N}"
    rt="$(python3 "$PREREQS" runtimes 2>&1)"
    case $? in
        0)
            report REQUIRED steam ok "found"
            if [ -z "$rt" ]; then
                report RECOMMENDED runtimes ok "Steam Linux Runtimes installed"
            else
                while IFS=$'\t' read -r name link; do
                    report RECOMMENDED runtime missing "$name  →  in Steam: Library › Tools, or $link"
                done <<< "$rt"
            fi ;;
        *)
            report REQUIRED steam missing "Steam wasn't found  →  install Steam (native, not Flatpak) and log in once" ;;
    esac
fi

# --- 4. Build toolchain + artifacts -----------------------------------------
[ "$QUIET" -eq 0 ] && echo && echo "${B}Hook libraries & toolchain${N}"
if [ "$INSTALLED" -eq 0 ]; then
    check_bin REQUIRED gcc  gcc  "compile the LD_PRELOAD hooks"
    check_bin REQUIRED make make "build the hook libraries"

    # 32-bit toolchain (needed for libjanus32.so, used by 32-bit games e.g. L4D2).
    if echo 'int main(void){return 0;}' | gcc -m32 -x c - -o /dev/null >/dev/null 2>&1; then
        report RECOMMENDED gcc-multilib ok "32-bit hook build available"
    else
        report RECOMMENDED gcc-multilib missing "needed for libjanus32.so  →  sudo apt install gcc-multilib"
    fi
fi

if [ -f "$BUILD_DIR/libjanus64.so" ]; then
    report REQUIRED libjanus64.so ok "$BUILD_DIR/libjanus64.so"
else
    report REQUIRED libjanus64.so missing "not built  →  scripts/compile_hooks.sh"
fi
if [ -f "$BUILD_DIR/libjanus32.so" ]; then
    report RECOMMENDED libjanus32.so ok "$BUILD_DIR/libjanus32.so"
else
    report RECOMMENDED libjanus32.so missing "not built  →  scripts/compile_hooks.sh 32"
fi

# --- summary ----------------------------------------------------------------
echo
if [ "$MISSING_REQUIRED" -eq 0 ] && [ "$MISSING_OPTIONAL" -eq 0 ]; then
    echo "${G}${B}All dependencies present.${N}"
    exit 0
fi
echo "${B}Summary:${N} ${R}${MISSING_REQUIRED} required${N} / ${Y}${MISSING_OPTIONAL} optional${N} missing."
if [ "$MISSING_REQUIRED" -gt 0 ]; then
    echo "${R}The GUI will not work until the required items above are installed.${N}"
    exit 1
fi
echo "${Y}The GUI will run; missing optional items disable specific features.${N}"
exit 0

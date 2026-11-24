#!/usr/bin/env bash
#
# compile_hooks.sh — build the Janus LD_PRELOAD hook libraries (and the
# ticket proxy DLL).
#
# Wraps `make` in src/hook, verifies prerequisites (gcc, and gcc-multilib for
# the 32-bit build), and prints clear status. Hooks go to build/.
#
# `all` also rebuilds the ticket proxy DLL (src/ticketproxy ->
# vendor/ticketproxy/steam_api64_ticket.dll, used by Game.goldbergRealTicket)
# when its sources changed AND the Windows cross-compiler is installed. That
# DLL is committed, so without the compiler the build just keeps the
# committed copy.
#
# Usage:
#   scripts/compile_hooks.sh             # 64-bit + 32-bit hooks (+ ticket proxy, see above)
#   scripts/compile_hooks.sh 64          # only the 64-bit hook
#   scripts/compile_hooks.sh 32          # only the 32-bit hook
#   scripts/compile_hooks.sh tracer      # also build the SDL tracer libs
#   scripts/compile_hooks.sh ticketproxy # only the ticket proxy (needs gcc-mingw-w64-x86-64)
#   scripts/compile_hooks.sh clean       # remove built libraries
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
HOOK_DIR="$REPO_ROOT/src/hook"
BUILD_DIR="$REPO_ROOT/build"

target="${1:-all}"

# --- prerequisite checks ----------------------------------------------------

if ! command -v gcc >/dev/null 2>&1; then
    echo "error: gcc not found. Install it with: sudo apt install gcc" >&2
    exit 1
fi

if ! command -v make >/dev/null 2>&1; then
    echo "error: make not found. Install it with: sudo apt install make" >&2
    exit 1
fi

needs_32bit() {
    case "$target" in
        all|32|tracer) return 0 ;;
        *) return 1 ;;
    esac
}

if needs_32bit; then
    # The 32-bit build needs gcc-multilib (provides /usr/include 32-bit headers).
    if ! echo 'int main(void){return 0;}' | gcc -m32 -x c - -o /dev/null >/dev/null 2>&1; then
        echo "error: 32-bit compilation failed (gcc -m32)." >&2
        echo "       Install multilib support: sudo apt install gcc-multilib" >&2
        echo "       Or build only the 64-bit hook: scripts/compile_hooks.sh 64" >&2
        exit 1
    fi
fi

# --- map friendly target name to a make goal --------------------------------

case "$target" in
    all)    goal="all" ;;
    64)     goal="64bit" ;;
    32)     goal="32bit" ;;
    tracer) goal="all tracer" ;;
    clean)  goal="clean" ;;
    ticketproxy) goal="" ;;
    *)
        echo "error: unknown target '$target'" >&2
        echo "usage: $0 [all|64|32|tracer|ticketproxy|clean]" >&2
        exit 1
        ;;
esac

if [ -n "$goal" ]; then
    echo "Building Janus hooks ($target) ..."
    make -C "$HOOK_DIR" $goal
fi

# --- ticket proxy DLL (committed; rebuilt only when sources changed) --------

TICKET_DIR="$REPO_ROOT/src/ticketproxy"
TICKET_DLL="$REPO_ROOT/vendor/ticketproxy/steam_api64_ticket.dll"
case "$target" in
    all|ticketproxy)
        if command -v x86_64-w64-mingw32-gcc >/dev/null 2>&1; then
            echo
            echo "Building ticket proxy DLL ..."
            make -C "$TICKET_DIR"
            echo "If vendor/ticketproxy/steam_api64_ticket.dll changed (git status), commit it."
        elif [ "$target" = "ticketproxy" ]; then
            echo "error: x86_64-w64-mingw32-gcc not found." >&2
            echo "       Install it with: sudo apt install gcc-mingw-w64-x86-64" >&2
            exit 1
        elif ! make -q -C "$TICKET_DIR" >/dev/null 2>&1; then
            echo
            echo "note: src/ticketproxy may be newer than the committed DLL, but the"
            echo "      Windows cross-compiler isn't installed, so it was not rebuilt."
            echo "      Only matters if you changed src/ticketproxy or vendor/goldberg:"
            echo "      sudo apt install gcc-mingw-w64-x86-64 && $0 ticketproxy"
        fi
        ;;
    clean)
        make -C "$TICKET_DIR" clean
        ;;
esac

if [ "$target" != "clean" ]; then
    echo
    echo "Done. Artifacts:"
    ls -lh "$BUILD_DIR"/libjanus*.so "$TICKET_DLL" 2>/dev/null || true
    [ "$target" = "tracer" ] && ls -lh "$BUILD_DIR"/libsdl_tracer*.so 2>/dev/null || true
fi

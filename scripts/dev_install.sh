#!/usr/bin/env bash
#
# dev_install.sh — test the zip the way a user gets it: build it, unpack it
# into dist/test/ and run its install.sh (per-user, ~/.local). Replaces any
# earlier per-user install; your settings, profiles and handlers are kept.
#
# Usage:
#   scripts/dev_install.sh              # build + install, no questions
#   scripts/dev_install.sh --no-build   # reuse the newest dist/*.zip
#   scripts/dev_install.sh -- --steam-shortcut   # pass options to install.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

BUILD=1
INSTALL_ARGS=(--yes)
while [ $# -gt 0 ]; do
    case "$1" in
        --no-build) BUILD=0 ;;
        --) shift; INSTALL_ARGS=("$@"); break ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

[ "$BUILD" -eq 1 ] && "$SCRIPT_DIR/package.py" --zip
zip="$(ls -t "$REPO_ROOT"/dist/janus-split-screen-*.zip 2>/dev/null | head -1)"
[ -n "$zip" ] || { echo "No zip in dist/ (run scripts/package.py --zip)" >&2; exit 1; }

test_dir="$REPO_ROOT/dist/test"
rm -rf "$test_dir"
mkdir -p "$test_dir"
echo "Unpacking $(basename "$zip") ..."
python3 -I -m zipfile -e "$zip" "$test_dir"   # like a user's unzip tool: may drop +x
pkg="$(ls -d "$test_dir"/janus-split-screen-*/)"
bash "$pkg/install.sh" "${INSTALL_ARGS[@]}"

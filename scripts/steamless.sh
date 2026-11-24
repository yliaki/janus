#!/usr/bin/env bash
# Unpack a SteamStub-wrapped game exe with the vendored Steamless CLI, run
# under Proton's Wine (wine-mono provides the .NET Framework runtime).
# Output: "<exe>.unpacked.exe" next to the input. See vendor/steamless/README.md.
set -euo pipefail
[ $# -ge 1 ] || { echo "usage: $0 <path/to/Game.exe> [extra Steamless.CLI args]" >&2; exit 2; }
exe=$1; shift
[ -f "$exe" ] || { echo "not a file: $exe" >&2; exit 1; }
repo=$(cd "$(dirname "$0")/.." && pwd)
cli="$repo/vendor/steamless/Steamless.CLI.exe"
[ -f "$cli" ] || { echo "missing $cli (see vendor/steamless/README.md)" >&2; exit 1; }

steam_common=${STEAM_COMMON:-$HOME/.local/share/Steam/steamapps/common}
proton=${PROTON_DIR:-}
if [ -z "$proton" ]; then
  for d in "$steam_common/Proton - Experimental" "$steam_common"/Proton\ [0-9]*; do
    [ -x "$d/files/bin/wine" ] && { proton=$d; break; }
  done
fi
[ -n "$proton" ] && [ -x "$proton/files/bin/wine" ] || { echo "no Proton with files/bin/wine found (set PROTON_DIR)" >&2; exit 1; }

export WINEPREFIX=${WINEPREFIX:-$HOME/.local/share/janus/steamless_prefix}
export PATH="$proton/files/bin:$PATH"
export LD_LIBRARY_PATH="$proton/files/lib64:$proton/files/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export WINEDLLPATH="$proton/files/lib64/wine:$proton/files/lib/wine"
export WINEDEBUG=${WINEDEBUG:--all}
export WINEDLLOVERRIDES="winemenubuilder.exe=d;${WINEDLLOVERRIDES:-}"
unset DISPLAY WAYLAND_DISPLAY   # headless: no dialogs, no wine desktop
mkdir -p "$WINEPREFIX"

echo "[steamless] proton: $proton"
echo "[steamless] prefix: $WINEPREFIX (first run installs wine-mono, ~1 min)"
abs=$(readlink -f "$exe")
(cd "$(dirname "$cli")" && "$proton/files/bin/wine" "$cli" --quiet "$@" "$abs")
"$proton/files/bin/wineserver" -w || true
out="$abs.unpacked.exe"
if [ -f "$out" ]; then
  echo "[steamless] OK: $out"
  python3 "$repo/scripts/pe_sections.py" "$out" || true
else
  echo "[steamless] no output produced (not SteamStub-wrapped, or unpack failed)" >&2
  exit 1
fi

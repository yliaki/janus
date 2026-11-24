#!/bin/sh
# Rebuild ../janus-controlify-fix.jar (committed) from src/ + res/.
# Compiles against the Minecraft, Mixin and Controlify jars of Janus's own
# Prism install (main.py setup minecraft), with Prism's bundled JDK.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
prism=${PRISM_DIR:-$HOME/.local/share/janus/games/minecraft/prism}
mc=$(python3 -c "import json;print(json.load(open('$prism/../install.json'))['minecraft'])")
jdk=$(dirname "$(ls -d "$prism"/java/*/bin/javac | head -1)")
cp="$(find "$prism/libraries" -name "minecraft-$mc-client.jar" | head -1)"
cp="$cp:$(find "$prism/libraries" -name 'sponge-mixin-*.jar' | head -1)"
cp="$cp:$(ls "$prism"/instances/janus/minecraft/mods/controlify-*.jar | head -1)"
out=$(mktemp -d)
trap 'rm -rf "$out"' EXIT
"$jdk/javac" --release 21 -nowarn -proc:none -cp "$cp" -d "$out" "$here"/src/dev/janus/controlifyfix/*.java
cp "$here"/res/* "$out"/
rm -f "$here/../janus-controlify-fix.jar"
(cd "$out" && "$jdk/jar" --create --file "$here/../janus-controlify-fix.jar" .)
echo "built $here/../janus-controlify-fix.jar (Minecraft $mc)"

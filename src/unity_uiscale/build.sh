#!/bin/sh
# Build JanusUiScale.dll (BepInEx 5 plugin) -> vendor/unity_uiscale/ (COMMITTED,
# so users never compile; rebuild + commit after changing JanusUiScale.cs).
# Needs mono-mcs (apt). Compiles against a Unity Mono game's own Managed/ dir
# (Unity's assemblies are never committed); the result only references them
# by name. Usage: src/unity_uiscale/build.sh "<game>_Data/Managed"
set -e
M=${1:?usage: $0 <game>_Data/Managed}
cd "$(dirname "$0")/../.."
B=vendor/bepinex/win_x64/BepInEx/core
mkdir -p vendor/unity_uiscale
mcs -target:library -nostdlib -noconfig -optimize \
    -out:vendor/unity_uiscale/JanusUiScale.dll \
    -r:"$M/mscorlib.dll" -r:"$M/netstandard.dll" -r:"$M/System.dll" \
    -r:"$M/System.Core.dll" -r:"$M/UnityEngine.dll" \
    -r:"$M/UnityEngine.CoreModule.dll" -r:"$M/UnityEngine.UI.dll" \
    -r:"$M/UnityEngine.UIModule.dll" -r:"$M/UnityEngine.TextRenderingModule.dll" \
    -r:$B/BepInEx.dll -r:$B/0Harmony.dll \
    src/unity_uiscale/JanusUiScale.cs
echo "built vendor/unity_uiscale/JanusUiScale.dll"

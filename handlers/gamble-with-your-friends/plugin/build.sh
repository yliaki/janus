#!/bin/sh
# Build JanusPadFocus.dll (BepInEx 5 plugin) -> ../payload/ (COMMITTED, so
# users never compile; rebuild + commit after changing JanusPadFocus.cs).
# Needs mono-mcs (apt). Compiles against the game's own Managed/ dir.
# Usage: plugin/build.sh "<install>/Gamble With Your Friends_Data/Managed"
set -e
M=${1:?usage: $0 <game>_Data/Managed}
cd "$(dirname "$0")"
B=../../../vendor/bepinex/win_x64/BepInEx/core
mcs -target:library -nostdlib -noconfig -optimize \
    -out:../payload/JanusPadFocus.dll \
    -r:"$M/mscorlib.dll" -r:"$M/netstandard.dll" -r:"$M/System.dll" \
    -r:"$M/System.Core.dll" -r:"$M/UnityEngine.dll" \
    -r:"$M/UnityEngine.CoreModule.dll" -r:"$M/Unity.InputSystem.dll" \
    -r:"$M/UnityEngine.UI.dll" -r:"$M/UnityEngine.UIModule.dll" \
    -r:$B/BepInEx.dll \
    JanusPadFocus.cs JanusInviteButton.cs
echo "built payload/JanusPadFocus.dll"

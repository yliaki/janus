# BepInEx 5 — vendored Unity mod loader

Unmodified [BepInEx](https://github.com/BepInEx/BepInEx) **5.4.23.5**,
`BepInEx_win_x64_5.4.23.5.zip` from the GitHub release
(sha256 `82f9878551030f54657792c0740d9d51a09500eeae1fba21106b0c441e6732c4`),
extracted to `win_x64/`. Deployed by `src/host/bepinex.py` (handler fields
`Game.unityUiScale` / `Game.bepinexPlugins`) to load Janus's own plugin
(`vendor/unity_uiscale/JanusUiScale.dll`, source `src/unity_uiscale/`) and
handler-shipped plugins into **Mono**-backend Unity games under Proton.
IL2CPP games would need BepInEx 6.

Loading under Proton: copy the contents of `win_x64/` next to the game exe
(Janus does it in the Goldberg mirror, never the real install) and set
`WINEDLLOVERRIDES=winhttp=n,b` so Wine loads Doorstop's `winhttp.dll`.
BepInEx writes `BepInEx/config`, `cache`, `LogOutput.log` there at runtime,
so it must be a copy, not a symlink into this directory.

## Licences (`licenses/`)

| Component | Files | Licence | Source |
|---|---|---|---|
| BepInEx 5 | `BepInEx/core/BepInEx*.dll` | MIT (v5 branch; v6 is LGPL-2.1) | https://github.com/BepInEx/BepInEx/tree/v5.4.23.5 |
| UnityDoorstop 4.5.0 | `winhttp.dll`, `doorstop_config.ini` | LGPL-2.1 | https://github.com/NeighTools/UnityDoorstop/tree/v4.5.0 |
| HarmonyX | `0Harmony*.dll`, `BepInEx.Harmony.dll`, `HarmonyXInterop.dll` | MIT | https://github.com/BepInEx/HarmonyX |
| MonoMod | `MonoMod.*.dll` | MIT | https://github.com/MonoMod/MonoMod |
| Mono.Cecil | `Mono.Cecil*.dll` | MIT | https://github.com/jbevain/cecil |

All binaries are unmodified upstream builds; replace them with your own builds
of the same projects if you like.

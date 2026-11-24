# UE4SS — vendored Unreal Engine mod loader

Unmodified [RE-UE4SS](https://github.com/UE4SS-RE/RE-UE4SS) experimental build
**v3.0.1-1181-g83330635** (`UE4SS_v3.0.1-1181-g83330635.zip` from the
`experimental-latest` release, uploaded 2026-10-17, sha256
`b9a166a756605b21058a64cde93bcaab1a855dba3553e5c7e6ce0fa8fdfad7b9`), extracted to
`win64/` without its sample `Mods/`. The experimental build is needed for UE 5.8
(Far Far West); the last stable release, v3.0.1 (2024-02), predates it.

Deployed by `src/host/unreal.py` (`Game.unrealUiFit`) next to the game's
`*-Shipping.exe` in the per-instance mirror, never the real install, with
`WINEDLLOVERRIDES=dwmapi=n,b` so Wine loads the proxy `dwmapi.dll`. It runs
Janus's own Lua mod (`src/unreal_uifit/main.lua`), which fits Unreal's UI scale
to each screen. Games whose engine build UE4SS can't scan by itself need the
upstream per-game config (`zCustomGameConfigs.zip` from the same release:
`UE4SS-settings.ini` + `UE4SS_Signatures/`), shipped in the handler's
`payload/ue4ss/` and copied over the defaults.

Licence: MIT (`win64/ue4ss/LICENSE`), the per-game configs too (same repository).

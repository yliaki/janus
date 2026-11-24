// Far Far West — Unreal Engine 5, Proton. Online subsystem Steam + EOS;
// Goldberg replaces Engine/Binaries/ThirdParty/Steamworks/Steamv157/Win64/
// steam_api64.dll in the mirror (the Nucleus handler by birden does the same).
// Under Goldberg the game can't reach its backend ("Could not connect to Evil
// Raptor network", "not connected to Epic Online Services"), so the online
// session types are greyed out and co-op runs over the game's own LAN mode.
// Validated 2026-10-25 with fake pads, 2 tiles: host CREATE SESSION ->
// OFFLINE / LAN (lands in the saloon hub); joiner BROWSE SESSIONS -> tick
// LAN under NETWORK -> REFRESH LIST -> the host's row -> "can't make sure this
// session is safe" -> Yes. Both HUDs then list both players.
// Menus use a free pad cursor (left stick; the D-pad does nothing there).
// Saves/config: AppData/Local/FarFarWest/Saved in each player's prefix.
// UI scale: unrealUiFit, i.e. "UI scale" is a maximum, fitted per screen by
// Janus's UE4SS mod (src/unreal_uifit/). UE 5.8 on a custom engine branch
// ("CanyonEngine"): UE4SS needs the upstream per-game config in
// payload/ue4ss/ (GNatives + ProcessLocalScriptFunction signatures, version
// 5.8, heavy hooks off). Validated 2026-10-25 with maximum 2.0: 1920x540
// notice 1.15, hub HUD 1.5-2.0, pause menu 1.6, in-game options 1.7;
// 960x1080 notice 1.3, main menu 1.4, create session 2.0. NOTES.md
// "Far Far West: per-screen UI fit".
Game.appid = 3124540;
Game.name = "Far Far West";
Game.maxPlayers = 4;
Game.launchMessage =
  "Menus: move the cursor with the left stick, A clicks.\n\n" +
  "To play together: one player chooses Create Session → Offline / LAN. " +
  "The others choose Browse Sessions, tick LAN under Network, press " +
  "Refresh List, pick the host's session and answer Yes.";
// The real game, not the FarFarWest.exe bootstrapper: in a fresh per-player
// prefix its prerequisite check stops at "Microsoft Visual C++ 2015-2022
// Redistributable (x64) is required" (Proton ships the runtime anyway).
Game.exeName = "FarFarWest-Win64-Shipping.exe";
Game.gameArgs = "-dx11";          // Steam's own "DX11 (Linux or old computers)" option
Game.isolation = "full";
Game.borderless = false;
Game.goldberg = true;
Game.goldbergDisableLan = false;  // keep the instances visible on the LAN
Game.unrealUiScale = true;        // "UI scale" options (unreal.py)
Game.unrealUiFit = true;          // ... fitted per screen via UE4SS

Game.play = function (ctx) {
  if (ctx.width && ctx.height) {
    ctx.gameArgs.push("-windowed", "-ResX=" + ctx.width, "-ResY=" + ctx.height);
  }
};

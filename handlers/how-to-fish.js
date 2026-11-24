// How to Fish — Unity, Proton. Same-host co-op via Goldberg (netcode is
// FishNet over Heathen Steamworks lobbies). Join via IN-GAME INVITES: all
// instances launch at once; host (friends-only or public) in any instance and
// use the game's invite button — the others join while running
// (Game.goldbergInvites, validated 2026-10-07). Pasting the lobby ID (shown in
// the GUI lobby panel; goldbergDebug) still works as a fallback. No auto-join:
// neither gbe_fork nor this game parses "+connect_lobby <id>" (NOTES.md).
//
// *** The shipped exe is SteamStub-DRM-wrapped (PE section ".bind"). ***
// The stub runs before Unity, talks to the Steam client directly (never via
// steam_api64.dll, so Goldberg can't intercept it), notices the process was
// not started by Steam and RELAUNCHES THE GAME THROUGH STEAM — in the real
// prefix, from the real install dir, on the real account. Symptom: Wine
// touches the isolated prefix but Unity never initialises there; Player.log
// appears in steamapps/compatdata/4001890 instead. Fix: Game.steamStubExes
// makes Janus unpack it with Steamless (cached) and use that in the mirror. See NOTES.md "SteamStub DRM".
//
// Install is nested (How to Fish/How to Fish/How to Fish.exe) and
// steam_api64.dll lives under How to Fish_Data/Plugins/x86_64/ — both are
// handled by the recursive exe search and the Goldberg mirror walk. The
// D3D12/ dir means Unity may pick D3D12; if it stutters under gamescope on
// NVIDIA, add "-force-d3d11" to gameArgs (cf. RV There Yet?).
Game.appid = 4001890;
Game.name = "How to Fish";
Game.launchMessage =
  "To play together: one player hosts a game (friends-only or public) and " +
  "uses the invite button; the others join automatically.";
Game.isolation = "full";
Game.borderless = false;
Game.goldberg = true;
Game.goldbergDisableLan = false;
Game.goldbergDebug = true;        // debug build -> lobby ID in the GUI panel
Game.goldbergAutoJoin = false;    // +connect_lobby is ignored; see header
Game.goldbergInvites = true;      // invite button pulls in running instances
Game.oskEnabled = true;

// SteamStub-wrapped exe -> Janus unpacks it and swaps it in, in the mirror.
Game.steamStubExes = ["How to Fish/How to Fish.exe"];

// Unity (Mono) UI scaling via BepInEx: adds the "UI scale" option. All
// canvases are ScaleWithScreenSize 1920x1080 matched to HEIGHT, so
// side-by-side tiles keep the full-size UI and top/bottom halves it.
Game.unityUiScale = true;
Game.resizeGame = "unity";        // re-flow: the plugin resizes it to its new tile

Game.play = function (ctx) {
  if (ctx.width && ctx.height) {
    ctx.gameArgs.push("-screen-fullscreen", "0",
                      "-screen-width", String(ctx.width),
                      "-screen-height", String(ctx.height));
  }
};

// Core Keeper — Unity 6000.0 (Mono), NATIVE Linux build. Co-op via Goldberg.
// Validated 2026-10-27 with two fake pads, 1920x540 tiles: host starts a
// world, the other types the host's Game ID under Join Game, both in one world.
//
// - Crossplay: with it on (the default) the game logs in to PlayFab with the
//   Steam ticket, which Goldberg's fake ticket fails ("Connection to the
//   multiplayer backend was lost", Player.log "PlayFab Login Error ...
//   User is offline"). play() turns it off in prefs.json: the game then uses
//   plain Steam lobbies + networking (Pug.Platform.SteamNetworking), which
//   Goldberg covers.
// - No invites: on Steam the game has no invite button (SteamNetworking
//   .CanSendInvites = false, its invitation flow is empty); real Steam users
//   join through the friends list. The way in is the Game ID shown in the
//   host's pause menu (derived from the lobby ID, new every session). It has
//   to be typed: under Goldberg there is no Steam gamepad text input, so text
//   fields (Game ID, world and character names) need a keyboard. Goldberg's LAN
//   discovery stays on so the joiner finds the host's lobby.
// - The game draws a fixed 16:9 pixel-art view (480x270 times a scale) with
//   its UI, letterboxed in other tile shapes: 1920x540 and 960x1080 both give
//   960x540. No UI scale needed.
// - Saves/settings: ~/.config/unity3d/Pugstorm/Core Keeper/Steam/<account id>/
//   (account id = Steam ID - 76561197960265728). `secondary`: the owner plays
//   on the real ones (prefs.json backed up and restored), the others in their
//   profile's home.
Game.appid = 1621690;
Game.name = "Core Keeper";
Game.maxPlayers = 8;
Game.launchMessage =
  "To play together: one player starts a world and opens the pause menu " +
  "(Start); the Game ID is shown at the top left (press the eye icon to " +
  "reveal it). The others choose Join Game and type that ID with a keyboard.\n\n" +
  "World and character names also need a keyboard.";
Game.isolation = "secondary";
Game.borderless = false;
Game.goldberg = true;
Game.goldbergDisableLan = false;  // the joiner finds the host's lobby over Goldberg's LAN

function prefsPath(ctx) {
  const account = BigInt(ctx.profile.steamId) - 76561197960265728n;
  return `${ctx.home}/.config/unity3d/Pugstorm/Core Keeper/Steam/${account}/prefs.json`;
}

Game.play = function (ctx) {
  if (ctx.width && ctx.height)
    ctx.gameArgs.push("-screen-fullscreen", "0",
                      "-screen-width", String(ctx.width),
                      "-screen-height", String(ctx.height));

  // A missing prefs.json gets only these keys; the game fills in the rest.
  const p = prefsPath(ctx);
  if (ctx.profile.owner) Janus.backup(p);
  const prefs = Janus.exists(p) ? Janus.readJson(p) : {};
  prefs.crossPlay = false;
  prefs.fullscreen = false;
  // Goldberg reports the Explorer's Edition DLC; its popup shows every start.
  prefs.showExplorersEditionPopup = false;
  // One soundtrack: music off for players 2+, the player's own volume kept
  // and given back when they play as player 1 (the owner's file is restored
  // anyway).
  const saved = `${ctx.saveRoot}/janus-music-volume.txt`;
  if (ctx.instance > 1) {
    if (prefs.bgmVol !== 0 && !ctx.profile.owner)
      Janus.writeFile(saved, String(prefs.bgmVol ?? 0.5));
    prefs.bgmVol = 0;
  } else if (Janus.exists(saved)) {
    prefs.bgmVol = Number(Janus.readFile(saved));
    Janus.remove(saved);
  }
  Janus.writeJson(p, prefs);
};

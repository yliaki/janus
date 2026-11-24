// Don't Starve Together — Klei engine, NATIVE Linux (bin64/dontstarve_steam_x64).
// Co-op over the game's own LAN mode, through Goldberg. Validated 2026-10-27
// with two fake pads, 1920x540 tiles: host Host Game → Survival → No Caves →
// Together → Generate World (Menu button) → OK; joiner Browse Games → No,
// Browse Games → the host's world in the LAN list → Join (Menu) → OK; both
// pick a survivor and play in one world, each pad moving only its player.
//
// - Online play logs in to Klei with the Steam ticket. Goldberg's fails
//   ("Account Failed (3): E_STEAM_USER_OFFLINE") and "Play Offline" then hung
//   on "Logging in". Goldberg's offline=1 (Steam reports offline mode) makes
//   the game skip the login: it starts in offline mode, where worlds are
//   "Local Only" / Server Mode Offline and joinable over the LAN.
// - Working dir must be bin64/ (Steam's launch config; from the install root:
//   "failed to change working directory to ../data").
// - The view is at most 16:9 (pillarboxed in a 1920x540 tile).
// - "Controller Detected" asks at every start in a fresh profile (and for
//   the owner, whose client.ini is restored); client.ini controller_popup
//   doesn't stop it. A = Enable Controller.
// - Settings and saves: ~/.klei/DoNotStarveTogether/<account id>/ (client.ini,
//   Cluster_N worlds), account id = Steam ID - 76561197960265728.
//   `secondary`: the owner keeps the real dir (client.ini backed up), the
//   others get their profile's home.
Game.appid = 322330;
// Re-flow (dynamic players): the game follows a resized window.
Game.resizeGame = "window";
Game.name = "Don't Starve Together";
Game.maxPlayers = 6;
Game.launchMessage =
  "At the start press A (Enable Controller), then Play.\n\n" +
  "To play together: one player chooses Host Game and creates a world " +
  "(Generate World is the Menu button). The others choose Browse Games, " +
  "pick that world from the list and press Menu (Join).";
Game.launchCommand = "/bin/sh -c 'cd bin64 && exec ./dontstarve_steam_x64 \"$@\"' dst";
Game.isolation = "secondary";
Game.borderless = false;
Game.goldberg = true;
Game.goldbergDisableLan = false;
Game.goldbergDebug = true;        // gbe reads steam_settings under GseAppPath
Game.killOnClose = ["dontstarve_dedicated_server_nullrenderer"];

Game.onMirrorReady = function (ctx) {
  for (const d of ["bin64/lib64/steam_settings", "_janus_gbe_logs/steam_settings"]) {
    if (d.startsWith("_janus") && !Janus.exists("_janus_gbe_logs")) continue;
    Janus.iniSet(`${d}/configs.main.ini`, "main::connectivity", "offline", "1");
  }
};

Game.play = function (ctx) {
  const account = BigInt(ctx.profile.steamId) - 76561197960265728n;
  const ini = `${ctx.home}/.klei/DoNotStarveTogether/${account}/client.ini`;
  if (ctx.profile.owner) Janus.backup(ini);
  Janus.iniSet(ini, "MISC", "autopause", "false");  // a pause would stop the host's world
  Janus.iniSet(ini, "GRAPHICS", "fullscreen", "false");
  if (ctx.width && ctx.height) {
    Janus.iniSet(ini, "GRAPHICS", "windowed_width", String(ctx.width));
    Janus.iniSet(ini, "GRAPHICS", "windowed_height", String(ctx.height));
  }
};

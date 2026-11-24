// PAYDAY 2 — Diesel engine, Proton (the native Linux build was dropped).
//
// - DLL check: at start-up the exe loads core/settings/security_settings from
//   its assets, lists every *.dll in its folder and verifies them; Goldberg's
//   steam_api64.dll fails that ("Installation corrupted. Please verify your
//   game files or reinstall."), and so does a folder without Valve's DLL.
//   So the game keeps Valve's steam_api64.dll (mirrorCopyInstead) and the
//   emulator comes in one level lower: gbe_fork's ColdClientLoader (payload,
//   from the same gbe_fork release-2026_05_30 as vendor/goldberg) starts the
//   game with its emulated steamclient64.dll as the Steam client. Same
//   emulator, same per-profile identity (GSE Saves/settings in the prefix),
//   same LAN discovery and invites as the other Goldberg handlers.
// - Input: the device that answers "Press any key" becomes the instance's
//   input for the whole session. Press a pad button there; a click or key
//   locks the instance to keyboard/mouse and its pad is ignored.
// - Co-op: host an online lobby from Crime.net in one tile, "Invite" in the
//   lobby pulls in the others (validated 2026-10-16).
// - Single instance: a Local\ mutex ("Another instance of PAYDAY 2 is
//   already running") only reaches processes of one wineserver = one
//   prefix. Every isolation mode but "none" gives each player their own.
Game.appid = 218620;
Game.name = "PAYDAY 2";
Game.maxPlayers = 4;
Game.exeName = "PAYDAY2.exe";
Game.isolation = "secondary";
Game.borderless = false;
Game.steamInputFilter = false;
Game.goldberg = true;
Game.goldbergDisableLan = false;  // instances see each other as friends
Game.goldbergInvites = true;      // "Invite" in the lobby pulls in the others
Game.mirrorCopyInstead = ["steam_api64.dll"];  // keep Valve's (DLL check)
// -steammm: Steam matchmaking (lobbies through the emulator) instead of EOS.
Game.gameArgs = "-steammm";
Game.launchMessage =
  "On the \"Press any key\" screen, every player must press a button on " +
  "their CONTROLLER. Don't touch the keyboard or mouse: the first device " +
  "used there becomes that game's only input, and its controller is then " +
  "ignored until the game is restarted.\n\n" +
  "To play together: one player hosts an online lobby from Crime.net, then " +
  "chooses \"Invite\" in the lobby to bring in the others.";

Janus.addOption("unlockAllDlc", {
  label: "Unlock all DLC (Goldberg)",
  description: "Have the emulator report every DLC as owned. Off = report " +
               "none (gbe_fork's own default, with no config, is all unlocked).",
  type: "bool", default: false});

// Resolution, windowed mode etc. Written by the game on first start (it
// picks the gamescope size by itself); later starts would keep the size of
// the previous session, so play() rewrites it to this tile.
const RENDERER = "AppData/Local/PAYDAY 2/renderer_settings_dx11.xml";

// ColdClientLoader + steamclient64.dll + steam_settings/ for this instance:
// in the player's prefix, outside the game folder.
const GBE_REL = "users/steamuser/Janus/payday2-gbe";
const gbeDir = ctx => `${ctx.winePrefix}/drive_c/${GBE_REL}`;
const winPath = p => "Z:" + p.replace(/\//g, "\\");

Game.onMirrorReady = function (ctx) {
  if (!ctx.winePrefix) Janus.error("PAYDAY 2: no Wine prefix (expected a Proton launch).");
  const dst = gbeDir(ctx);
  if (Janus.exists(dst)) Janus.remove(dst, {force: true});
  Janus.mkdir(dst);
  Janus.deployPayload(dst);  // coldclient/
  // gbe_fork reads steam_settings/ beside steamclient64.dll.
  if (Janus.exists("steam_settings")) {
    Janus.copy("steam_settings", `${dst}/coldclient/steam_settings`);
    Janus.remove("steam_settings");
  }
  Janus.writeFile(`${dst}/coldclient/steam_settings/configs.app.ini`,
                  `[app::dlcs]\nunlock_all=${ctx.options.unlockAllDlc ? 1 : 0}\n`);
  Janus.writeFile(`${dst}/coldclient/ColdClientLoader.ini`, [
    "[SteamClient]",
    `Exe=${winPath(ctx.gameDir)}\\PAYDAY2.exe`,
    `ExeRunDir=${winPath(ctx.gameDir)}`,
    "ExeCommandLine=",
    `AppId=${ctx.appid}`,
    "SteamClientDll=steamclient64.dll",
    "SteamClient64Dll=steamclient64.dll",
    "[Injection]",
    "ForceInjectSteamClient=0",
    "ForceInjectGameOverlayRenderer=0",
    "DllsToInjectFolder=",
    "IgnoreInjectionError=1",
    "IgnoreLoaderArchDifference=0",
    "[Persistence]",
    "Mode=0",
    "[Debug]",
    "ResumeByDebugger=0",
    ""].join("\r\n"));
};

// The loader passes its own arguments on to the game.
Game.buildCommand = function (ctx) {
  return [`${gbeDir(ctx)}/coldclient/steamclient_loader_x64.exe`];
};

Game.play = function (ctx) {
  // With goldbergDebug on, Janus points GseAppPath at the mirror; the
  // emulator must keep reading the steam_settings/ written above.
  ctx.env.GseAppPath = `${gbeDir(ctx)}/coldclient/`;
  const xml = `${ctx.winePrefix}/drive_c/users/steamuser/${RENDERER}`;
  if (!ctx.width || !ctx.height) return;
  if (ctx.profile.owner) Janus.backup(xml);
  // Left to itself the first start picks gamescope's largest mode, which for
  // a portrait tile comes out rotated (2160x1920 in a 1920x2160 tile).
  if (!Janus.exists(xml)) Janus.writeFile(xml, [
    "<renderer_config>",
    "\t<d3d_device",
    '\t\tadapter_index = "0"',
    `\t\tresolution = "${ctx.width} ${ctx.height}"`,
    '\t\twindowed = "false"',
    '\t\tborderless = "false"',
    '\t\trefresh_rate = "60"',
    `\t\taspect_ratio = "1"`,
    '\t\tv_sync = "1"',
    "\t/>",
    "</renderer_config>", ""].join("\n"));
  const text = Janus.readFile(xml)
    .replace(/(\bresolution\s*=\s*")[^"]*"/, `$1${ctx.width} ${ctx.height}"`)
    .replace(/(\baspect_ratio\s*=\s*")[^"]*"/, `$1${(ctx.width / ctx.height).toFixed(5)}"`)
    .replace(/(\bwindowed\s*=\s*")[^"]*"/, '$1false"');
  Janus.writeFile(xml, text);
};

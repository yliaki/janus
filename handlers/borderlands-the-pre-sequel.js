// Borderlands: The Pre-Sequel — Unreal Engine 3, Aspyr's NATIVE Linux port
// (32-bit ELF), the same port as Borderlands 2 (handlers/borderlands-2.js
// explains the details; everything below mirrors it).
//
// - Input: SDL2 statically linked, reads /dev/input/event* through libc
//   open(), so the evdev layer restricts each instance to its pad.
// - Steam: libsteam_api.so is ELF NEEDED with RPATH "." (= cwd = the
//   Goldberg mirror). Old Steamworks SDK: steam_interfaces.txt (identical
//   interface list to Borderlands 2's lib) or the game exits after the menu map.
// - The game finds its data dir via readlink(/proc/<pid>/exe), so the binary
//   and steamassets/binaries are real copies in the mirror.
// - DLC: Goldberg reports the DLC with installed depots (= owned).
// - Saves/config: ~/.local/share/aspyr-media/borderlands the pre-sequel/.
// Validated 2026-10-27 with two fake pads, 1920x540 tiles: host New Game,
// joiner selects the host under "Welcome to Helios" → Join Game → host's
// lobby (2/4). Menus are 16:9 in the middle of a wide tile (as in BL2).
Game.appid = 261640;
Game.name = "Borderlands: The Pre-Sequel";
Game.maxPlayers = 4;
Game.launchMessage =
  "To play together: Set the Network Options to Online Friends Only (should be on by default). One player " +
  "starts the game, then select that player's name on the main menu to join.";
Game.exeName = "BorderlandsPreSequel";
Game.mirrorCopyInstead = ["BorderlandsPreSequel", "steamassets/binaries"];
Game.isolation = "secondary";
Game.goldberg = true;
Game.goldbergDisableLan = false;  // instances see each other as friends
Game.goldbergInvites = true;
Game.borderless = false;
Game.gameArgs = "-nostartupmovies";

const DATA = ".local/share/aspyr-media/borderlands the pre-sequel/willowgame";

Janus.addOption("unlockDlc", {
  label: "Unlock installed DLC (Goldberg)",
  description: "Have Goldberg report the DLC Steam has installed for this " +
               "game (i.e. what your account owns) as owned, for every " +
               "player. Off = report none.",
  type: "bool", default: true});
Janus.addOption("skipNewGameIntro", {
  label: "Skip the new-game intro cinematic",
  description: "Leave out the unskippable opening movie that plays when a " +
               "new character starts the story.",
  type: "bool", default: true});

if (Janus.getOption("skipNewGameIntro"))
  Game.mirrorExclude = ["steamassets/willowgame/movies/megaintro.bik"];

// Taken from the original libsteam_api.so (`strings`).
const STEAM_INTERFACES = [
  "SteamClient012", "SteamGameServer011", "SteamGameServerStats001",
  "SteamUser017", "SteamFriends014", "SteamUtils006", "SteamMatchMaking009",
  "SteamMatchMakingServers002", "STEAMUSERSTATS_INTERFACE_VERSION011",
  "STEAMAPPS_INTERFACE_VERSION006", "SteamNetworking005",
  "STEAMREMOTESTORAGE_INTERFACE_VERSION012",
  "STEAMSCREENSHOTS_INTERFACE_VERSION002", "STEAMHTTP_INTERFACE_VERSION002",
  "STEAMUNIFIEDMESSAGES_INTERFACE_VERSION001", "STEAMUGC_INTERFACE_VERSION001"];

function installedDlc(ctx) {
  const acf = `${ctx.installDir}/../../appmanifest_${ctx.appid}.acf`;
  if (!Janus.exists(acf)) { Janus.log(`no ${acf}; no DLC reported`); return []; }
  const ids = new Set();
  for (const m of Janus.readFile(acf).matchAll(/"dlcappid"\s+"(\d+)"/g)) ids.add(m[1]);
  return [...ids].sort();
}

Game.onMirrorReady = function (ctx) {
  const dirs = ["steam_settings"];
  if (Janus.exists("_janus_gbe_logs")) dirs.push("_janus_gbe_logs/steam_settings");
  const dlc = ["[app::dlcs]", "unlock_all=0"];
  if (ctx.options.unlockDlc)
    for (const id of installedDlc(ctx)) dlc.push(`${id}=DLC ${id}`);
  for (const d of dirs) {
    Janus.writeLines(`${d}/steam_interfaces.txt`, STEAM_INTERFACES);
    Janus.writeLines(`${d}/configs.app.ini`, dlc);
  }
  Janus.log(`Goldberg DLCs: ${dlc.length - 2 ? dlc.slice(2).join(", ") : "none"}`);
};

Game.play = function (ctx) {
  if (ctx.verbose) ctx.gameArgs.push("-forcelogflush");
  if (ctx.gameDir !== ctx.installDir && !Janus.exists("_janus_gbe_logs"))
    ctx.env.GseAppPath = ctx.gameDir + "/";

  if (ctx.width && ctx.height)
    ctx.gameArgs.push("-windowed", `-ResX=${ctx.width}`, `-ResY=${ctx.height}`);

  // The user ini only exists after the first start.
  const ini = Janus.glob(`${ctx.home}/${DATA}/[Cc]onfig/[Ww]illow[Ee]ngine.ini`)[0];
  if (!ini) return;
  if (ctx.profile.owner) Janus.backup(ini);
  for (const [s, k, v] of [
      ["SystemSettings", "Fullscreen", "False"],
      ["SystemSettings", "WindowedFullscreen", "False"],
      ["Engine.Engine", "bPauseOnLossOfFocus", "False"],
      ["Engine.Engine", "bMuteAudioWhenNotInFocus", "False"],
      ["WillowGame.WillowGameEngine", "bPauseLostFocusWindowed", "False"],
      ["WillowGame.WillowGameEngine", "bMuteAudioWhenNotInFocus", "False"]])
    Janus.iniSet(ini, s, k, v);
  if (ctx.width && ctx.height) {
    Janus.iniSet(ini, "SystemSettings", "ResX", String(ctx.width));
    Janus.iniSet(ini, "SystemSettings", "ResY", String(ctx.height));
  }
};

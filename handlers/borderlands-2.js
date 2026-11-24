// Borderlands 2 — Unreal Engine 3, Aspyr's NATIVE Linux port (32-bit ELF,
// what Steam installs on Linux).
//
// - Input: SDL2 is STATICALLY linked (no SDL symbols to hook), but it opens
//   /dev/input/event* through libc open(), so the evdev layer restricts each
//   instance to its pad.
// - Steam: libsteam_api.so is ELF NEEDED with RPATH "." (= cwd), and the
//   game runs with cwd = the Goldberg mirror, whose libsteam_api.so is the
//   32-bit Goldberg build (picked by ELF class).
// - Co-op: Network Options = Online Friends Only (the default), one instance
//   starts a game, the others select that player's name on the main menu.
//   Goldberg LAN discovery is left on so the instances appear to each other
//   as Steam friends.
// - Saves: <XDG_DATA_HOME>/aspyr-media/borderlands 2/willowgame/savedata/
//   <steamid>/. Goldberg reports each profile's Steam ID (the owner's real
//   one), so saves follow the profile; `secondary` isolation keeps the
//   owner on their real saves + config and gives everyone else a fresh home.
// - Window: -windowed -ResX/-ResY = the gamescope render size. UE3 writes
//   the resolution back into WillowEngine.ini on exit, so the owner's real
//   one is Janus.backup'd and restored.
// - Layout: the menus are laid out for 16:9 and get cut off at both sides
//   in tiles narrower than that (side-by-side 960x1080). Use top/bottom
//   (1920x540) for two players, or quad.
// - The binary itself appends "-ONETHREAD -FULLSCREEN" to its command line
//   (see launch.log); the tile size still applies.
// - DLC: Goldberg's configs.app.ini lists the DLC Steam installed for the
//   game (= owned on the account), so every player gets them. Listing all
//   DLC crashes after character select (see DLCS below).
// - New-game intro: the unskippable cinematic is the movie
//   willowgame/movies/megaintro.bik; leaving it out of the mirror skips it.
// - The game finds its data dir via readlink(/proc/<pid>/exe), then chdirs
//   to steamassets/binaries/win32 and loads everything relative to that.
//   Through a symlinked binary or binaries/ dir both resolve to the REAL
//   install and mirrorExclude does nothing, so both are real copies.
Game.appid = 49520;
Game.name = "Borderlands 2";
Game.maxPlayers = 4;
Game.launchMessage =
  "To play together: Set the Network Options to Online Friends Only (should be on by default). One player " +
  "starts the game, then select that player's name on the main menu to join.";
Game.exeName = "Borderlands2";
// Real copies, so the game's data dir resolves to the mirror (see above).
Game.mirrorCopyInstead = ["Borderlands2", "steamassets/binaries"];
Game.isolation = "secondary";
Game.goldberg = true;
Game.goldbergDisableLan = false;  // instances see each other as friends
Game.goldbergInvites = true;      // a friends-list invite pulls in a running instance
Game.borderless = false;
// No Aspyr / 2K / Gearbox logos at start-up.
Game.gameArgs = "-nostartupmovies";

const DATA = ".local/share/aspyr-media/borderlands 2/willowgame";

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

// DLC names (appinfo.vdf listofdlc, 2026-10-16), only for the config file.
// Reporting ALL of them as owned crashes the game right after character
// select (SIGSEGV in UnrealScript) — it applies DLC content that isn't on
// disk. So only DLC with an installed depot in the app manifest is listed
// (Steam downloads depots only for owned DLC); unlock_all stays 0.
const DLCS = {
  207870: "Captain Scarlett and her Pirate's Booty",
  208690: "Creature Slaughterdome", 208691: "Creature Dome",
  213210: "Mr. Torgue's Campaign of Carnage", 213230: "Mechromancer Pack",
  213231: "Premiere Club", 213250: "Sir Hammerlock's Big Game Hunt",
  213780: "Collector's Edition Pack", 213781: "Collectors Edition",
  213782: "Collectors Edition OSX", 218550: "Tiny Tina's Assault on Dragon Keep",
  218560: "DLC 218560", 221710: "DLC 221710",  // not in the appinfo cache
  224140: "Mechromancer Beatmaster Pack", 224141: "Gunzerker Greasy Grunt Pack",
  224142: "Psycho Party Pack", 224143: "Siren Learned Warrior Pack",
  224144: "Commando Devilish Good Looks Pack", 224145: "Assassin Cl0ckw0rk Pack",
  224160: "Mechromancer Steampunk Slayer Pack", 224161: "Gunzerker Dapper Gent Pack",
  224162: "Psycho Dark Psyche Pack", 224163: "Psycho Madness Pack",
  224164: "Psycho Supremacy Pack", 224165: "Psycho Domination Pack",
  224166: "Siren Glitter and Gore Pack", 224167: "Commando Haggard Hunter Pack",
  224168: "Assassin Stinging Blade Pack",
  224180: "Ultimate Vault Hunter Upgrade Pack 2",
  224200: "Ultimate Vault Hunter Upgrade Pack",
  225820: "Commando Madness Pack", 225821: "Commando Supremacy Pack",
  225822: "Commando Domination Pack", 225823: "Siren Madness Pack",
  225824: "Siren Supremacy Pack", 225825: "Siren Domination Pack",
  225826: "Gunzerker Madness Pack", 225827: "Gunzerker Supremacy Pack",
  225828: "Gunzerker Domination Pack", 225829: "Mechromancer Madness Pack",
  225830: "Mechromancer Supremacy Pack", 225831: "Mechromancer Domination Pack",
  225832: "Assassin Madness Pack", 225833: "Assassin Supremacy Pack",
  225834: "Assassin Domination Pack", 230090: "Psycho Pack",
  245890: "Headhunter 1: Bloody Harvest", 245910: "Headhunter 2: Wattle Gobbler",
  245930: "Headhunter 3: Mercenary Day", 245950: "Headhunter 4: Wedding Day Massacre",
  245970: "Headhunter 5: Son of Crawmerax",
  872280: "Commander Lilith & the Fight for Sanctuary",
  941170: "Ultra HD Texture Pack"};

// The game was built against an old Steamworks SDK. Without this list
// gbe_fork hands out its newest interface versions, whose vtables don't
// match, and the game exits silently right after loading the menu map.
// Taken from the original libsteam_api.so (`strings`).
const STEAM_INTERFACES = [
  "SteamClient012", "SteamGameServer011", "SteamGameServerStats001",
  "SteamUser017", "SteamFriends014", "SteamUtils006", "SteamMatchMaking009",
  "SteamMatchMakingServers002", "STEAMUSERSTATS_INTERFACE_VERSION011",
  "STEAMAPPS_INTERFACE_VERSION006", "SteamNetworking005",
  "STEAMREMOTESTORAGE_INTERFACE_VERSION012",
  "STEAMSCREENSHOTS_INTERFACE_VERSION002", "STEAMHTTP_INTERFACE_VERSION002",
  "STEAMUNIFIEDMESSAGES_INTERFACE_VERSION001", "STEAMUGC_INTERFACE_VERSION001"];

// Appids of the DLC whose depots Steam installed ("dlcappid" in
// steamapps/appmanifest_<appid>.acf, two levels above the install dir).
function installedDlc(ctx) {
  const acf = `${ctx.installDir}/../../appmanifest_${ctx.appid}.acf`;
  if (!Janus.exists(acf)) { Janus.log(`no ${acf}; no DLC reported`); return []; }
  const ids = new Set();
  for (const m of Janus.readFile(acf).matchAll(/"dlcappid"\s+"(\d+)"/g)) ids.add(m[1]);
  return [...ids].sort();
}

// gbe_fork reads steam_settings/ under GseAppPath, which Janus sets to
// <mirror>/_janus_gbe_logs (created before this hook) with the debug build.
// Without GseAppPath it should use the lib's own dir, but this game loads
// the lib through RPATH "." and gbe_fork then finds nothing (empirically:
// same crash) — so play() points GseAppPath at the mirror for that case.
Game.onMirrorReady = function (ctx) {
  const dirs = ["steam_settings"];
  if (Janus.exists("_janus_gbe_logs")) dirs.push("_janus_gbe_logs/steam_settings");
  const dlc = ["[app::dlcs]", "unlock_all=0"];
  if (ctx.options.unlockDlc) {
    for (const id of installedDlc(ctx))
      dlc.push(`${id}=Borderlands 2: ${DLCS[id] || "DLC " + id}`);
  }
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

  if (ctx.width && ctx.height) {
    ctx.gameArgs.push("-windowed", `-ResX=${ctx.width}`, `-ResY=${ctx.height}`);
  }

  // The user ini only exists after the first start (UE3 regenerates one
  // without [IniVersion] from the defaults, so never create it ourselves).
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

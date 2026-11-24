// The Forest — Unity 5.6 (Mono), Windows build under Proton. Co-op is Bolt
// over Steam lobbies + Steam P2P (Steamworks.NET + CSteamworks.dll), no
// ticket validation, no EOS: Goldberg covers it. Join flow: Goldberg IN-GAME
// INVITES — the host's lobby screen has "Invite friends"
// (ActivateGameOverlayInviteDialog); the others get the invite
// (GameLobbyJoinRequested / LobbyInvite) and join while running. The
// "Join" game browser lists the host's lobby too (LAN discovery on).
// The game also parses "+connect_lobby <id>" (AutoJoinAfterMPInvite).
//
// Pads: the menus up to the host's lobby work with a controller, but the
// lobby's "Invite friends" button and the Join browser's rows are
// mouse-only (2026-11-01, fake pads: no D-pad path reaches them). So GUI
// sessions use Janus's AUTO-JOIN: only the host (player 1) starts; once it
// has created its lobby, Janus reads the lobby ID from the debug gbe_fork
// log and starts the others with "+connect_lobby <id>", which joins them
// straight into the lobby. Mouse fallback: click Invite friends.
//
// steam_api64.dll exists twice (install root + TheForest_Data/Plugins);
// the Goldberg mirror replaces both.
//
// Unity's "Screen resolution" launcher dialog (mouse-only, before the game
// starts, not skipped by -screen-* args): PlayerSettings
// displayResolutionDialog = 1 in TheForest_Data/globalgamemanagers. The
// mirror gets a real copy and onMirrorReady sets it to 0 (Disabled). Its
// neighbours (Unity 5.6 PlayerSettings order) make the pattern unique:
// ios/android/tizenShowActivityIndicatorOnLoading = -1, -1, -1,
// iosAppInBackgroundBehavior = 0, displayResolutionDialog = 1, then four
// bools = 1 (found at 0x1120 in build 2026-11-01).
Game.appid = 242760;
Game.name = "The Forest";
Game.maxPlayers = 8;
Game.exeName = "TheForest.exe";
Game.isolation = "full";
Game.borderless = false;
Game.steamInputFilter = false;
Game.goldberg = true;
Game.goldbergDisableLan = false;  // invites + the game browser between instances
Game.goldbergInvites = true;
Game.goldbergDebug = true;        // lobby ID in the gbe log (auto-join, GUI panel)
Game.goldbergAutoJoin = true;     // GUI: the others start once the host's lobby exists
Game.goldbergHostInstance = 1;
Game.oskEnabled = true;
Game.lobbyConnectArg = "+connect_lobby %LOBBY%";
Game.mirrorCopyInstead = ["TheForest_Data/globalgamemanagers"];
Game.launchMessage =
  "Only player 1's game starts at first. Player 1: Multiplayer > Host Game " +
  "> New Game (or Continue) > difficulty > Create Lobby. The other games " +
  "then start and join that lobby by themselves; when everyone is listed, " +
  "player 1 presses Start Game.\n\n" +
  "(Without auto-join: player 1 clicks \"Invite friends\" in the lobby " +
  "with the mouse.)";

// Old Steamworks SDK (2015): gbe_fork must be told the interface versions
// (steam_interfaces.txt beside each replaced steam_api64.dll, the strings of
// Valve's DLL), or it pops "INTERFACE=... CALLER FN=Steam_Client::..." boxes.
const STEAM_INTERFACES = [
  "SteamClient017", "SteamGameServer012", "SteamGameServerStats001",
  "SteamUser018", "SteamFriends015", "SteamUtils007", "SteamMatchMaking009",
  "SteamMatchMakingServers002", "STEAMUSERSTATS_INTERFACE_VERSION011",
  "STEAMAPPS_INTERFACE_VERSION007", "SteamNetworking005",
  "STEAMREMOTESTORAGE_INTERFACE_VERSION012", "STEAMSCREENSHOTS_INTERFACE_VERSION002",
  "STEAMHTTP_INTERFACE_VERSION002", "STEAMUNIFIEDMESSAGES_INTERFACE_VERSION001",
  "STEAMCONTROLLER_INTERFACE_VERSION", "STEAMUGC_INTERFACE_VERSION005",
  "STEAMAPPLIST_INTERFACE_VERSION001", "STEAMMUSIC_INTERFACE_VERSION001",
  "STEAMMUSICREMOTE_INTERFACE_VERSION001", "STEAMHTMLSURFACE_INTERFACE_VERSION_002",
  "STEAMINVENTORY_INTERFACE_V001", "STEAMVIDEO_INTERFACE_V001",
];

Game.onMirrorReady = function (ctx) {
  for (const d of Janus.glob(`${ctx.gameDir}/**/steam_settings`))
    Janus.writeLines(`${d}/steam_interfaces.txt`, STEAM_INTERFACES);
  const n = Janus.binaryPatch("TheForest_Data/globalgamemanagers",
    "ffffffffffffffffffffffff00000000" + "01000000" + "01010101",
    "ffffffffffffffffffffffff00000000" + "00000000" + "01010101");
  if (n !== 1)
    Janus.warn("The Forest: couldn't switch off Unity's \"Screen resolution\" " +
               "dialog (a game update moved it). Click Play! in each game " +
               "window to start it.");
};

// Unity's resolution memory (HKCU\Software\SKS\TheForest).
const REG_KEY = "[Software\\\\SKS\\\\TheForest]";
function setUnityResolution(ctx) {
  const reg = `${ctx.winePrefix}/user.reg`;
  if (!Janus.exists(reg) || !ctx.width || !ctx.height) return;
  const vals = {
    "Screenmanager Resolution Width_h182942802": ctx.width,
    "Screenmanager Resolution Height_h2627697771": ctx.height,
    "Screenmanager Is Fullscreen mode_h3981298716": 0,
  };
  const dword = v => "dword:" + (v >>> 0).toString(16).padStart(8, "0");
  const lines = Janus.readFile(reg).split("\n");
  let start = lines.findIndex(l => l.startsWith(REG_KEY));
  if (start < 0) {
    lines.push("", `${REG_KEY} 0`);
    start = lines.length - 1;
  }
  let end = start + 1;
  while (end < lines.length && !lines[end].startsWith("[")) end++;
  for (const [name, v] of Object.entries(vals)) {
    const line = `"${name}"=${dword(v)}`;
    const i = lines.slice(start, end).findIndex(l => l.startsWith(`"${name}"=`));
    if (i >= 0) lines[start + i] = line;
    else { lines.splice(end - (lines[end - 1] === "" ? 1 : 0), 0, line); end++; }
  }
  Janus.writeFile(reg, lines.join("\n"));
}

Game.play = function (ctx) {
  if (!ctx.winePrefix) Janus.error("The Forest: no Wine prefix (expected a Proton launch).");
  setUnityResolution(ctx);
  if (ctx.width && ctx.height)
    ctx.gameArgs.push("-screen-fullscreen", "0", "-popupwindow",
                      "-screen-width", String(ctx.width),
                      "-screen-height", String(ctx.height));
};

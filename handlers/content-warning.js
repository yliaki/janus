// Content Warning — Unity, Proton (Windows-only build). Same Landfall
// "Zorro" network stack as PEAK (3527290): a Steam lobby carries the Photon
// room name/region ("Got PhotonRoomKey From Steam"), the gameplay itself runs
// on Photon Cloud. Both are satisfiable on one host: gbe_fork emulates the
// Steam lobby + invite, each instance is its own Photon Cloud client.
//
// *** Photon auth: REAL Steam tickets (Game.goldbergRealTicket). ***
// Landfall's Photon app validates the login ticket with Valve, and gbe_fork's
// fake ticket fails ("Failed to authenticate with server: Failed(2): error
// response from Steam", 2026-10-08). So a proxy steam_api64.dll in front of
// Goldberg swaps in a real session ticket that src/host/steam_ticket.py gets
// from the running Steam client (the user's account owns the game);
// everything else stays on Goldberg. Needs Steam running (the proxy DLL is
// committed prebuilt in vendor/ticketproxy/). See NOTES.md.
//
// Every instance's ticket is for the SAME real account, and Photon's Steam
// auth sets the player's UserId from the ticket, so the 2nd instance hit
// "Join failed: UserId '<real id>' already joined the specified game".
// PUN creates every room with CheckUserOnJoin; we patch that off in
// PhotonRealtime.dll (mirror copy) — see allowSameUserId() below.
//
// Optional fallback: your own Photon AppIds (options below — NOT needed,
// leave blank). The
// AppIds are 36-char GUIDs in PhotonServerSettings inside resources.assets,
// swapped in place in the Goldberg mirror (real install untouched). Same idea
// as the Thunderstore "Self Sufficient" mod. All instances must match.
Game.appid = 2881650;
Game.name = "Content Warning";
Game.maxPlayers = 4;
Game.launchMessage =
  "To play together: one player hosts a game, then chooses \"Invite\" in the " +
  "menu; the others join automatically.";
Game.isolation = "full";
// Copied from PEAK; revisit if the game turns out to call ISteamInput.
Game.steamInputFilter = false;
Game.goldberg = true;
Game.goldbergDisableLan = false;
Game.goldbergDebug = true;        // logs invites + lobby IDs (diagnostics)
Game.goldbergAutoJoin = false;
Game.goldbergInvites = true;      // in-game "Invite" pulls in running instances
Game.goldbergRealTicket = true;   // real Steam ticket for Landfall's Photon app
Game.oskEnabled = true;
Game.unityUiScale = true;         // "UI scale" option (BepInEx, NOTES.md)
Game.resizeGame = "unity";        // re-flow: the plugin resizes it to its new tile
// Experimental: the helmet HUD's corner groups sit at the screen edges, so the
// canvas as a whole can't grow; grow each group toward the centre instead.
// Works in wide tiles. In tall tiles the game re-positions most HUD blocks
// every frame (its AspectRatioUISetter + something else — switching the setter
// off didn't help), so those are left at the game's size (NOTES.md).
Game.unityUiScaleGroups = true;

// Landfall's AppIds as shipped in resources.assets (PhotonServerSettings).
const STOCK_PUN = "007fb385-31ff-490a-bb4d-b5740f5602b9";
const STOCK_VOICE = "f6a30374-c1ce-4ea5-8919-45bc29203bc9";
const GUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

// Fallback only. NOT needed: leave both blank and the game uses Landfall's
// own Photon app via the real Steam ticket above.
Janus.addOption("photonAppId", {
  label: "Photon PUN AppId (optional, leave blank)",
  description: "Not needed: leave blank. Blank = Landfall's own Photon servers, " +
               "logged in with a real Steam ticket (Steam must be running). " +
               "Only fill this in to use your own Photon app " +
               "(dashboard.photonengine.com) instead.",
  type: "string", default: ""});
Janus.addOption("photonVoiceAppId", {
  label: "Photon Voice AppId (optional, leave blank)",
  description: "Not needed: leave blank. Only used together with your own " +
               "PUN AppId above (proximity chat on your own Photon app).",
  type: "string", default: ""});

function hex(s) {
  let out = "";
  for (let i = 0; i < s.length; i++) out += s.charCodeAt(i).toString(16).padStart(2, "0");
  return out;
}

function swapAppId(file, stock, mine, what) {
  if (!mine) return;                  // Landfall's app + real Steam ticket
  mine = mine.trim().toLowerCase();
  if (!GUID.test(mine)) Janus.error(`Content Warning: ${what} AppId "${mine}" is not a Photon AppId (GUID).`);
  const n = Janus.binaryPatch(file, hex(stock), hex(mine));
  if (n !== 1) Janus.warn(`Content Warning: expected 1 ${what} AppId in ${file}, patched ${n} — game update?`);
  else Janus.log(`${what} AppId -> ${mine}`);
}

// Photon.Realtime.LoadBalancingPeer.RoomOptionsToOpParameters always marks
// rooms CheckUserOnJoin, twice: `flags |= 1` (ldloc.1 ldc.i4.1 or stloc.1)
// and right after it `op[232 /*ParameterCode.CheckUserOnJoin*/] = true`
// (ldarg.1 ldc.i4 232 ldc.i4.1 ...). Turn both ldc.i4.1 into ldc.i4.0 so the
// rooms we create (game + "_voice_") accept the same UserId more than once.
const CHECK_USER_ON = "0717600b0320e800000017";
const CHECK_USER_OFF = "0716600b0320e800000016";

function allowSameUserId() {
  const n = Janus.binaryPatch("Content Warning_Data/Managed/PhotonRealtime.dll",
                              CHECK_USER_ON, CHECK_USER_OFF);
  if (n !== 1) Janus.warn("Content Warning: couldn't find PUN's CheckUserOnJoin " +
                          `in PhotonRealtime.dll (patched ${n}) — game update? ` +
                          "Joining a second instance will fail with \"UserId " +
                          "already joined\".");
  else Janus.log("PhotonRealtime: CheckUserOnJoin off");
}

Game.onMirrorReady = function (ctx) {
  // Only needed with the shared real-account ticket; own AppIds still auth
  // with distinct (Goldberg) UserIds, but turning it off is harmless there.
  allowSameUserId();
  const file = "Content Warning_Data/resources.assets";
  swapAppId(file, STOCK_PUN, ctx.options.photonAppId, "PUN");
  swapAppId(file, STOCK_VOICE, ctx.options.photonVoiceAppId, "Voice");
};

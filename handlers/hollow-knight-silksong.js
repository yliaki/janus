// Hollow Knight: Silksong — Unity 6 (Mono), native Linux build. The game has
// no multiplayer; co-op comes from the SSMP mod (Silksong Multiplayer,
// github.com/Extremelyd1/SSMP, by the HKMP team): every player runs their
// own game with their own save, and SSMP shows the others (and their
// attacks, optionally PvP) in the same rooms. Janus doesn't ship the mod:
// on the first launch the handler downloads the pinned versions below
// (sha256-checked) into ~/.local/share/janus/mods/1030300/ and copies them
// into every instance's mirror, never into the Steam install.
//
// - BepInEx: the Silksong community's BepInExPack (Thunderstore
//   silksong_modding/BepInExPack_Silksong): BepInEx 5.4.23.4 + the native
//   doorstop for Linux + a BepInEx.cfg tuned for Unity 6 (HideManagerGameObject).
//   On Linux doorstop is an LD_PRELOAD library; buildCommand preloads it into
//   the game process only (sh -c in the runtime container), so gamescope
//   and the container tools never load it.
// - Goldberg gives every player their own Steam ID and name, which SSMP and
//   the save folder (~/.config/unity3d/Team Cherry/Hollow Knight Silksong/
//   <steamid>/) use. isolation "full": every profile has its own home.
// - Joining: SSMP's "Start Multiplayer" menu. The host hosts (Steam lobby or
//   direct/LAN), the others join by IP 127.0.0.1 (pre-filled in each
//   player's ssmp/modsettings.json, with the player's name) or accept the
//   host's Steam invite (Goldberg invites).
Game.appid = 1030300;
Game.name = "Hollow Knight: Silksong";
Game.maxPlayers = 4;
Game.isolation = "full";
Game.borderless = false;
Game.steamInputFilter = false;   // Unity Input System; no ISteamInput calls
Game.goldberg = true;            // own Steam ID per player + the mirror for the mods
Game.goldbergDisableLan = false; // SSMP Steam lobbies/invites between instances
Game.goldbergInvites = true;
Game.oskEnabled = true;          // SSMP's connect menu asks for text
// Unity's player resolves /proc/self/exe and keeps its single-instance lock
// (boot.config "single-instance") as unity.lock beside it: through the
// mirror's symlink that was the Steam install, shared by every instance
// ("Player is already running", 2026-11-01). A real copy of the small
// executable per mirror keeps the lock (and the data dir lookup) there.
Game.mirrorCopyInstead = ["Hollow Knight Silksong"];
Game.mirrorExclude = ["unity.lock"];
Game.launchMessage =
  "Multiplayer comes from the SSMP mod. Each player loads their own save.\n\n" +
  "SSMP's multiplayer menu needs the MOUSE (click a game to give it the " +
  "mouse): one player chooses \"Start Multiplayer\" on the title screen, " +
  "opens the Direct IP tab and clicks HOST LOBBY; the others choose " +
  "\"Start Multiplayer\", Direct IP, and CONNECT (127.0.0.1 and your name " +
  "are filled in). Then pick a save slot.";

// Pinned downloads (verified 2026-11-01). Bump together with a test.
const MODS = {
  pack: {
    url: "https://thunderstore.io/package/download/silksong_modding/BepInExPack_Silksong/1.0.3/",
    sha256: "a343275c6953ff12233e059d6d406fbdb9faf795fa695f12d5e7ad5c70fb6da7",
    label: "BepInExPack for Silksong 1.0.3",
  },
  ssmp: {
    url: "https://github.com/Extremelyd1/SSMP/releases/download/v0.3.1/SSMP.zip",
    sha256: "5e47a43fe715bfc46dedfceb0634dc2754d3ca0292b2de1371021a25eb7fec83",
    label: "SSMP 0.3.1",
  },
};

Janus.addOption("ssmp", {
  label: "Multiplayer mod (SSMP)",
  description: "Load SSMP so the players can join each other's game. Off = " +
               "plain Silksong (each player alone, own save).",
  type: "bool", default: true});

const modsDir = ctx => ctx.profilesDir.replace(/\/profiles\/?$/, "") + `/mods/${ctx.appid}`;
const PACK = "BepInExPack_Silksong-1.0.3";
const SSMP = "SSMP-0.3.1";

// Download + unpack once (hooks run one at a time, so the first instance
// does it and the others find it ready).
function fetchMods(ctx) {
  const dir = modsDir(ctx);
  for (const [name, m] of [[PACK, MODS.pack], [SSMP, MODS.ssmp]]) {
    if (Janus.exists(`${dir}/${name}/.complete`)) continue;
    if (Janus.exists(`${dir}/${name}`)) Janus.remove(`${dir}/${name}`);
    const zip = Janus.download(m.url, `${dir}/${name}.zip`, {sha256: m.sha256, label: m.label});
    Janus.extract(zip, `${dir}/${name}`);
    Janus.remove(zip);
    Janus.writeFile(`${dir}/${name}/.complete`, m.url + "\n");
    Janus.log(`downloaded ${m.label}`);
  }
}

Game.onMirrorReady = function (ctx) {
  if (!ctx.options.ssmp) return;
  fetchMods(ctx);
  const dir = modsDir(ctx);
  // BepInEx/ + libdoorstop.so beside the executable (the mirror is rebuilt
  // every launch, so these are fresh copies).
  Janus.copy(`${dir}/${PACK}/BepInExPack/BepInEx`, `${ctx.gameDir}/BepInEx`);
  Janus.copy(`${dir}/${PACK}/BepInExPack/libdoorstop.so`, `${ctx.gameDir}/libdoorstop.so`);
  Janus.copy(`${dir}/${SSMP}`, `${ctx.gameDir}/BepInEx/plugins/SSMP`);
  Janus.remove(`${ctx.gameDir}/BepInEx/plugins/SSMP/.complete`);
};

Game.buildCommand = function (ctx) {
  const exe = `${ctx.gameDir}/Hollow Knight Silksong`;
  if (!ctx.options.ssmp) return [exe];
  // Inside the runtime container, put doorstop in front of whatever the
  // container's LD_PRELOAD is (the Janus hook) for the game process only.
  return ["/bin/sh", "-c",
          'LD_PRELOAD="$JANUS_DOORSTOP_LIB${LD_PRELOAD:+:$LD_PRELOAD}" exec "$@"',
          "silksong", exe];
};

const dataDir = ctx => `${ctx.home}/.config/unity3d/Team Cherry/Hollow Knight Silksong`;

// SSMP's own settings: the player's name and the address to join. SSMP
// keeps them in the game's save folder, which is per Steam account:
// <data>/<account id>/ssmp/modsettings.json (account id = the 32-bit
// SteamID; the Goldberg ID of the player's profile).
function ssmpSettings(ctx) {
  const account = (BigInt(ctx.profile.steamId) - 76561197960265728n).toString();
  const f = `${dataDir(ctx)}/${account}/ssmp/modsettings.json`;
  let s = {};
  if (Janus.exists(f)) {
    try { s = Janus.readJson(f); } catch (e) { s = {}; }
  }
  // SSMP limits names to 20 letters/digits/spaces.
  s.Username = ctx.profile.name.replace(/[^A-Za-z0-9 ]/g, "").slice(0, 20) || "Player";
  if (!s.ConnectAddress) s.ConnectAddress = "127.0.0.1";
  if (!s.ConnectPort) s.ConnectPort = 26960;
  Janus.writeJson(f, s);
}

Game.play = function (ctx) {
  if (ctx.options.ssmp) {
    ctx.env.DOORSTOP_ENABLED = "1";
    ctx.env.DOORSTOP_TARGET_ASSEMBLY = `${ctx.gameDir}/BepInEx/core/BepInEx.Preloader.dll`;
    ctx.env.JANUS_DOORSTOP_LIB = `${ctx.gameDir}/libdoorstop.so`;
    ssmpSettings(ctx);
  }
  if (ctx.width && ctx.height)
    ctx.gameArgs.push("-screen-fullscreen", "0",
                      "-screen-width", String(ctx.width),
                      "-screen-height", String(ctx.height));
};

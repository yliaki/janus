// Hollow Knight — Unity 2020.2 (Mono), Windows build under Proton. The game
// has no multiplayer; co-op comes from HKMP (Hollow Knight Multiplayer,
// github.com/Extremelyd1/HKMP) on the community Modding API
// (github.com/hk-modding/api): every player runs their own game and save,
// HKMP shows the others in the same rooms (optional PvP, teams, skins).
// Janus doesn't ship the mods: the first launch downloads the pinned
// versions below (sha256-checked) into ~/.local/share/janus/mods/367520/,
// and every instance's mirror gets a real copy of hollow_knight_Data/Managed
// with the Modding API's assemblies and Mods/HKMP over it. The Steam install
// is never written.
//
// *** Needs the game's "1.5.78.11833" beta branch. *** The Modding API only
// exists for 1.5.78.11833; the 2025 update (1.5.12xxx, Unity 6) broke every
// mod. Steam: Hollow Knight -> Properties -> Betas -> 1.5.78.11833. The
// handler checks the installed version (Assembly-CSharp's Constants) and
// refuses with these instructions otherwise (option "hkmp" off = plain game).
//
// Joining: HKMP's panel in the pause menu. One player presses "Start
// Hosting" (port 26950), the others "Connect" to 127.0.0.1:26950, which
// the handler pre-fills in each player's HkmpMod.GlobalSettings.json along
// with the player's name. No Steam networking: Goldberg is only on for the
// per-instance mirror and a Steam identity per player.
Game.appid = 367520;
Game.name = "Hollow Knight";
Game.maxPlayers = 4;
Game.exeName = "hollow_knight.exe";
// No Game.resizeGame: the plugin needs BepInEx, whose MonoMod/Cecil break the
// Modding API's (HKMP: MissingMethodException in MonoMod, "Failed to
// initialize"), and the game keeps a 16:9 picture in any tile anyway.
Game.isolation = "full";
Game.borderless = false;
Game.steamInputFilter = false;
Game.goldberg = true;              // the mirror (mods) + an identity per player
Game.goldbergDisableLan = true;    // HKMP connects by IP; nothing to discover
Game.oskEnabled = true;            // HKMP's connect panel takes text
Game.mirrorCopyInstead = ["hollow_knight_Data/Managed"];  // the API replaces files here
Game.launchMessage =
  "Multiplayer comes from the HKMP mod. Each player loads their own save.\n\n" +
  "HKMP's panel is in the pause menu and needs the MOUSE (click a game to " +
  "give it the mouse): one player clicks \"Start Hosting\"; the others " +
  "click \"Connect\" (127.0.0.1 and your name are filled in).";

const MODS = {
  api: {
    url: "https://github.com/hk-modding/api/releases/download/1.5.78.11833-77/moddingapi.v77.windows.zip",
    sha256: "bc9f0db3d0916b05cd5a2420bb602fb1b239ce3ff6c289fd84bffb682fb8f1d6",
    label: "Hollow Knight Modding API v77",
    dir: "ModdingAPI-77",
  },
  hkmp: {
    url: "https://github.com/Extremelyd1/HKMP/releases/download/v2.4.3/HKMP.zip",
    sha256: "1a31d197e4a843d012daabfa940298ac58f1502bda51e9611d875a8a62e70948",
    label: "HKMP 2.4.3",
    dir: "HKMP-2.4.3",
  },
};
const GAME_VERSION = "1.5.78.11833";

Janus.addOption("hkmp", {
  label: "Multiplayer mod (HKMP)",
  description: "Load the Modding API and HKMP so the players can join each " +
               "other's game (needs the game's 1.5.78.11833 beta branch). " +
               "Off = plain Hollow Knight, each player alone.",
  type: "bool", default: true});
Janus.addOption("importSave", {
  label: "Copy my save into",
  description: "Copy your real saves (all four slots) into a player's " +
               "profile the first time it plays: \"all\" = every profile, " +
               "\"first\" = only the Steam account's player. It is a copy: " +
               "your real saves are never written. \"none\" = fresh start.",
  type: "choice", choices: ["all", "first", "none"], default: "first"});

const modsDir = ctx => ctx.profilesDir.replace(/\/profiles\/?$/, "") + `/mods/${ctx.appid}`;
const MANAGED = "hollow_knight_Data/Managed";

// The installed game's version: the highest "1.x.y[.z]" literal in
// Assembly-CSharp.dll (UTF-16 in the #US heap). The first one isn't it: the
// save-upgrade code lists older versions (1.3.0.0 ... 1.4.0.0) before it,
// and the 2025 updates have three parts (1.5.12620).
function installedVersion(ctx) {
  const dll = `${ctx.installDir}/${MANAGED}/Assembly-CSharp.dll`;
  const r = Janus.run(["sh", "-c", 'strings -el "$1" | grep -E "^1\\.[0-9]+\\.[0-9]+(\\.[0-9]+)?$" | sort -V | tail -1', "sh", dll]);
  return r.stdout.trim() || null;
}

function fetchMods(ctx) {
  const dir = modsDir(ctx);
  for (const m of [MODS.api, MODS.hkmp]) {
    if (Janus.exists(`${dir}/${m.dir}/.complete`)) continue;
    if (Janus.exists(`${dir}/${m.dir}`)) Janus.remove(`${dir}/${m.dir}`);
    const zip = Janus.download(m.url, `${dir}/${m.dir}.zip`, {sha256: m.sha256, label: m.label});
    Janus.extract(zip, `${dir}/${m.dir}`);
    Janus.remove(zip);
    Janus.writeFile(`${dir}/${m.dir}/.complete`, m.url + "\n");
    Janus.log(`downloaded ${m.label}`);
  }
}

Game.onMirrorReady = function (ctx) {
  if (!ctx.options.hkmp) return;
  const v = installedVersion(ctx);
  if (v !== GAME_VERSION)
    Janus.error(
      `HKMP needs Hollow Knight ${GAME_VERSION}, but version ${v || "unknown"} ` +
      "is installed.\n\nIn Steam: Hollow Knight → Properties → Betas → choose " +
      `"${GAME_VERSION}", wait for the download, then launch again. Or turn ` +
      "the \"Multiplayer mod (HKMP)\" option off to play without it.");
  fetchMods(ctx);
  const dir = modsDir(ctx);
  const managed = `${ctx.gameDir}/${MANAGED}`;
  // Real copies (mirrorCopyInstead), so overwriting never touches the install.
  for (const f of Janus.glob(`${dir}/${MODS.api.dir}/*`)) {
    const name = f.split("/").pop();
    if (/\.(md|xml)$/i.test(name) || name === ".complete") continue;
    Janus.copy(f, `${managed}/${name}`);
  }
  Janus.copy(`${dir}/${MODS.hkmp.dir}`, `${managed}/Mods/HKMP`);
  Janus.remove(`${managed}/Mods/HKMP/.complete`);
};

const dataDir = ctx =>
  `${ctx.winePrefix}/drive_c/users/steamuser/AppData/LocalLow/Team Cherry/Hollow Knight`;

function seedSave(ctx) {
  const mode = ctx.options.importSave;
  if (!(mode === "all" || (mode === "first" && ctx.profile.owner))) return;
  const dst = dataDir(ctx);
  if (Janus.glob(`${dst}/user*.dat`).length) return;
  const src = `${ctx.installDir}/../../compatdata/${ctx.appid}/pfx/drive_c/users/steamuser/AppData/LocalLow/Team Cherry/Hollow Knight`;
  const saves = Janus.glob(`${src}/user[1-4].dat`);
  if (src === dst || !saves.length) { Janus.log("no real save found; starting fresh"); return; }
  Janus.mkdir(dst);
  for (const f of saves) Janus.copy(f, `${dst}/${f.split("/").pop()}`);
  Janus.log(`saves ${src} -> ${dst}`);
}

// HKMP's settings (Modding API global settings, persistentDataPath).
function hkmpSettings(ctx) {
  const f = `${dataDir(ctx)}/HkmpMod.GlobalSettings.json`;
  let s = {};
  if (Janus.exists(f)) {
    try { s = Janus.readJson(f); } catch (e) { s = {}; }
  }
  // HKMP limits names to 20 letters/digits/spaces.
  s.Username = ctx.profile.name.replace(/[^A-Za-z0-9 ]/g, "").slice(0, 20) || "Player";
  if (!s.ConnectAddress) s.ConnectAddress = "127.0.0.1";
  if (!s.ConnectPort) s.ConnectPort = 26950;
  s.AutoConnectWhenHosting = true;   // the host joins its own server
  Janus.writeJson(f, s);
}

// Unity's resolution memory (HKCU\Software\Team Cherry\Hollow Knight).
const REG_KEY = "[Software\\\\Team Cherry\\\\Hollow Knight]";
function setUnityResolution(ctx) {
  const reg = `${ctx.winePrefix}/user.reg`;
  if (!Janus.exists(reg) || !ctx.width || !ctx.height) return;
  const vals = {
    "Screenmanager Resolution Width_h182942802": ctx.width,
    "Screenmanager Resolution Height_h2627697771": ctx.height,
    "Screenmanager Fullscreen mode_h3630240806": 3,          // windowed
    "Screenmanager Resolution Use Native_h1405027254": 0,
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
  if (!ctx.winePrefix) Janus.error("Hollow Knight: no Wine prefix (expected a Proton launch).");
  seedSave(ctx);
  if (ctx.options.hkmp) hkmpSettings(ctx);
  setUnityResolution(ctx);
  if (ctx.width && ctx.height)
    ctx.gameArgs.push("-screen-fullscreen", "0",
                      "-screen-width", String(ctx.width),
                      "-screen-height", String(ctx.height));
};

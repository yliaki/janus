// Gunfire Reborn — Unity 2018.4 (IL2CPP), Proton (Windows-only build).
//
// - DLL check: GameInit.PackageTesting verifies Plugins/steam_api64.dll at
//   start-up; Goldberg's DLL fails it ("Game files integrity verification
//   failed, code:1", 2026-10-18). Valve's DLL in the symlinked mirror passes.
//   Same recipe as PAYDAY 2 (NOTES.md): keep Valve's steam_api64.dll
//   (mirrorCopyInstead) and run gbe_fork one level lower, as the Steam client:
//   ColdClientLoader + its steamclient64.dll (payload, gbe_fork
//   release-2026_05_30). The Nucleus Co-op handler does the same.
// - Co-op: Steam lobbies through the emulator. New Adventure -> room, "+"
//   slot -> "Invite": every other instance is pulled into the room, no
//   prompt (Game.goldbergInvites, needs LAN discovery on; validated
//   2026-10-18, 2 tiles, fakepad). Janus writes the invite switches beside
//   steam_api64.dll (Plugins/); onMirrorReady moves them beside
//   steamclient64.dll, where gbe_fork reads them.
// - Saves: Steam Cloud's remote/user_1.dat, which gbe_fork keeps under
//   GSE Saves/1217060/remote in each player's prefix. Optionally seeded from
//   the real Steam Cloud copy (importSave).
// - Pads: InControl over XInput under Wine. Default: Steam Input, i.e. each
//   game reads its own Steam virtual pad with the game's Steam layout
//   (validated 2026-10-18 with fake pads, incl. Steam reordering its slots
//   after launch). The "steamInput" option off bypasses Steam Input
//   (JANUS_NO_STEAM_INPUT): each game reads its physical pad through Wine's
//   evdev backend.
// - payload/inject/janus_uiscale.dll (src/il2cpp_uiscale/), injected by the
//   loader (DllsToInjectFolder): IL2CPP with embedded/encrypted metadata, so
//   no BepInEx/MelonLoader; it works through the il2cpp runtime API.
//   * "UI scale" option: enlarges every uGUI canvas, each menu only as far
//     as nothing is pushed off screen and no text gets covered.
//   * Tile size: the game shrinks its window to 3/4 of the height (960x810
//     in a 960x1080 tile, letterboxed); the DLL re-applies the tile size
//     (JANUS_SCREEN_SIZE), setUnityResolution() seeds Unity's registry.
//   NOTES.md "Gunfire Reborn: UI scale and tile size".
Game.appid = 1217060;
Game.name = "Gunfire Reborn";
Game.maxPlayers = 4;
Game.exeName = "Gunfire Reborn.exe";
Game.isolation = "full";
Game.borderless = false;
Game.steamInputFilter = false;
Game.goldberg = true;
Game.goldbergDisableLan = false;  // instances see each other as friends
Game.goldbergInvites = true;
// Re-flow (dynamic players): payload/inject/janus_uiscale.dll re-reads
// $JANUS_SIZE_FILE and keeps the game windowed at that size.
Game.resizeGame = "file";      // an invite pulls the others into the room
Game.oskEnabled = true;
Game.mirrorCopyInstead = ["Gunfire Reborn_Data/Plugins"];  // keep Valve's DLL
Game.launchMessage =
  "A new player starts in the tutorial: press Back (View) and choose " +
  "\"Skip the Tutorial\", then quit the run to reach the menu.\n\n" +
  "To play together: one player chooses New Adventure, selects a \"+\" " +
  "slot in the room and picks \"Invite\"; the others are pulled in " +
  "automatically.";

const DLCS = [
  [2111850, "Gunfire Reborn - Visitors of Spirit Realm"],
  [2430400, "Gunfire Reborn - Artisan and Magician"],
  [3063250, "Gunfire Reborn - Realm of Frost and Inkwash"],
  [3920680, "Gunfire Reborn - Echoes From Primal Grove"],
];

Janus.addOption("unlockAllDlc", {
  label: "Unlock all DLC (Goldberg)",
  description: "Have the emulator report every DLC (heroes) as owned. Off = " +
               "report none.",
  type: "bool", default: false});
Janus.addOption("steamInput", {
  label: "Use Steam Input",
  description: "On: controllers go through Steam Input (your Steam controller " +
               "settings apply; needs \"Enable Steam Input\" for the game). " +
               "Off: each game reads its controller directly. Turn it off if " +
               "the controllers do nothing in the game.",
  type: "bool", default: true});
if (!Janus.getOption("steamInput")) {
  Game.requireSteamInput = false;
  Game.extraEnv = {JANUS_NO_STEAM_INPUT: "1"};
}
const UI_SCALES = [];
for (let i = 10; i <= 25; i++) UI_SCALES.push((i / 10).toFixed(1));
Janus.addOption("uiScale", {
  label: "UI scale",
  description: "Enlarge the game's menus and HUD in small screens. Each menu " +
               "grows up to this factor, only as far as it still fits on " +
               "screen. 1.0 = the game's own size (nothing injected).",
  type: "choice", choices: UI_SCALES, default: "1.0"});
Janus.addOption("importSave", {
  label: "Copy my save into",
  description: "Copy your real Steam Cloud save (talents, unlocks) into a " +
               "player's profile the first time it plays: \"all\" = every " +
               "profile, \"first\" = only the Steam account's player. It is a " +
               "copy: your real save is never written. \"none\" = fresh start.",
  type: "choice", choices: ["all", "first", "none"], default: "none"});

// ColdClientLoader + steamclient64.dll + steam_settings/ for this instance:
// in the player's prefix, outside the game folder.
const GBE_REL = "users/steamuser/Janus/gunfire-gbe";
const gbeDir = ctx => `${ctx.winePrefix}/drive_c/${GBE_REL}`;
const winPath = p => "Z:" + p.replace(/\//g, "\\");
const gseRemote = ctx =>
  `${ctx.winePrefix}/drive_c/users/steamuser/AppData/Roaming/GSE Saves/${ctx.appid}/remote`;

Game.onMirrorReady = function (ctx) {
  if (!ctx.winePrefix) Janus.error("Gunfire Reborn: no Wine prefix (expected a Proton launch).");
  const dst = gbeDir(ctx);
  if (Janus.exists(dst)) Janus.remove(dst, {force: true});
  Janus.mkdir(dst);
  Janus.deployPayload(dst);  // coldclient/
  // gbe_fork reads steam_settings/ beside steamclient64.dll. Janus writes its
  // files (invites, overlay) beside steam_api64.dll, here in Plugins/.
  const settings = `${dst}/coldclient/steam_settings`;
  Janus.mkdir(settings);
  for (const src of ["steam_settings", "Gunfire Reborn_Data/Plugins/steam_settings"]) {
    if (!Janus.exists(src)) continue;
    for (const f of Janus.glob(`${ctx.gameDir}/${src}/*`))
      Janus.copy(f, `${settings}/${f.split("/").pop()}`);
    Janus.remove(src, {force: true});
  }
  const unlock = ctx.options.unlockAllDlc;
  Janus.writeFile(`${settings}/configs.app.ini`,
    `[app::dlcs]\nunlock_all=0\n` +
    (unlock ? DLCS.map(([id, name]) => `${id}=${name}\n`).join("") : ""));
  Janus.writeFile(`${dst}/coldclient/ColdClientLoader.ini`, [
    "[SteamClient]",
    `Exe=${winPath(ctx.gameDir)}\\Gunfire Reborn.exe`,
    `ExeRunDir=${winPath(ctx.gameDir)}`,
    "ExeCommandLine=",
    `AppId=${ctx.appid}`,
    "SteamClientDll=steamclient64.dll",
    "SteamClient64Dll=steamclient64.dll",
    "[Injection]",
    "ForceInjectSteamClient=0",
    "ForceInjectGameOverlayRenderer=0",
    `DllsToInjectFolder=${winPath(dst)}\\inject`,
    "IgnoreInjectionError=1",
    "IgnoreLoaderArchDifference=0",
    "[Persistence]",
    "Mode=0",
    "[Debug]",
    "ResumeByDebugger=0",
    ""].join("\r\n"));
};

const uiScale = ctx => parseFloat(ctx.options.uiScale) || 1;

// The loader passes its own arguments on to the game.
Game.buildCommand = function (ctx) {
  return [`${gbeDir(ctx)}/coldclient/steamclient_loader_x64.exe`];
};

// Newest Steam Cloud copy of the save among the machine's Steam accounts.
function realSaveDir(ctx) {
  const dirs = Janus.glob(`~/.steam/steam/userdata/*/${ctx.appid}/remote`)
    .filter(d => Janus.exists(`${d}/user_1.dat`));
  if (dirs.length <= 1) return dirs[0] || null;
  const r = Janus.run(["stat", "-c", "%Y %n"].concat(dirs.map(d => `${d}/user_1.dat`)));
  return r.stdout.trim().split("\n").map(l => l.split(" "))
    .sort((a, b) => Number(b[0]) - Number(a[0]))[0].slice(1).join(" ")
    .replace(/\/user_1\.dat$/, "");
}

function seedSave(ctx) {
  const mode = ctx.options.importSave;
  if (!(mode === "all" || (mode === "first" && ctx.profile.owner))) return;
  const dst = gseRemote(ctx);
  if (Janus.exists(`${dst}/user_1.dat`)) return;
  const src = realSaveDir(ctx);
  if (!src) { Janus.log("no Steam Cloud save found; starting fresh"); return; }
  Janus.mkdir(dst);
  for (const f of Janus.glob(`${src}/*`)) Janus.copy(f, `${dst}/${f.split("/").pop()}`);
  Janus.log(`save ${src} -> ${dst}`);
}

// Unity's own resolution memory (HKCU\Software\duoyi\Gunfire Reborn, Unity's
// hashed value names). Left alone the game comes up at its previous size, or
// gamescope's largest mode rotated in a portrait tile (1080x960 in 960x1080),
// letterboxed. user.reg is plain text and the prefix isn't running yet.
const REG_KEY = "[Software\\\\duoyi\\\\Gunfire Reborn]";
function setUnityResolution(ctx) {
  const reg = `${ctx.winePrefix}/user.reg`;
  if (!Janus.exists(reg)) return;
  const vals = {
    "Screenmanager Resolution Width_h182942802": ctx.width,
    "Screenmanager Resolution Height_h2627697771": ctx.height,
    "Screenmanager Fullscreen mode_h3630240806": 1,          // fullscreen window
    "Screenmanager Resolution Use Native_h1405027254": 0,
  };
  const dword = v => "dword:" + (v >>> 0).toString(16).padStart(8, "0");
  let lines = Janus.readFile(reg).split("\n");
  let start = lines.indexOf(lines.find(l => l.startsWith(REG_KEY)));
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
  // The game's own display mode setting (2 = borderless), once it exists.
  const us = `${ctx.winePrefix}/drive_c/users/steamuser/AppData/LocalLow/duoyi/Gunfire Reborn/usersetting.ini`;
  if (Janus.exists(us)) Janus.iniSet(us, "Setting", "305", "2");
}

Game.play = function (ctx) {
  // With goldbergDebug on, Janus points GseAppPath at the mirror; the
  // emulator must keep reading the steam_settings/ written above.
  ctx.env.GseAppPath = `${gbeDir(ctx)}/coldclient/`;
  seedSave(ctx);
  // payload/inject/janus_uiscale.dll (always injected): UI scale, and the
  // tile size forced on the game's SetResolution calls (see setUnityResolution).
  ctx.env.JANUS_UI_SCALE = String(uiScale(ctx));
  if (ctx.width && ctx.height) ctx.env.JANUS_SCREEN_SIZE = `${ctx.width}x${ctx.height}`;
  if (ctx.verbose) ctx.env.JANUS_UI_VERBOSE = "1";
  if (ctx.width && ctx.height) {
    setUnityResolution(ctx);
    ctx.gameArgs.push("-screen-fullscreen", "0", "-popupwindow",
                      "-screen-width", String(ctx.width),
                      "-screen-height", String(ctx.height));
  }
};

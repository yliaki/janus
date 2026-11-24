// Risk of Rain 2 — Unity 2021.3 (Mono), Proton. Online co-op is Unity HLAPI
// over Facepunch.Steamworks (Steam lobbies + Steam P2P), so Goldberg covers
// it. Since Survivors of the Void the game also runs Epic Online Services
// crossplay, which can't work under Goldberg: "--disableCrossplay" (a switch
// in RoR2.dll) keeps it on Steam lobbies only. Join flow: Goldberg IN-GAME
// INVITES — a player opens a multiplayer lobby and presses Invite (the
// lobby's InviteButton -> ActivateGameOverlayInviteDialog); the others get
// a lobby invite and join while running.
//
// Settings and saves: the game keeps both in Steam remote storage,
// UserProfiles/config.cfg (convars: window_mode, resolution, intro_skip, ...)
// and UserProfiles/<guid>.xml (unlocks, logbook). Under Goldberg that's
// AppData/Roaming/GSE Saves/632360/remote/UserProfiles in each player's own
// prefix (isolation full). play() writes the tile size, windowed mode, the
// intro/splash skips and music off for players 2+ into config.cfg; the
// "importSave" option copies the real Steam Cloud profile in once.
//
// Input: Rewired over XInput/Windows.Gaming.Input under Wine; each instance
// sees only its own Steam virtual pad (the hook), nothing game-specific.
Game.appid = 632360;
Game.name = "Risk of Rain 2";
Game.maxPlayers = 4;
Game.exeName = "Risk of Rain 2.exe";
Game.isolation = "full";
Game.borderless = false;
Game.steamInputFilter = false;
Game.goldberg = true;
Game.goldbergDisableLan = false;  // invites need the instances to see each other
Game.goldbergInvites = true;      // Invite in the lobby pulls the others in
Game.oskEnabled = true;
Game.unityUiScale = true;         // "UI scale" option (BepInEx)
Game.resizeGame = "unity";        // re-flow: the plugin resizes it to its new tile
Game.gameArgs = "--disableCrossplay";
// Risk of Rain 2_Data/Config/config.cfg is read at startup (writeConfig):
// real copies per instance, not symlinks into the install.
Game.mirrorCopyInstead = ["Risk of Rain 2_Data/Config"];
Game.launchMessage =
  "To play together: one player chooses Multiplayer > Host Game and presses " +
  "Invite in the lobby; the others join automatically.";

Janus.addOption("importSave", {
  label: "Copy my save into",
  description: "Copy your real Steam Cloud profile (unlocked survivors, items, " +
               "logbook) into a player's profile the first time it plays: " +
               "\"all\" = every profile, \"first\" = only the Steam account's " +
               "player. It is a copy: your real save is never written. " +
               "\"none\" = fresh start.",
  type: "choice", choices: ["all", "first", "none"], default: "all"});
Janus.addOption("unlockAllDlc", {
  label: "Unlock all DLC (Goldberg)",
  description: "Have the emulator report the expansions (Survivors of the " +
               "Void, Seekers of the Storm, Alloyed Collective) as owned. " +
               "Off = report none. Every player must have the same setting " +
               "to play together.",
  type: "bool", default: false});

const DLCS = [
  [1607890, "Risk of Rain 2: Survivors of the Void"],
  [2306620, "Risk of Rain 2: Seekers of the Storm"],
  [2781620, "Risk of Rain 2: Alloyed Collective"],
];

// Goldberg's DLC list, beside every replaced steam_api64.dll.
Game.onMirrorReady = function (ctx) {
  const lines = ["[app::dlcs]", "unlock_all=0"];
  if (ctx.options.unlockAllDlc) for (const [id, name] of DLCS) lines.push(`${id}=${name}`);
  for (const d of Janus.glob(`${ctx.gameDir}/**/steam_settings`))
    Janus.writeLines(`${d}/configs.app.ini`, lines);
};

const profilesDir = ctx =>
  `${ctx.winePrefix}/drive_c/users/steamuser/AppData/Roaming/GSE Saves/${ctx.appid}/remote/UserProfiles`;

// The owner's Steam Cloud copy: userdata/<account id>/632360/remote/UserProfiles.
function realProfiles(ctx) {
  const owner = ctx.players.map(p => p.profile).find(p => p.owner) ||
                (ctx.profile.owner ? ctx.profile : null);
  const dirs = Janus.glob(`~/.steam/steam/userdata/*/${ctx.appid}/remote/UserProfiles`);
  if (owner && owner.steamId) {
    const account = (BigInt(owner.steamId) - 76561197960265728n).toString();
    const mine = dirs.find(d => d.includes(`/userdata/${account}/`));
    if (mine) return mine;
  }
  return dirs[0] || null;
}

function seedSave(ctx) {
  const mode = ctx.options.importSave;
  if (!(mode === "all" || (mode === "first" && ctx.profile.owner))) return;
  const dst = profilesDir(ctx);
  if (Janus.glob(`${dst}/*.xml`).length) return;      // already has a profile
  const src = realProfiles(ctx);
  if (!src || !Janus.glob(`${src}/*.xml`).length) {
    Janus.log("no Steam Cloud profile found; starting fresh");
    return;
  }
  Janus.mkdir(dst);
  for (const f of Janus.glob(`${src}/*`)) {
    const out = `${dst}/${f.split("/").pop()}`;
    Janus.copy(f, out);
    // Name the copied profile after the player (shown in the menus).
    if (out.endsWith(".xml") && !ctx.profile.owner) {
      const xml = Janus.readFile(out);
      Janus.writeFile(out, xml.replace(/<name>[^<]*<\/name>/,
        `<name>${ctx.profile.name.replace(/[<>&]/g, "")}</name>`));
    }
  }
  Janus.log(`save ${src} -> ${dst}`);
}

// config.cfg holds "name value;" lines; set (or add) some of them.
function setConvars(cfg, want) {
  let lines = Janus.exists(cfg)
    ? Janus.readFile(cfg).replace(/^﻿/, "").split(/\r?\n/) : [];
  for (const [k, v] of Object.entries(want)) {
    const line = `${k} ${v};`;
    // Every occurrence: the game's own file repeats its convars.
    let found = false;
    lines = lines.map(l => l.startsWith(k + " ") ? (found = true, line) : l);
    if (!found) lines.unshift(line);
  }
  Janus.writeFile(cfg, lines.join("\n"));
}

function writeConfig(ctx) {
  const cfg = `${profilesDir(ctx)}/config.cfg`;
  const want = {
    window_mode: "Window",
    intro_skip: "1",
    splash_skip: "1",
    audio_focused_only: "0",       // keep sound in the tiles without focus
  };
  if (ctx.width && ctx.height) want.resolution = `${ctx.width}x${ctx.height}x60`;
  // One soundtrack: music off in players 2+, the player's own volume kept
  // in janus-music-volume.txt (restored when that profile plays as P1).
  const keep = `${ctx.saveRoot}/janus-music-volume.txt`;
  const m = Janus.exists(cfg) && Janus.readFile(cfg).match(/^volume_music\s+([^;\s]+)/m);
  const cur = m ? m[1] : null;
  if (ctx.instance > 1) {
    if (cur && cur !== "0") Janus.writeFile(keep, cur);
    want.volume_music = "0";
  } else if (Janus.exists(keep)) {
    want.volume_music = Janus.readFile(keep).trim() || "100";
    Janus.remove(keep);
  }
  Janus.mkdir(profilesDir(ctx));
  setConvars(cfg, want);
  // The startup config in the game dir is read first; the intro/splash
  // skips only count there (the profile's copy loads after the intro).
  setConvars(`${ctx.gameDir}/Risk of Rain 2_Data/Config/config.cfg`, want);
  signInProfile(ctx);
}

// SaveSystem.LoadUserProfiles() walks UserProfiles/ in the order the
// storage lists it, loading each .xml and running each .cfg. Steam's cloud
// lists the profile first; Goldberg lists the directory (readdir order),
// which on ext4 can put config.cfg first. Its "user_profile_main <guid>"
// then signs in a profile that isn't loaded yet, nobody is signed in, and
// the main menu opens "Create Profile" (seen 2026-11-01). Console.
// RunConfigsAfterLoad() runs Config/autoexec.cfg after the profiles are
// loaded, so the sign-in is repeated there (the per-instance copy).
function signInProfile(ctx) {
  const dir = profilesDir(ctx);
  const cfg = `${dir}/config.cfg`;
  let main = Janus.exists(cfg) &&
             (Janus.readFile(cfg).match(/^user_profile_main\s+([^;\s]+)/m) || [])[1];
  const xmls = Janus.glob(`${dir}/*.xml`).map(f => f.split("/").pop().replace(/\.xml$/, ""));
  if (!main || !xmls.includes(main)) main = xmls.length === 1 ? xmls[0] : null;
  if (!main) return;                         // new player: the game asks for a name
  const autoexec = `${ctx.gameDir}/Risk of Rain 2_Data/Config/autoexec.cfg`;
  const text = Janus.exists(autoexec) ? Janus.readFile(autoexec) : "";
  // The title menu may already have opened "Create Profile" (it runs while
  // the game still loads); reloading the title scene (what the intro does
  // when it ends) starts it again with the profile signed in.
  Janus.writeFile(autoexec, text.replace(/\s*$/, "\n") +
                  `user_profile_main ${main};\nset_scene title;\n`);
}

// Unity's own resolution memory (HKCU\Software\Hopoo Games, LLC\Risk of
// Rain 2); the prefix isn't running yet, so user.reg is edited as text.
const REG_KEY = "[Software\\\\Hopoo Games, LLC\\\\Risk of Rain 2]";
function setUnityResolution(ctx) {
  const reg = `${ctx.winePrefix}/user.reg`;
  if (!Janus.exists(reg) || !ctx.width || !ctx.height) return;
  // Value names are Unity's PlayerPrefs keys: <name>_h<djb2-xor hash>.
  const vals = {
    "Screenmanager Resolution Width_h182942802": ctx.width,
    "Screenmanager Resolution Height_h2627697771": ctx.height,
    "Screenmanager Fullscreen mode_h3630240806": 3,          // windowed
    "Screenmanager Resolution Use Native_h1405027254": 0,
    // PlayerPrefs "HasShownFirstTimePopup": the "cross-play has arrived"
    // popup on the first Multiplayer visit (crossplay is off here anyway).
    "HasShownFirstTimePopup_h3858764215": 1,
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
  if (!ctx.winePrefix) Janus.error("Risk of Rain 2: no Wine prefix (expected a Proton launch).");
  seedSave(ctx);
  writeConfig(ctx);
  setUnityResolution(ctx);
  if (ctx.width && ctx.height)
    ctx.gameArgs.push("-screen-fullscreen", "0",
                      "-screen-width", String(ctx.width),
                      "-screen-height", String(ctx.height));
};

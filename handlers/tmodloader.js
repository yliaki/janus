// tModLoader — native Linux, .NET 8 (FNA/SDL2). Same handler as
// handlers/terraria.js (keep the two in step) with tModLoader's launch on top.
// See NOTES.md "Terraria / tModLoader".
//
// Multiplayer: as in terraria.js. Host & Play's server is tModLoader re-running
// its own dotnet with "tModLoader.dll -server -autoshutdown ... -lobby friends";
// the hook gives it its own Goldberg identity (JANUS_ARGV_ENV_MATCH), and
// replaces gbe_fork's invite call, which skips every instance because the
// host client isn't in the lobby (JANUS_GOLDBERG_INVITE_FIX).
//
// tModLoader-specific:
// - Launched straight as `dotnet/dotnet tModLoader.dll` (what
//   LaunchUtils/ScriptCaller.sh ends with). The scripts also download the
//   .NET runtime into the install dir on first start; onGameResolved does
//   that once with tModLoader's own installer.
// - InstallVerifier md5-checks Libraries/steamworks.net.anycpu/.../
//   libsteam_api.so against Valve's lib and exits on a mismatch, so the mirror
//   keeps the ORIGINAL file (mirrorCopyInstead: no Goldberg swap). Goldberg
//   still gets loaded: the hook redirects every dlopen of libsteam_api.so.
// - Vanilla assets come from the Terraria install, found with
//   SteamApps.GetAppInstallDir(105600); Goldberg answers that from
//   configs.app.ini [app::paths]. Same for tModLoader's own install dir, from
//   which it finds <library>/steamapps/workshop (workshop mods), including in
//   the Host & Play server process.
Game.appid = 1281930;
Game.name = "tModLoader";
Game.launchCommand = "dotnet/dotnet tModLoader.dll";
Game.mirrorCopyInstead = ["Libraries/steamworks.net.anycpu"];
Game.maxPlayers = 4;
Game.launchMessage =
  "To play together: one player chooses Multiplayer > Host & Play. Once in " +
  "the world, they open the menu (Esc) and choose \"Invite Friends\": the " +
  "others get the character select and join. Or the others choose " +
  "Multiplayer > Join via IP, address 127.0.0.1.";
Game.runtime = "none";            // Steam runs it bare (no runtime in appinfo)
Game.isolation = "none";          // the handler separates instances itself
Game.profileSaves = true;         // ...per player profile (ctx.saveRoot)
Game.borderless = false;
Game.goldberg = true;
Game.goldbergDisableLan = false;  // instances must see each other for invites
Game.goldbergInvites = true;
// Pads: Steam Input. FNA's SDL3 reads each instance's Steam virtual pad (the
// hook allows SDL to open it and blocks the physical one), and the session
// forces this game's Steam Input layout (steam_input.force_layout), so Steam's
// per-game remapping applies. tModLoader never asks for ISteamInput itself.
Game.steamInputFilter = false;
// FNA3D's default Vulkan path doesn't present under gamescope (GAMES.md).
Game.extraEnv = {FNA3D_FORCE_DRIVER: "OpenGL"};

const APPID = 1281930;
const SLUG = "tmodloader";

Janus.addOption("saves", {
  label: "Saves",
  description: "firstReal: the Steam account's player plays on your real " +
               "characters and worlds (local and Steam Cloud); every other " +
               "profile has its own saves, empty at first. isolated: every " +
               "profile has its own saves, empty at first. copy: every profile " +
               "gets a copy of your real saves the first time it plays, then " +
               "keeps its own (progress stays in Janus; your real saves are " +
               "never written).",
  type: "choice", choices: ["firstReal", "isolated", "copy"], default: "firstReal"});
Janus.addOption("terrariaDir", {
  label: "Terraria install folder",
  description: "tModLoader loads Terraria's own assets from it. Empty = the " +
               "Terraria install Steam knows about (any Steam library).",
  type: "string", default: ""});

function realSaves(ctx) {
  return `${ctx.home}/.local/share/Terraria/tModLoader`;
}

function terrariaDir(ctx) {
  const opt = String(ctx.options.terrariaDir || "").trim();
  return opt || Janus.appInstallDir(105600) || ctx.installDir.replace(/\/[^/]+\/?$/, "/Terraria");
}

// SteamClientLaunch=1 would make tModLoader start its TerrariaSteamClient
// helper (ownership/playtime check through the REAL Steam client).
Game.buildEnvironment = function (ctx, env) {
  delete env.SteamClientLaunch;
};

// A gbe_fork app dir (GseAppPath): identity, LAN on, own saves folder.
// Local steam_settings beat the global "GSE Saves/settings" (merge_ini keeps
// the first value), and with saves_folder_name set the global dir isn't the
// one in ~/.local/share/GSE Saves anyway.
function writeGseSettings(ctx, dir, name, steamid, gseFolder) {
  const s = `${dir}/steam_settings`;
  Janus.writeFile(`${s}/steam_appid.txt`, `${APPID}\n`);
  Janus.writeFile(`${s}/configs.user.ini`,
    "[user::general]\n" +
    `account_name=${name}\n` +
    `account_steamid=${steamid}\n` +
    "language=english\n" +
    "[user::saves]\n" +
    `saves_folder_name=${gseFolder}\n`);
  Janus.writeFile(`${s}/configs.main.ini`,
    "[main::connectivity]\n" +
    "disable_lan_only=0\n");
  // gbe_fork resolves [app::paths] relative to GseAppPath.
  let ini = "[app::paths]\n";
  for (const [appid, path] of [[105600, terrariaDir(ctx)], [APPID, ctx.installDir]]) {
    const r = Janus.run(["realpath", "-m", "--relative-to", dir, path]);
    ini += `${appid}=${r.stdout.trim()}\n`;
  }
  Janus.writeFile(`${s}/configs.app.ini`, ini);
}

// The Steam Cloud folder of the most recently used Steam account that has one
// for this app (cloud characters/worlds live there).
function steamCloudDir(ctx) {
  const dirs = Janus.glob(`${ctx.home}/.local/share/Steam/userdata/*/${APPID}/remote`);
  if (dirs.length <= 1) return dirs[0] || null;
  const r = Janus.run(["stat", "-c", "%Y %n"].concat(dirs));
  return r.stdout.trim().split("\n").map(l => [Number(l.split(" ")[0]), l.slice(l.indexOf(" ") + 1)])
    .sort((a, b) => b[0] - a[0])[0][1];
}

function isLink(p) {
  return Janus.run(["test", "-L", p]).code === 0;
}

// Returns {dir: local save dir to pass (null = the game's default, i.e. the
// real one), gseFolder: Goldberg saves_folder_name, relative to ~/.local/share}.
// Goldberg's emulated Steam Cloud is <gseFolder>/<appid>/remote. Saves belong
// to the player's profile (ctx.saveRoot), not to the instance number.
function setupSaves(ctx) {
  const data = `${ctx.home}/.local/share/janus/${SLUG}`;
  const mode = ctx.options.saves;
  const cloud = steamCloudDir(ctx);
  const rel = (p) => p.slice(`${ctx.home}/.local/share/`.length);

  // firstReal, the Steam account's player: the real saves.
  if (mode === "firstReal" && ctx.profile.owner) {
    const gse = `${data}/real/gse`;
    const remote = `${gse}/${APPID}/remote`;
    // The real cloud folder, live. Steam syncs the changes the next time the
    // game is started through Steam, as after playing offline.
    if (!Janus.exists(remote) && !isLink(remote)) {
      Janus.mkdir(`${gse}/${APPID}`);
      if (cloud) Janus.symlink(cloud, remote);
      else Janus.mkdir(remote);
    }
    return {dir: null, gseFolder: rel(gse)};
  }

  // Everyone else: own saves, kept across sessions. copy seeds them from the
  // real saves the first time; isolated (and firstReal's other profiles)
  // start empty. Kept apart, so switching modes never mixes them.
  const base = `${ctx.saveRoot}/${mode === "copy" ? "copy" : "isolated"}`;
  const dir = `${base}/saves`;
  const remote = `${base}/gse/${APPID}/remote`;
  if (!Janus.exists(dir)) {
    Janus.mkdir(dir);
    const real = realSaves(ctx);
    if (mode === "copy" && Janus.exists(real)) {
      for (const p of Janus.glob(`${real}/*`)) {
        const name = p.slice(p.lastIndexOf("/") + 1);
        if (name !== "Mods") Janus.copy(p, `${dir}/${name}`);  // mods stay shared, see play
      }
      Janus.log(`${ctx.profile.name}: copied ${real} -> ${dir}`);
    }
  }
  if (!Janus.exists(remote)) {
    if (mode === "copy" && cloud) {
      Janus.copy(cloud, remote);
      Janus.log(`${ctx.profile.name}: copied Steam Cloud ${cloud} -> ${remote}`);
    } else Janus.mkdir(remote);
  }
  return {dir, gseFolder: rel(`${base}/gse`)};
}

// LastLaunchedVersion written by the game in use (tModLoader 2026.8 = Terraria 1.4.4.9). Only a
// fallback: see lastLaunchedVersion().
const KNOWN_RELEASE = 279;

// The game runs per-version settings upgrades for every release newer than
// config.json's LastLaunchedVersion (Main.LoadSettings), and with the key
// missing (0) one of them resets the volumes to 1.0, so a fresh instance's
// music setting never stuck. A config written by the game has the current
// value: take it from the real config or any profile's, else KNOWN_RELEASE.
function lastLaunchedVersion(ctx) {
  const cfgs = [`${realSaves(ctx)}/config.json`].concat(
    Janus.glob(`${ctx.profilesDir}/*/${APPID}/*/saves/config.json`));
  let v = 0;
  for (const p of cfgs) {
    try { v = Math.max(v, Number(Janus.readJson(p).LastLaunchedVersion) || 0); }
    catch (e) { /* missing or unreadable */ }
  }
  return v || KNOWN_RELEASE;
}

// config.json, every launch: windowed at the tile size, and music off in
// every instance but the first (one soundtrack, not N overlapping copies).
// The real config.json (firstReal, the Steam account's player) is snapshot first and
// restored when the session ends.
function writeConfig(dir, ctx, isReal) {
  const set = {};
  if (ctx.width && ctx.height)
    Object.assign(set, {DisplayWidth: ctx.width, DisplayHeight: ctx.height,
                        Fullscreen: false, WindowMaximized: false, WindowBorderless: true});
  const cfg = `${dir}/config.json`;
  // A profile's saves follow it to any player number: the volume muted as
  // P2+ is put back when it plays as P1 (the real config is restored from
  // its backup anyway).
  const muted = `${dir}/janus-music-volume.txt`;
  if (ctx.instance === 1 && Janus.exists(muted)) {
    const v = Number(Janus.readFile(muted));
    set.VolumeMusic = isNaN(v) ? 1 : v;
    Janus.remove(muted, {force: true});
  }
  if (!Object.keys(set).length && ctx.instance === 1) return;
  if (isReal) Janus.backup(cfg);
  let c = {};
  if (Janus.exists(cfg)) {
    try { c = Janus.readJson(cfg); }
    catch (e) { Janus.log(`config.json unreadable, left alone: ${e.message}`); return; }
  }
  if (ctx.instance > 1) {
    if (!isReal && c.VolumeMusic !== 0 && !Janus.exists(muted))
      Janus.writeFile(muted, String(c.VolumeMusic == null ? 1 : c.VolumeMusic));
    set.VolumeMusic = 0;
  }
  if (!c.LastLaunchedVersion) set.LastLaunchedVersion = lastLaunchedVersion(ctx);
  Janus.writeJson(cfg, Object.assign(c, set));
}

// The profile menus' line for each profile: its characters and world count,
// from the saves setupSaves() gives it (read-only: only globs).
Game.profileInfo = function (ctx) {
  const mode = ctx.options.saves;
  let dirs;
  if (mode === "firstReal" && ctx.profile.owner) {
    const cloud = Janus.glob(`${ctx.home}/.local/share/Steam/userdata/*/${APPID}/remote`);
    dirs = [realSaves(ctx)].concat(cloud);
  } else {
    const base = `${ctx.saveRoot}/${mode === "copy" ? "copy" : "isolated"}`;
    dirs = [`${base}/saves`, `${base}/gse/${APPID}/remote`];
  }
  const names = (kind, ext) => {
    const out = new Set();
    for (const d of dirs)
      for (const sub of [kind, kind.toLowerCase()])
        for (const p of Janus.glob(`${d}/${sub}/*.${ext}`))
          out.add(p.slice(p.lastIndexOf("/") + 1, -ext.length - 1).replace(/_/g, " "));
    return [...out].sort();
  };
  const chars = names("Players", "plr"), worlds = names("Worlds", "wld");
  if (!chars.length && !worlds.length) return "No characters yet";
  const who = chars.length > 3 ? `${chars.slice(0, 3).join(", ")} +${chars.length - 3}`
                               : chars.join(", ") || "no characters";
  return `${who} · ${worlds.length} world${worlds.length === 1 ? "" : "s"}`;
};

Game.play = function (ctx) {
  if (!Janus.exists(`${terrariaDir(ctx)}/Content/Images`))
    Janus.error(`tModLoader needs Terraria installed: no Terraria assets in ` +
                `${terrariaDir(ctx)}. Install Terraria in Steam (or set ` +
                `"Terraria install folder").`);
  const n = ctx.instance;
  const saves = setupSaves(ctx);
  const name = ctx.profile.name;
  // The server gets its own Steam ID, derived from the player's. SteamID64s
  // exceed 2^53, hence BigInt.
  const serverId = (BigInt(ctx.profile.steamId) + 100n).toString();

  // Client and Host & Play server identities. gbe_fork reads steam_settings/
  // under GseAppPath; the vendored lib is loaded through the hook's dlopen
  // redirect, so "next to the lib" would be shared by every instance.
  const gse = `${ctx.gameDir}/_janus_gbe_logs`;   // = Janus's debug-build GseAppPath
  const srv = `${ctx.gameDir}/_janus_gbe_server`;
  writeGseSettings(ctx, gse, name, ctx.profile.steamId, saves.gseFolder);
  writeGseSettings(ctx, srv, `${name} (host)`, serverId, saves.gseFolder);
  for (const f of ["auto_send_invite.txt", "auto_accept_invite.txt"])
    Janus.writeFile(`${gse}/steam_settings/${f}`, "");
  ctx.env.GseAppPath = gse;
  ctx.env.JANUS_ARGV_ENV_MATCH = "-autoshutdown";
  ctx.env.JANUS_ARGV_ENV = "GseAppPath=" + srv;
  // gbe_fork only invites friends whose lobby differs from the inviter's;
  // this client is in NO lobby (the server owns it), and neither are the
  // other instances, so gbe skips them all. The hook sends the invites itself.
  ctx.env.JANUS_GOLDBERG_INVITE_FIX = "1";

  if (saves.dir) {
    // Own saves, same mods as playing normally (and as the host's server,
    // which gets -modpath from the client).
    const mods = `${realSaves(ctx)}/Mods`;
    Janus.mkdir(mods);
    ctx.gameArgs.push("-tmlsavedirectory", saves.dir, "-modpath", mods);
  }
  // EnvironmentFix.sh: bundled native libs (libSDL2.so link: onGameResolved).
  ctx.env.LD_LIBRARY_PATH = `${ctx.gameDir}/Libraries/Native/Linux`;
  ctx.env.DOTNET_ROLL_FORWARD = "Disable";
  writeConfig(saves.dir || realSaves(ctx), ctx, saves.dir == null);
  Janus.log(`instance ${n} (${name}): host with Multiplayer > Host & Play, then Esc > ` +
            `"Invite Friends"; or join with Multiplayer > Join via IP 127.0.0.1`);
};

// The .NET runtime and the libSDL2.so link live in the real install, exactly
// where tModLoader's launch scripts put them (they do this on every launch).
// Before the mirror is built, so it links them in.
Game.onGameResolved = function (info) {
  const dir = info.installDir;
  const lib = `${dir}/Libraries/Native/Linux`;
  if (!Janus.exists(`${lib}/libSDL2.so`))
    Janus.symlink(`${lib}/libSDL2-2.0.so.0`, `${lib}/libSDL2.so`);
  if (Janus.exists(`${dir}/dotnet/dotnet`)) return;
  const rc = Janus.readFile(`${dir}/tModLoader.runtimeconfig.json`);
  const m = /"version":\s*"([^"]+)"/.exec(rc);
  if (!m) Janus.error("tModLoader: can't read the .NET version from tModLoader.runtimeconfig.json.");
  Janus.log(`installing .NET ${m[1]} into ${dir}/dotnet (first start; takes a minute)`);
  const r = Janus.run(["bash", "./InstallDotNet.sh"], {
    cwd: `${dir}/LaunchUtils`, timeout: 900,
    env: {dotnet_dir: `${dir}/dotnet`, dotnet_version: m[1], LD_PRELOAD: ""}});
  if (!Janus.exists(`${dir}/dotnet/dotnet`))
    Janus.error(`tModLoader: installing .NET ${m[1]} failed:\n` +
                (r.stdout + r.stderr).slice(-800));
};

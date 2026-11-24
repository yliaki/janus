// Deep Rock Galactic — Unreal Engine 4.27, Proton (Windows-only build).
// Multiplayer is stock OnlineSubsystemSteam: a Steam lobby session + Steam P2P
// (SteamNetworking006 / SteamNetworkingSockets009), so gbe_fork covers it on
// one host. Same-host co-op via Goldberg IN-GAME INVITES, as in RV There Yet?
// (the other UE / OSS Steam handler, validated): all instances launch in
// parallel; "Invite friends" in any instance -> OSS Steam ShowInviteUI ->
// ActivateGameOverlayInviteDialog(lobby), which gbe_fork turns into a real
// lobby invite to every LAN friend (= the other instances) when
// auto_send_invite.txt exists. The receiver gets GameLobbyJoinRequested and
// OSS Steam joins the host's session. Needs LAN discovery ON.
//
// Per-instance Saved/ via -UserDir=: DRG keeps saves AND config in its
// install dir (FSD/Saved), which the Goldberg mirror would symlink straight
// back into the real install, so every instance would share one
// GameUserSettings.ini (resolution) and write saves into the real game. UE's
// -UserDir= (present in FSD-Win64-Shipping.exe) moves ProjectSavedDir into
// the instance's Wine prefix instead; FSD/Saved is excluded from the mirror so
// nothing can reach the real install. See NOTES.md "Deep Rock Galactic (548430)".
Game.appid = 548430;
Game.name = "Deep Rock Galactic";
Game.maxPlayers = 4;
Game.launchMessage =
  "To play together: one player chooses \"Invite friends\" in the menu on the " +
  "Space Rig; the others join automatically.";
Game.isolation = "full";
// The Shipping exe directly: FSD.exe is UE's bootstrap stub that only
// re-launches it (one more process between gamescope and the game).
Game.exeName = "FSD-Win64-Shipping.exe";
// -dx11: the user config defaults to D3D12 (bUseD3D12InGame=True), which
// rubber-bands under gamescope on NVIDIA (RV There Yet?, NOTES.md) and
// compiles PSOs at every start. -nohmd: skip SteamVR/Oculus/OpenXR probing
// (DRG ships all three plugins). -nosplash: no splash bitmap.
Game.gameArgs = "-dx11 -nohmd -nosplash -fullscreen";
// DRG never requests SteamInput/SteamController (only Apps/Friends/
// MatchMaking/Networking/User/Utils/RemoteStorage/UserStats); pads come in
// through XInput under Wine. The filter would only block physical evdev.
Game.steamInputFilter = false;
Game.borderless = false;
Game.goldberg = true;
Game.goldbergDisableLan = false;  // invites need LAN discovery
Game.goldbergDebug = true;        // logs invites + lobby IDs (diagnostics)
Game.goldbergAutoJoin = false;    // invites replace wait-for-host + connect_lobby
Game.goldbergInvites = true;      // "Invite friends" pulls in running instances
Game.mirrorExclude = ["FSD/Saved"];
Game.unrealUiScale = true;        // "UI scale" option: Engine.ini ApplicationScale (unreal.py)
Game.unrealUiFit = true;          // ... fitted per screen via UE4SS (unreal.py); 1920x540,
Game.resizeGame = "unreal";       // re-flow: the UE4SS mod runs r.SetRes to its new tile
                                  // max 2.0 (2026-10-25): start 1.2, rig HUD 1.9, escape menu 1.15

const INTRO_MOVIES = ["FSD/Content/Movies/DRG_LogoIntro 720p30.mp4",
                      "FSD/Content/Movies/DRG_LogoIntro_Lower_Sound_720p30.mp4"];

Janus.addOption("skipIntro", {
  label: "Skip intro logo movie",
  description: "Leave the logo intro movies out of the Goldberg mirror (the " +
               "known DRG trick of deleting them, without touching the real " +
               "install).",
  type: "bool", default: true});
Janus.addOption("disableModding", {
  label: "Disable modding (-disablemodding)",
  description: "DRG's Steam \"Safemode\" launch option: skips mod.io start-up. " +
               "mod.io logs in with a Steam ticket, which Goldberg can't " +
               "provide, so mods don't load under Janus anyway.",
  type: "bool", default: true});
Janus.addOption("importSave", {
  label: "Copy my save into",
  description: "Copy your real save (dwarves, levels, unlocks) into a " +
               "player's profile the first time it plays: \"all\" = every " +
               "profile, \"first\" = only the Steam account's player. It is a " +
               "copy: progress made in Janus stays in Janus and your real save " +
               "is never written. \"none\" = fresh level-1 dwarves.",
  type: "choice", choices: ["all", "first", "none"], default: "all"});
Janus.addOption("importConfig", {
  label: "Copy my settings (graphics, keybinds) on first start",
  type: "bool", default: true});
Janus.addOption("unlockAllDlc", {
  label: "Unlock all DLC (Goldberg)",
  description: "Have Goldberg report every DLC as owned. Off = report none " +
               "(gbe_fork's own default, with no config, is all unlocked).",
  type: "bool", default: false});

if (Janus.getOption("skipIntro")) Game.mirrorExclude = Game.mirrorExclude.concat(INTRO_MOVIES);

// The player's UE user dir, as Windows path and as host path. Lives in the
// profile's Wine prefix (persistent across launches, isolation-aware); the
// profile id keeps players apart even if they share one prefix.
function userDir(ctx) {
  const rel = `users/steamuser/Janus/DRG-${ctx.profile.id}/`;
  return {win: "C:/" + rel, host: `${ctx.winePrefix}/drive_c/${rel}`};
}

// Newest <17-digit steamid>_Player.sav of the real install (the Steam account).
function realSave(ctx) {
  const saves = Janus.glob(`${ctx.installDir}/FSD/Saved/SaveGames/*_Player.sav`)
    .filter(p => /\/\d{17}_Player\.sav$/.test(p));
  if (saves.length <= 1) return saves[0] || null;
  const r = Janus.run(["stat", "-c", "%Y %n"].concat(saves));
  return r.stdout.trim().split("\n").map(l => l.split(" "))
    .sort((a, b) => Number(b[0]) - Number(a[0]))[0].slice(1).join(" ");
}

function seed(ctx, dir) {
  const saveDir = dir.host + "Saved/SaveGames/";
  const cfgDir = dir.host + "Saved/Config/WindowsNoEditor/";
  const mode = ctx.options.importSave;
  // DRG names saves <steamid>_Player.sav; Goldberg reports the profile's ID.
  const dst = `${saveDir}${ctx.profile.steamId}_Player.sav`;
  if (mode === "all" || (mode === "first" && ctx.profile.owner)) {
    if (!Janus.exists(dst)) {
      const src = realSave(ctx);
      if (src) { Janus.copy(src, dst); Janus.log(`save ${src} -> ${dst}`); }
      else Janus.log("no real save found; starting fresh");
    }
  }
  const realCfg = `${ctx.installDir}/FSD/Saved/Config/WindowsNoEditor`;
  if (ctx.options.importConfig && !Janus.exists(cfgDir) && Janus.exists(realCfg)) {
    Janus.copy(realCfg, cfgDir);
    Janus.log(`config ${realCfg} -> ${cfgDir}`);
  }
  return cfgDir + "GameUserSettings.ini";
}

// gbe_fork reads steam_settings/ next to its steam_api64.dll, or under
// GseAppPath when set (Janus pins it to <mirror>/_janus_gbe_logs with the
// debug build) — write both, like write_goldberg_invite_settings().
Game.onMirrorReady = function (ctx) {
  const ini = `[app::dlcs]\nunlock_all=${ctx.options.unlockAllDlc ? 1 : 0}\n`;
  for (const d of ["Engine/Binaries/ThirdParty/Steamworks/Steamv151/Win64/steam_settings",
                   "_janus_gbe_logs/steam_settings"])
    Janus.writeFile(`${d}/configs.app.ini`, ini);
  Janus.log(`Goldberg DLCs: ${ctx.options.unlockAllDlc ? "all unlocked" : "none"}`);
};

Game.play = function (ctx) {
  if (!ctx.winePrefix) Janus.error("Deep Rock Galactic: no Wine prefix (expected a Proton launch).");
  const dir = userDir(ctx);
  Janus.mkdir(dir.host);
  const gus = seed(ctx, dir);
  ctx.gameArgs.push("-UserDir=" + dir.win);
  if (ctx.options.disableModding) ctx.gameArgs.push("-disablemodding");
  if (ctx.verbose) ctx.gameArgs.push("-forcelogflush");
  // Copied settings are the user's; force what split-screen needs.
  if (Janus.exists(gus)) Janus.iniSet(gus, "D3DRHIPreference", "bUseD3D12InGame", "False");
  if (ctx.width && ctx.height) {
    ctx.gameArgs.push("-ResX=" + ctx.width, "-ResY=" + ctx.height);
    if (Janus.exists(gus)) {
      const S = "/Script/FSD.FSDGameUserSettings";
      for (const [k, v] of [["ResolutionSizeX", ctx.width], ["ResolutionSizeY", ctx.height],
                            ["LastUserConfirmedResolutionSizeX", ctx.width],
                            ["LastUserConfirmedResolutionSizeY", ctx.height],
                            ["DesiredScreenWidth", ctx.width], ["DesiredScreenHeight", ctx.height],
                            ["LastUserConfirmedDesiredScreenWidth", ctx.width],
                            ["LastUserConfirmedDesiredScreenHeight", ctx.height]])
        Janus.iniSet(gus, S, k, String(v));
    }
  }
};

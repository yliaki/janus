// Valheim — Unity (Mono), native Linux build. Multiplayer is Steam
// networking sockets (P2P listen socket + a Steam lobby that advertises the
// world) or PlayFab for crossplay. The host checks every joiner's Steam
// session ticket (ZNet.RPC_PeerInfo -> BeginAuthSession), which works
// Goldberg-to-Goldberg, so every instance runs on Goldberg with its own
// Steam ID; the host world is then listed in the others' server browser.
// PlayFab (crossplay) logs in with a real Steam ticket and can't work under
// Goldberg: the host must leave "Crossplay" off.
//
// - Saves: characters_local/ and worlds_local/ in ~/.config/unity3d/IronGate/
//   Valheim of each player's own home (isolation full); the "importSave"
//   option copies the real ones in once.
// - The executable is a real copy in each mirror (Unity resolves
//   /proc/self/exe to find its data dir; see the Silksong handler).
// - Intro cinematic (several minutes, A doesn't skip): PlayerPrefs
//   SkipIntroCinematic = 1 (FejdStartup.TryPlayIntroCinematic), written into
//   Unity's prefs XML in each player's home before the start.
// - UI size: GuiScaler = min(W/1920, H/1080) * GuiScale (pref, slider up to
//   115%), so any half-screen tile draws the GUI at half size; not changed.
// - The local dedicated server (app 896660) is not used: it would run on
//   real Steam and reject the Goldberg players' tickets.
Game.appid = 892970;
// Re-flow (dynamic players): Unity follows a resized window (windowed, play()).
Game.resizeGame = "window";
Game.name = "Valheim";
Game.maxPlayers = 10;
Game.isolation = "full";
Game.borderless = false;
Game.steamInputFilter = false;
Game.goldberg = true;
Game.goldbergDisableLan = false;   // the lobby/server list between instances
Game.goldbergInvites = true;
Game.oskEnabled = true;            // character and world names
Game.mirrorCopyInstead = ["valheim.x86_64"];
Game.mirrorExclude = ["unity.lock"];
Game.launchMessage =
  "To play together: one player picks a character, then a world, ticks " +
  "\"Start server\" (leave \"Crossplay\" OFF) and starts. The others pick " +
  "a character, open Join Game, and choose the host's world on the " +
  "Friends tab (or Community).";

Janus.addOption("importSave", {
  label: "Copy my characters and worlds into",
  description: "Copy your real Valheim characters and worlds into a " +
               "player's profile the first time it plays: \"all\" = every " +
               "profile, \"first\" = only the Steam account's player. It is " +
               "a copy: your real saves are never written. \"none\" = fresh.",
  type: "choice", choices: ["all", "first", "none"], default: "first"});

const DATA = ".config/unity3d/IronGate/Valheim";

function seedSave(ctx) {
  const mode = ctx.options.importSave;
  if (!(mode === "all" || (mode === "first" && ctx.profile.owner))) return;
  const dst = `${ctx.home}/${DATA}`;
  const real = Janus.glob(`~/${DATA}`)[0];
  if (!real || real === dst) return;
  for (const sub of ["characters_local", "worlds_local", "characters", "worlds"]) {
    if (!Janus.exists(`${real}/${sub}`) || Janus.exists(`${dst}/${sub}`)) continue;
    Janus.copy(`${real}/${sub}`, `${dst}/${sub}`);
    Janus.log(`copied ${real}/${sub} -> ${dst}/${sub}`);
  }
}

// Unity's Linux PlayerPrefs file (XML): set some int prefs. The player
// reads ~/.config/unity3d/unknown/unknown/prefs in these launches (seen
// 2026-11-01, also 7 Days to Die), so both files are written.
function setPrefs(ctx, prefs) {
  for (const f of [`${ctx.home}/${DATA}/prefs`, `${ctx.home}/.config/unity3d/unknown/unknown/prefs`])
    setPrefsFile(f, prefs);
}
function setPrefsFile(f, prefs) {
  let xml = Janus.exists(f) ? Janus.readFile(f)
    : '<unity_prefs version_major="1" version_minor="1">\n</unity_prefs>';
  for (const [k, v] of Object.entries(prefs)) {
    const line = `\t<pref name="${k}" type="int">${v}</pref>`;
    const re = new RegExp(`\\t?<pref name="${k}" type="[^"]*">[^<]*</pref>`);
    xml = re.test(xml) ? xml.replace(re, line) : xml.replace("</unity_prefs>", line + "\n</unity_prefs>");
  }
  Janus.writeFile(f, xml);
}

Game.buildCommand = function (ctx) {
  return [`${ctx.gameDir}/valheim.x86_64`];
};

Game.play = function (ctx) {
  seedSave(ctx);
  setPrefs(ctx, {SkipIntroCinematic: 1});
  ctx.env.SteamAppId = String(ctx.appid);
  if (ctx.width && ctx.height)
    ctx.gameArgs.push("-screen-fullscreen", "0",
                      "-screen-width", String(ctx.width),
                      "-screen-height", String(ctx.height));
};

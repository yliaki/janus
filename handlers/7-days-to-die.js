// 7 Days to Die — Unity (Mono), native Linux build. Multiplayer: the host
// runs the game server in-process (LiteNetLib UDP on 26900, or Steam
// networking); clients join by IP or through Steam lobbies/invites. The
// server validates Steam session tickets (BeginAuthSession), which works
// Goldberg-to-Goldberg, so every instance runs on Goldberg with its own
// Steam ID.
//
// - EasyAntiCheat: 7DaysToDie.sh starts 7dLauncher, which starts the game
//   under EAC (no EAC on Proton/Goldberg). The handler starts the Unity
//   player directly with "-noeac" (read in AntiCheatCommon.Init); EAC-off
//   clients can join EAC-off hosts, which all instances are.
// - Crossplay (EOS): platform.cfg ships "crossplatform=EOS"; EOS needs real
//   accounts. Each mirror gets its own platform.cfg with crossplatform
//   empty and serverplatforms=Steam (as the Nucleus Co-op handler does),
//   plus -AllowCrossplay=false.
// - The executable is a real copy in each mirror: Unity resolves
//   /proc/self/exe to find its data, so a symlink would lead it back to the
//   Steam install (and its platform.cfg); see the Silksong handler.
// - Saves/prefs: ~/.local/share/7DaysToDie and ~/.config/unity3d/The Fun
//   Pimps/7 Days To Die in each player's own home (isolation full).
// - Validated 2026-11-01 (fake pads, 2 tiles): host Continue Game -> Start,
//   client Join a Game -> Friends filter -> the host's server -> Connect,
//   Goldberg-to-Goldberg auth passes, both in the same world. The default
//   browser view lists nothing (region filter); the Friends icon shows it.
// - The game sets Application.runInBackground: no pause on focus loss.
// - Discord: the game's Discord SDK starts at game start, registers an
//   invite URL handler (xdg-mime) and "Logging in with Discord user" opens
//   a browser chooser over the tiles (KDE "Choose Application ... https",
//   2026-11-01). PlayerPrefs DiscordDisabled=1 (+ DiscordFirstTimeInfoShown,
//   the "Discord integration" popup) turns it off; OptionsCrossplay=0 too.
//   The prefs file is ~/.config/unity3d/unknown/unknown/prefs here (Unity
//   names the prefs dir before it reads app.info?); both paths are written.
Game.appid = 251570;
Game.name = "7 Days to Die";
Game.maxPlayers = 8;
Game.isolation = "full";
Game.borderless = false;
Game.steamInputFilter = false;
Game.goldberg = true;
Game.goldbergDisableLan = false;   // LAN server list + invites between instances
Game.goldbergInvites = true;
Game.oskEnabled = true;            // world names, IP field
Game.mirrorCopyInstead = ["7DaysToDie.x86_64", "platform.cfg"];
Game.mirrorExclude = ["EasyAntiCheat", "7DaysToDie_EAC", "unity.lock"];
Game.staggerSeconds = 8;           // heavy start-up (~several GB each)
Game.launchMessage =
  "Accept the EULA once per player (scroll down, Start = Accept).\n\n" +
  "To play together: player 1 chooses Play Game, New Game (or Continue " +
  "Game) and Start, then Spawn (D-pad up, A). The others choose Play " +
  "Game, RB to Join a Game, move up to the icons above the list and press " +
  "A on the Friends icon: the host's game appears; select it and press " +
  "Start (Connect), then Spawn near a friend.";

Game.onMirrorReady = function (ctx) {
  Janus.writeLines(`${ctx.gameDir}/platform.cfg`,
                   ["platform=Steam", "crossplatform=", "serverplatforms=Steam,LAN,"]);
};

Game.buildCommand = function (ctx) {
  return [`${ctx.gameDir}/7DaysToDie.x86_64`];
};

// Unity's Linux PlayerPrefs file (XML): set some int prefs.
// ServerEnabled: a hosted game is an offline server otherwise ("NET: Starting
// offline server"), invisible to the others; crossplay needs EOS.
const PREFS = {DiscordDisabled: 1, DiscordFirstTimeInfoShown: 1, OptionsCrossplay: 0,
               ServerEnabled: 1, ServerAllowCrossplay: 0};
function setPrefs(ctx) {
  for (const dir of ["unknown/unknown", "The Fun Pimps/7 Days To Die"]) {
    const f = `${ctx.home}/.config/unity3d/${dir}/prefs`;
    let xml = Janus.exists(f) ? Janus.readFile(f)
      : '<unity_prefs version_major="1" version_minor="1">\n</unity_prefs>';
    for (const [k, v] of Object.entries(PREFS)) {
      const line = `\t<pref name="${k}" type="int">${v}</pref>`;
      const re = new RegExp(`\\t?<pref name="${k}" type="[^"]*">[^<]*</pref>`);
      xml = re.test(xml) ? xml.replace(re, line) : xml.replace("</unity_prefs>", line + "\n</unity_prefs>");
    }
    Janus.writeFile(f, xml);
  }
}

Game.play = function (ctx) {
  setPrefs(ctx);
  ctx.env.LD_LIBRARY_PATH = ctx.gameDir;   // as the game's own scripts do
  ctx.gameArgs.push("-noeac", "-AllowCrossplay=false", "-SkipNewsScreen=true");
  if (ctx.width && ctx.height)
    ctx.gameArgs.push("-screen-fullscreen", "0",
                      "-screen-width", String(ctx.width),
                      "-screen-height", String(ctx.height));
};

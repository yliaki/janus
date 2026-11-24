// Mycopunk — Unity 6 (Mono), Proton. Unity Netcode over Facepunch.Steamworks
// (Steam lobbies + Steam networking sockets), so Goldberg covers it. The game
// has no overlay-invite button (no ActivateGameOverlayInviteDialog call): the
// others join from the lobby list (RequestLobbyList), which under Goldberg
// lists the LAN instances' lobbies, so LAN discovery must stay on. Validated
// 2026-10-25 with fake pads, 2 tiles: every instance starts its own ship in
// the H.U.B.; Start -> CREW tab (LT/RT) -> Quick Join / Join a Crew -> the
// host's "<name>'s Ship" -> A, then pick character + weapons. Works with the
// host's lobby left on Friends only (Goldberg instances are friends).
// Saves: AppData/LocalLow/Pigeons at Play/Mycopunk/Profiles/default/
// (PlayerData.saxon, not keyed by Steam ID) in each player's own prefix;
// "importSave" copies the real prefix's once.
//
// Modded joiners: the game rejects a joiner that loads any BepInEx plugin
// ("Host does not allow modded clients to join") unless the host's lobby
// option "Allow modded joiners" is on, and PlayerOptions.InitializeConfig()
// resets it to off on every start. The UI scale option loads BepInEx in every
// instance, so payload/JanusMycoModJoin.dll (source + build.sh in plugin/)
// turns it on after the config loads (Harmony postfix). Seen 2026-10-25 with
// UI scale 2.0: the join failed with that message.
Game.appid = 3247750;
Game.name = "Mycopunk";
Game.maxPlayers = 4;
Game.launchMessage =
  "To play together: everyone picks a character and lands in the H.U.B. " +
  "Then the joining players press Start, go to the CREW tab (LT), choose " +
  "the host's ship under Join a Crew and press A.";
Game.isolation = "full";
Game.borderless = false;
Game.steamInputFilter = false;    // the game never calls ISteamInput
Game.goldberg = true;
Game.goldbergDisableLan = false;  // the lobby browser finds lobbies over LAN
Game.goldbergInvites = true;      // Steam-friends joins (OnGameLobbyJoinRequested)
Game.oskEnabled = true;
Game.bepinexPlugins = ["JanusMycoModJoin.dll"];  // "Allow modded joiners" on
Game.unityUiScale = true;
// HUD pieces that move to/over the screen edge by design and must not shrink
// the HUD: ping markers pinned to the edge, the dialogue box sliding in, the
// objective bar's fade gradient (2026-10-25: the HUD scale jumped 1.15-2.0).
Game.unityUiScaleIgnore = ["Ping(Clone)", "DialogueBox(Clone)", "BKGFade"];         // "UI scale" option (BepInEx, NOTES.md)

Janus.addOption("importSave", {
  label: "Copy my save into",
  description: "Copy your real save (progress, unlocks) into a player's " +
               "profile the first time it plays: \"all\" = every profile, " +
               "\"first\" = only the Steam account's player. It is a copy: " +
               "your real save is never written. \"none\" = fresh start.",
  type: "choice", choices: ["all", "first", "none"], default: "first"});

const SAVE_REL = "drive_c/users/steamuser/AppData/LocalLow/Pigeons at Play/Mycopunk/Profiles";

function seedSave(ctx) {
  const mode = ctx.options.importSave;
  if (!(mode === "all" || (mode === "first" && ctx.profile.owner))) return;
  if (!ctx.winePrefix) return;
  const dst = `${ctx.winePrefix}/${SAVE_REL}`;
  if (Janus.exists(`${dst}/default/PlayerData.saxon`)) return;
  // The real prefix: <library>/steamapps/compatdata/<appid>/pfx.
  const src = `${ctx.installDir}/../../compatdata/${ctx.appid}/pfx/${SAVE_REL}`;
  if (src === dst || !Janus.exists(`${src}/default/PlayerData.saxon`)) {
    Janus.log("no real save found; starting fresh");
    return;
  }
  Janus.copy(src, dst);
  Janus.log(`save ${src} -> ${dst}`);
}

Game.play = function (ctx) {
  seedSave(ctx);
  if (ctx.width && ctx.height) {
    ctx.gameArgs.push("-screen-fullscreen", "0",
                      "-screen-width", String(ctx.width),
                      "-screen-height", String(ctx.height));
  }
};

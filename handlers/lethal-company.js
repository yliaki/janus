// Lethal Company — Unity, Proton. Co-op via Goldberg IN-GAME INVITES: all
// instances launch at once; host in any instance and invite — the others join
// while running (validated 2026-10-07). Invites need LAN discovery ON. Caveat:
// with LAN on, an earlier session saw the two instances auto-join each other
// and deadlock the netcode handshake (CLAUDE.md "Goldberg LAN auto-discovery");
// not reproduced with the invite flow. If it recurs, set goldbergDisableLan =
// true and join via the in-game online lobby instead.
Game.appid = 1966720;
Game.name = "Lethal Company";
Game.maxPlayers = 4;
Game.launchMessage =
  "To play together: one player hosts a game, then chooses \"Invite " +
  "friends\" in the menu; the others join automatically.";
Game.isolation = "full";
Game.borderless = false;
Game.goldberg = true;
Game.goldbergDisableLan = false;  // invites need LAN discovery (see caveat above)
Game.goldbergInvites = true;      // in-game invite joins running instances (validated)
Game.oskEnabled = true;
Game.unityUiScale = true;         // "UI scale" option (BepInEx, NOTES.md)
Game.resizeGame = "unity";        // re-flow: the plugin resizes it to its new tile

Game.play = function (ctx) {
  if (ctx.width && ctx.height) {
    ctx.gameArgs.push("-screen-fullscreen", "0",
                      "-screen-width", String(ctx.width),
                      "-screen-height", String(ctx.height));
  }
};

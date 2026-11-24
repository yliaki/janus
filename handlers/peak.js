// PEAK — Unity, Proton. Validated two-pad 50/50 split on KWin Wayland
// (WAYLAND.md). Same-host co-op via Goldberg INVITES, joining RUNNING
// instances: all instances launch in parallel; clicking "Invite players" in
// ANY instance calls ActivateGameOverlayInviteDialog(lobby), which gbe_fork
// turns into a real InviteUserToLobby to every LAN friend (= the other
// instances) when auto_send_invite.txt is present. With no overlay, a received
// lobby invite always fires GameLobbyJoinRequested, which PEAK handles by
// joining. Game.goldbergInvites does the gbe_fork setup; needs LAN
// discovery ON (goldbergDisableLan = false). See NOTES.md.
Game.appid = 3527290;
Game.name = "PEAK";
Game.maxPlayers = 4;
Game.launchMessage =
  "To play together: one player chooses \"Invite players\" in their game; " +
  "the others are pulled into it automatically, nothing to type.";
Game.isolation = "full";
// NO Steam Input filter: PEAK never calls ISteamInput (its Assembly-CSharp
// has no Steam Input references, NOTES.md "Which games actually use the
// Steam Input API"), and under Goldberg no Steam Input reaches it anyway.
// The filter blocks physical-pad evdev, leaving only Steam's virtual pads,
// which Steam routes to the focused tile -> both pads drove instance 1,
// none drove instance 2 (2026-10-07). Plain evdev filtering is correct.
Game.steamInputFilter = false;
Game.goldberg = true;
Game.goldbergDisableLan = false;
Game.goldbergDebug = true;        // logs invites + lobby IDs (diagnostics)
Game.goldbergAutoJoin = false;    // invites replace wait-for-host + connect_lobby
Game.goldbergInvites = true;      // "Invite players" pulls in running instances
Game.oskEnabled = true;
Game.unityUiScale = true;         // "UI scale" option (BepInEx, NOTES.md)
Game.resizeGame = "unity";        // re-flow: the plugin resizes it to its new tile

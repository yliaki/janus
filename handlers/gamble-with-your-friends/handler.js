// Gamble With Your Friends — Unity 6 (Mono), Proton. Mirror networking over
// Steam P2P (FizzySteamworks) with Steam lobbies, so Goldberg covers it.
// Co-op via Goldberg IN-GAME INVITES: the game's "Invite friends" button
// (ActivateGameOverlayInviteDialog) joins the others on GameLobbyJoinRequested.
// That button is a mouse-only "+" icon, so the plugin below adds a big
// "INVITE FRIENDS" button above Host Game (the pad's first selection) and
// maps the pad's View button to the same call. Validated 2026-10-25 with fake pads, 2 tiles: View in
// the host's main menu pulled the other instance into "ILYAKI'S LOBBY"; Host
// Game -> Yes, I am -> slot took both into the game (both names in the HUD).
//
// Steam Input ON, with a Gamepad layout per controller. Steam lists the game
// as "Controllers Not Supported", so each pad type defaults to the community
// "Keyboard (WASD) and Mouse" layout (workshop 3717893984): Steam then makes
// no virtual pads and every pad drives the shared desktop pointer/keyboard
// (2026-10-25). Choosing a Gamepad layout (controller_<type>_gamepad_joystick)
// for the game makes Steam create virtual pads as for any other game. The
// choice is stored per physical controller (Steam Controller Configs/<account>/
// config/configset_<vid>-<pid>-….vdf / configset_NSP<mac>.vdf), so every pad
// needs it once; requireGamepadLayout checks it at launch. The game reads pads itself (Unity
// Input System via Windows.Gaming.Input), here its Steam virtual pad; the
// menus take the pad. (Earlier version: requireSteamInput = "disabled" and
// physical pads, which fails for pads whose kernel node is silent.)
// The start screen (OfflineSplashCoinFlip, the first
// scene, always shown, no argument skips it) asks "FEELING LUCKY?": NO
// continues; YES flips a coin and heads QUITS the game ("Uh oh, you lost
// sucka."). The plugin below skips it (calls its LoadNextScene, like NO).
//
// payload/JanusPadFocus.dll (source + build.sh in plugin/), a BepInEx plugin:
// Unity's Input System disables gamepads while the window is unfocused
// (backgroundBehavior ResetAndDisableNonBackgroundDevices), and only one
// gamescope tile has focus, so only that tile's pad worked. The plugin sets
// IgnoreFocus (validated 2026-10-25: both menus followed their pads while
// neither tile had focus). It also skips the start screen (see above),
// adds the INVITE FRIENDS button and binds View to the invite.
Game.appid = 3892270;
Game.name = "Gamble With Your Friends";
Game.maxPlayers = 4;
Game.requireGamepadLayout = true; // launch refuses pads without a Gamepad layout
Game.launchMessage =
  "To play together: on the main menu the host presses Invite Friends (or " +
  "View/Back on the controller) to add players to the lobby. Once the " +
  "others have popped in automatically, the host chooses Host Game → " +
  "Yes, I am → a save slot.";
Game.gameArgs = "-force-d3d11";   // Steam launch option "DX 11"; D3D12 is the default
Game.isolation = "full";
Game.borderless = false;
Game.steamInputFilter = false;    // the game never calls ISteamInput
Game.goldberg = true;
Game.goldbergDisableLan = false;  // invites need LAN discovery
Game.goldbergInvites = true;
Game.oskEnabled = true;
Game.bepinexPlugins = ["JanusPadFocus.dll"];  // pads work in unfocused tiles
Game.unityUiScale = true;         // "UI scale" option (BepInEx, NOTES.md)
Game.resizeGame = "unity";        // re-flow: the plugin resizes it to its new tile

// Controller sensitivity: the game's default (9) is far too low for a pad.
// Raised to CONTROLLER_SENSITIVITY while still at the default, so a player's
// own setting is kept. settings.json is the game's own file in the profile's
// prefix; a profile that never ran gets a file with just that entry (the
// game fills in the rest).
const DEFAULT_SENSITIVITY = 9;
const CONTROLLER_SENSITIVITY = 200;
const SETTINGS = "drive_c/users/steamuser/AppData/LocalLow/TENSTACK/Gamble With Your Friends/settings.json";

function setControllerSensitivity(ctx) {
  if (!ctx.winePrefix) return;
  const path = `${ctx.winePrefix}/${SETTINGS}`;
  const entry = {key: "controllerSensitivity", type: "slider", boolValue: false,
                 floatValue: CONTROLLER_SENSITIVITY, index: 0, stringValue: "",
                 width: 0, height: 0};
  if (!Janus.exists(path)) {
    Janus.writeJson(path, {entries: [entry], timestamp: 0, version: "1.0"});
    Janus.log(`controller sensitivity ${CONTROLLER_SENSITIVITY} (new settings)`);
    return;
  }
  const data = Janus.readJson(path);
  const e = (data.entries || []).find(x => x.key === "controllerSensitivity");
  if (!e) data.entries = (data.entries || []).concat([entry]);
  else if (Math.abs(e.floatValue - DEFAULT_SENSITIVITY) < 0.01) e.floatValue = CONTROLLER_SENSITIVITY;
  else return;
  Janus.writeJson(path, data);
  Janus.log(`controller sensitivity ${DEFAULT_SENSITIVITY} -> ${CONTROLLER_SENSITIVITY}`);
}

Game.play = function (ctx) {
  setControllerSensitivity(ctx);
  if (ctx.width && ctx.height) {
    ctx.gameArgs.push("-screen-fullscreen", "0",
                      "-screen-width", String(ctx.width),
                      "-screen-height", String(ctx.height));
  }
};

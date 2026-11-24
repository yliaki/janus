// DDNet (DDraceNetwork) — native Linux (SDL2), Steam build.
//
// Controllers: DDNet's own controller support (inp_controller_*, SDL joystick
// API) with absolute aim: the right stick's direction IS the aim direction.
// (Emulating a mouse with the stick was bad: DDNet adds mouse motion to a
// cursor clamped to a circle, so the aim depended on where it was before.)
// Its menus take the stick as a cursor too, but no pad button clicks and only
// Esc opens the menu, so Janus adds exactly those (Game.keyboardMouse with
// keepPad: the game keeps its pad): RT = left click (fire in game, the same
// as RT's own bind), Start = Esc. Needs Janus input.
//
// Playing together: by default Janus starts the DDNet-Server that ships with
// the game, on 127.0.0.1 only (not registered on the master server, not
// reachable from the LAN), and every tile connects to it on launch. Map:
// option below; the server's own map votes (Esc -> Call vote) change it
// in game. Option join = "browser" instead leaves every tile at the main menu, to
// join public servers; option server = "host:port" sends every tile straight
// to that online server (servers cap clients per IP, usually 4).
//
// Everything the game needs is passed as console commands on its command line
// (DDNet runs each argument after settings_ddnet.cfg): window size, controller
// settings, binds, connect. They'd be saved into settings_ddnet.cfg on exit, so
// the owner's real config is Janus.backup'd and restored at session end; the
// other profiles have their own home (isolation "secondary") and keep theirs.
//
// Pad layout: left stick or D-pad left/right = walk, right stick = aim (menu
// cursor), A = jump, RT = fire (menu click), LT or B = hook, LB/RB = previous/
// next weapon, X (hold, aim, release) = emote wheel, Y = scoreboard,
// Start = menu, D-pad up/down = vote yes/no. View does nothing (it used to be
// kill; too easy to hit by accident).
Game.appid = 412220;
Game.name = "DDNet";
Game.maxPlayers = 8;
Game.janusInput = true;
Game.steamInput = false;          // Start = Esc / RT = click come from Janus input
Game.isolation = "secondary";     // own settings (name, skin, binds) per profile
Game.borderless = false;
Game.keyboardMouse = {             // only what DDNet's pad support lacks
  keepPad: true,
  leftStick: null,
  rightStick: null,
  buttons: {rt: "mouse1", start: "Escape"},
};
Game.resizeGame = "window";       // SDL window, DDNet re-lays out on resize
Game.oskEnabled = true;           // L3+R3: type names, chat, server addresses

const MAPS = ["Tutorial", "Sunny Side Up", "Gold Mine", "Tsunami", "LearnToPlay"];
const PORT = 8303 + 97;           // off the default port, in case a real server runs

Janus.addOption("join", {
  label: "Playing together",
  description: "local: Janus starts a private DDNet server on this computer " +
               "and every player joins it at once. browser: everyone starts " +
               "at the main menu (public servers via Play -> Internet).",
  type: "choice", choices: ["local", "browser"], default: "local"});
Janus.addOption("server", {
  label: "Online server address (optional)",
  description: "e.g. 45.141.57.22:8316 (Play -> Internet shows each server's " +
               "address). Set = every player joins that server on launch, " +
               "instead of the local server. Servers allow only a few players " +
               "from one internet connection (usually 4).",
  type: "string", default: ""});
Janus.addOption("map", {
  label: "Map (local server)",
  description: "Map the local server starts on. Change it in game with " +
               "Esc -> Call vote.",
  type: "choice", choices: MAPS, default: "Tutorial"});

Game.launchMessage =
  "Left stick walks, right stick aims (in menus it moves the cursor), A jumps, " +
  "RT fires / clicks, LT or B hooks, LB/RB switch weapons, X emotes, " +
  "Y scoreboard, Start = menu. L3+R3 opens the " +
  "on-screen keyboard.";

// DDNet's own controller support (SDL joystick API, raw indices). Janus input
// gives the game one virtual wired Xbox 360 pad (xpad layout): axes 0/1 left
// stick, 2 LT, 3/4 right stick, 5 RT (triggers rest at -1: bind only _right);
// buttons 0 A, 1 B, 2 X, 3 Y, 4 LB, 5 RB, 6 Back, 7 Start, 8 Guide, 9 L3,
// 10 R3; hat 0 = D-pad. Its only pad, so no GUID needed.
const PAD_CMDS = [
  "inp_controller_enable 1", "inp_controller_guid \"\"",
  "inp_controller_absolute 1", "inp_controller_x 3", "inp_controller_y 4",
  "inp_controller_tolerance 5", "ui_controller_sens 50",
  "bind joy_axis0_left +left", "bind joy_axis0_right +right",
  "bind joy_hat0_left +left", "bind joy_hat0_right +right",
  "bind joystick0 +jump", "bind joy_axis5_right +fire", "bind joy_axis2_right +hook",
  "bind joystick1 +hook", "bind joystick4 +prevweapon", "bind joystick5 +nextweapon",
  "bind joystick2 +emote", "bind joystick3 +scoreboard",
  "unbind joystick6",   // was kill: also clears it from profiles' saved settings
  "bind joy_hat0_up \"vote yes\"", "bind joy_hat0_down \"vote no\"",
];

// DDNet's console needs quoted strings for names with spaces etc.
function q(s) { return '"' + String(s).replace(/\\/g, "\\\\").replace(/"/g, '\\"') + '"'; }

function configPath(ctx) { return ctx.home + "/.local/share/ddnet/settings_ddnet.cfg"; }

let serverPid = null;

function startServer(ctx) {
  if (serverPid) return;
  // Its data dir (ranks sqlite, teehistorian, logs): Janus's run dir, never
  // the real ~/.local/share/ddnet.
  const dir = ctx.profilesDir.replace(/\/profiles\/?$/, "") + "/run/412220/server";
  Janus.mkdir(dir);
  // Steam's install: <installDir>/ddnet/DDNet-Server (beside DDNet).
  const bin = Janus.glob(ctx.installDir + "/**/DDNet-Server")[0];
  if (!bin) Janus.error("DDNet-Server isn't in " + ctx.installDir +
                        ". Verify the game's files in Steam, or set " +
                        "\"Playing together\" to browser.");
  const binDir = bin.replace(/\/[^/]*$/, "");
  const cmds = [
    "sv_port " + PORT,
    "bindaddr 127.0.0.1",
    "sv_register 0",
    "sv_name " + q("Janus split screen"),
    "sv_max_clients_per_ip 16",
    "sv_map " + q(ctx.options.map || "Tutorial"),
    "stdout_output_level -1",
  ].join("; ");
  serverPid = Janus.spawn([bin, cmds], {cwd: binDir, env: {XDG_DATA_HOME: dir}});
  Janus.log("DDNet-Server pid " + serverPid + " on 127.0.0.1:" + PORT +
            ", map " + (ctx.options.map || "Tutorial"));
}

Game.play = function (ctx) {
  const cfg = configPath(ctx);
  const fresh = !Janus.exists(cfg);
  // The real config (owner): restored after, or removed again if DDNet only
  // made it during the session (it saves the command-line settings on quit).
  if (ctx.profile.owner) Janus.backup(cfg);

  const cmds = [];
  if (ctx.width && ctx.height)
    cmds.push("gfx_fullscreen 0", "gfx_borderless 0",
              "gfx_screen_width " + ctx.width, "gfx_screen_height " + ctx.height);
  cmds.push("snd_nonactive_mute 0", "cl_show_welcome 0");
  cmds.push.apply(cmds, PAD_CMDS);
  // A fresh profile would play under the Steam account's name: use its own.
  if (!ctx.profile.owner) {
    const named = !fresh && Janus.readLines(cfg).some(function (l) {
      return l.indexOf("player_name ") === 0 && l.trim() !== 'player_name ""';
    });
    if (!named) cmds.push("player_name " + q(ctx.profile.name.slice(0, 15)));
  }
  const server = String(ctx.options.server || "").trim();
  if (server) {
    if (!/^[A-Za-z0-9.\-:\[\]]+$/.test(server))
      Janus.error("\"" + server + "\" isn't a server address (host:port).");
    cmds.push("connect " + server);
  } else if (ctx.options.join === "local") {
    startServer(ctx);
    cmds.push("connect 127.0.0.1:" + PORT);
  }
  ctx.gameArgs.push(cmds.join("; "));
};

Game.onStop = function () { serverPid = null; };

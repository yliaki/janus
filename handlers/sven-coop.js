// Sven Co-op — GoldSrc, native 32-bit (svencoop.sh → svencoop_linux).
//
// - Input: the game reads pads itself (GoldSrc SDL joystick, "joystick 1",
//   svencoop/controller-xb360.cfg, Xbox 360 axis numbering); it never calls
//   Steam Input, so no Steam Input filter (it blocked the raw pads). Steam's
//   default layout for Sven is keyboard/mouse: it makes no virtual pads and
//   every pad steers the focused tile through the shared desktop pointer.
//   With a Gamepad layout (requireGamepadLayout checks every pad at launch)
//   Steam gives each pad an Xbox 360 virtual pad, which matches the game's
//   config for any pad type, and the hook gives each instance its own.
//   SDL2 drops joystick input while a window is unfocused; only one tile has
//   focus, hence SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS.
// - Menus are mouse-only, so nobody goes through them: instance 1 starts a
//   listen server (exec janus_host.cfg: sv_lan 1, maxplayers, the map), the
//   others "+connect 127.0.0.1:27015", each with its own server + client port. The map
//   name "-sp_campaign_portal" (the campaign hub) starts with "-", which the
//   engine's command line takes as a new switch; hence the cfg. svencoop/ is
//   a symlink into the install in the mirror, so onMirrorReady makes it a real
//   dir of symlinks and writes the cfg there; the install stays untouched.
// - Steam overlay off: svencoop.sh appends ~/.steam/bin32/gameoverlayrenderer.so
//   to LD_PRELOAD. With the Janus hook loaded too, the game ran out of file
//   descriptors at start ("Failed to create SDL Window": the GL driver's dlopen
//   failed with "Too many open files", ~300 leaked dir fds on "/" and /sys),
//   and before the hook's dlsym fix the overlay recursed forever in open().
//   "-nosteamoverlay" would empty LD_PRELOAD (the hook too), so a copy of the
//   script sets STEAMOVERLAY=0 and keeps LD_PRELOAD. The overlay can't work
//   here anyway (Goldberg, gamescope).
Game.appid = 225840;
Game.name = "Sven Co-op";
Game.maxPlayers = 8;
Game.launchMessage =
  "Player 1 starts a game in the campaign hub map, the others join it " +
  "automatically. Walk into a portal to pick a campaign.\n\n" +
  "Mouse: click a game to use the mouse there. If one controller or the " +
  "mouse moves two games at once, press Alt+1.";
Game.isolation = "prefix";
Game.goldberg = true;
Game.goldbergDisableLan = false;
Game.steamInputFilter = false;
Game.requireGamepadLayout = true;
Game.staggerSeconds = 12;         // the listen server must be up before the others connect
Game.kwinKeepAbove = false;
Game.mirrorCopyInstead = ["svencoop.sh"];
Game.extraEnv = {SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS: "1"};

Janus.addOption("map", {
  label: "Start map",
  description: "Map the host starts. -sp_campaign_portal is the hub with a " +
               "portal to every campaign.",
  type: "string", default: "-sp_campaign_portal"});
Janus.addOption("padConfig", {
  label: "Gamepad controls",
  description: "Load the game's Xbox controller bindings (controller-xb360.cfg) " +
               "at start; the right stick looks around.",
  type: "bool", default: true});

Janus.addOption("skipBriefings", {
  label: "Skip map briefings",
  description: "Don't show the mission briefing window at map start. It can " +
               "only be closed with the mouse or keyboard, not the pad.",
  type: "bool", default: true});

const PORT = 27015;

// <mirror>/<rel> as a real dir whose entries link into the install.
function materialize(ctx, rel, skip) {
  Janus.remove(`${ctx.gameDir}/${rel}`);
  Janus.mkdir(`${ctx.gameDir}/${rel}`);
  for (const p of Janus.glob(`${ctx.installDir}/${rel}/*`)) {
    const name = p.split("/").pop();
    if (!skip || !skip(name)) Janus.symlink(p, `${ctx.gameDir}/${rel}/${name}`);
  }
}

Game.onMirrorReady = function (ctx) {
  if (Janus.replaceLine("svencoop.sh", "^STEAMOVERLAY=1", "STEAMOVERLAY=0") < 0 ||
      Janus.replaceLine("svencoop.sh", '^\\s*export LD_PRELOAD="" #',
                        '\texport LD_PRELOAD="${LD_PRELOAD}"') < 0)
    Janus.warn("Sven Co-op: svencoop.sh changed; couldn't turn the Steam overlay off. " +
               "The game may fail to start.");
  // svencoop/ as a real dir of symlinks, so janus_host.cfg stays in the mirror.
  materialize(ctx, "svencoop");
  // The briefing is maps/<map>_motd.txt, shown by each client.
  if (ctx.options.skipBriefings)
    materialize(ctx, "svencoop/maps", n => n.endsWith("_motd.txt"));
  // Pad controls: the game's Xbox config, from userconfig.cfg (the user's own
  // file, run at start after the joystick is set up; "+exec" on the command
  // line ran too early and the sticks only moved, never turned). +jlook
  // makes the right stick look up/down too, not only turn.
  if (ctx.options.padConfig) {
    const uc = `${ctx.gameDir}/svencoop/userconfig.cfg`;
    const own = Janus.exists(uc) ? Janus.readFile(uc) : "";
    Janus.remove(uc);
    Janus.writeFile(uc, own + "\n// Janus: controller\nexec controller-xb360.cfg\n+jlook\n");
  }
  Janus.writeLines(`${ctx.gameDir}/svencoop/janus_host.cfg`, [
    "sv_lan 1",
    // No server MOTD either (same window, also mouse-only to close).
    ...(ctx.options.skipBriefings ? ['motdfile "janus_no_motd.txt"'] : []),
    `maxplayers ${Math.max(ctx.numPlayers || 1, 2)}`,
    // Start the map only once the main menu is up. Started straight from the
    // command line, the host's game menus ignored the mouse for the whole
    // session (no cursor, no clicks); a map started later, from the console,
    // keeps them working. "wait" = one frame.
    Array(600).fill("wait").join(";"),
    `map ${ctx.options.map}`]);
};

Game.play = function (ctx) {
  if (ctx.width && ctx.height)
    ctx.gameArgs.push("-w", String(ctx.width), "-h", String(ctx.height));
  // Every instance opens a server and a client port; a taken server port
  // leaves the client without network ("Could not get IP address").
  ctx.gameArgs.push("-port", String(PORT - 1 + ctx.instance),
                    "+clientport", String(27004 + ctx.instance));
  if (ctx.instance === 1) ctx.gameArgs.push("+exec", "janus_host.cfg");
  else ctx.gameArgs.push("+connect", `127.0.0.1:${PORT}`);
};

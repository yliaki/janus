// Supermarket Together — Unity (Mono), Proton (Windows-only build). Mirror
// networking over Steam P2P (FizzySteamworks) with Steam lobbies, so Goldberg
// covers it (same stack as Gamble With Your Friends).
//
// Input: Rewired, keyboard and mouse only (it sees an Xbox pad but has no
// bindings for it, and the menus are mouse-driven). So every player's pad
// drives keyboard and mouse in their own gamescope (Game.keyboardMouse,
// src/host/kbm.py: XTest into that instance's X server, nothing reaches the
// desktop or the other tiles); the game itself gets no pad. Needs Janus input.
// Game bindings (Options -> Customize Controls): WASD move, mouse look, LMB
// main action, RMB secondary, E build, F drop, Space jump, Left Shift sprint,
// Tab builder, mouse wheel menu next/previous, C crouch, Z/X builder tabs,
// P / Escape menu, T chat, 1-8 dances.
Game.appid = 2709570;
Game.name = "Supermarket Together";
Game.maxPlayers = 4;
Game.launchMessage =
  "Supermarket Together only takes keyboard and mouse, so Janus turns each " +
  "controller into its own keyboard and mouse: left stick walks, right stick " +
  "looks around and moves the menu cursor, RT clicks (main action), LT right-" +
  "clicks, A jumps, X builds, Y opens the builder, B drops, LB/RB scroll, " +
  "L3 sprints, R3 crouches, Start opens the menu. New Store needs a " +
  "name: click the name field, then press View/Back to type one.";
Game.janusInput = true;
Game.steamInput = false;          // the controller -> keyboard/mouse needs Janus input
Game.isolation = "full";
Game.borderless = false;
Game.goldberg = true;
Game.goldbergDisableLan = false;  // invites need LAN discovery
Game.goldbergInvites = true;
Game.keyboardMouse = {
  leftStick: "wasd",
  rightStick: "mouse",
  mouseSpeed: 1400,
  buttons: {
    rt: "mouse1",                 // main action
    lt: "mouse3",                 // secondary action
    a: "space",                   // jump
    b: "f",                       // drop item
    x: "e",                       // build
    y: "Tab",                     // open builder
    lb: "wheelup",                // menu previous
    rb: "wheeldown",              // menu next
    l3: "Shift_L",                // sprint
    r3: "c",                      // crouch
    start: "Escape",
    back: "type:Janus Mart",      // a store name (New Store needs one)
    left: "z",                    // builder tab previous
    right: "x",                   // builder tab next
    up: "1",                      // dance
    down: "2",
  },
};

Game.play = function (ctx) {
  // With two or more instances, one of them froze within a couple of
  // minutes (every run): Unity's render thread stuck in DXGI Present, DXVK's
  // present thread stuck in Vulkan, i.e. waiting on gamescope's WSI layer.
  // Presenting through Xwayland instead: no freeze (NOTES.md).
  ctx.env.ENABLE_GAMESCOPE_WSI = "0";
  if (ctx.width && ctx.height)
    ctx.gameArgs.push("-screen-fullscreen", "0",
                      "-screen-width", String(ctx.width),
                      "-screen-height", String(ctx.height));
};

// Left 4 Dead 2 — Source engine, native 32-bit. SDL2 comes in via ELF
// NEEDED; controller isolation happens at the evdev layer (NOTES.md).
// Sheet mirrors the tuned GUI config; -w/-h come from the tile in play().
Game.appid = 550;
Game.name = "Left 4 Dead 2";
Game.maxPlayers = 4;
// No -novid: with it Source reports the missing en_US.UTF-8 locale (the
// runtime container has none) as a "SetLocale('en_US.UTF-8') failed" popup
// that stops every instance until OK is clicked; without it, only in the log.
Game.gameArgs = "-console -nointro +sv_lan 1 -insecure +sv_allow_lobby_connect_only 0";
Game.isolation = "full";
// Deviation from the saved JSON (which predates the MACHENIKE pads): keep
// Steam Input active and vtable-filter it per instance. Validated on this
// game (NOTES.md); needed for pads without an SDL gamecontroller mapping.
Game.steamInputFilter = true;
Game.borderless = false;
Game.goldbergNoOverlay = false;
Game.goldbergDisableLan = false;
Game.kwinKeepAbove = false;

Game.play = function (ctx) {
  // Explicit geometry keeps Source from starting at its 640x480 default.
  if (ctx.width && ctx.height) {
    ctx.gameArgs.push("-w", String(ctx.width), "-h", String(ctx.height));
  }
};

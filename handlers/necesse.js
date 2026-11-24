// Necesse — native Linux but Java: the real entry point exists only in
// Steam's appinfo.vdf launch config, so executable auto-detection would find
// the dedicated-server scripts instead. The handler states the JVM command.
// Sheet mirrors the tuned GUI config (goldberg hybrid, overlay + LAN on,
// debug build for lobby detection). See GAMES.md + NOTES.md.
Game.appid = 1169040;
Game.name = "Necesse";
Game.maxPlayers = 4;
Game.launchMessage =
  "To play together: one player hosts a world and invites the others from " +
  "the game's invite list (they show up as Steam friends); they join " +
  "automatically.";
Game.launchCommand = "./jre/bin/java -XX:+UnlockExperimentalVMOptions -XX:+UseG1GC -XX:+ExplicitGCInvokesConcurrent -XX:G1NewSizePercent=20 -XX:G1ReservePercent=20 -XX:MaxGCPauseMillis=50 -XX:G1HeapRegionSize=32M -Xms512m -Xmx4G -jar Necesse.jar";
Game.isolation = "full";
Game.borderless = false;
Game.goldberg = true;
Game.goldbergNoOverlay = false;   // overlay ON (gbe_fork experimental UI)
Game.goldbergDisableLan = false;  // LAN discovery on — Necesse handles loopback peers fine
Game.goldbergDebug = true;        // debug build -> lobby-ID log lines
// REQUIRED: steamworks4j dereferences ISteamController without a null check
// (SIGSEGV in SteamControllerNative_init ~1 s in), so it must get the REAL
// interface: the vtable filter + Goldberg hybrid (CLAUDE.md, NOTES.md). This
// line was missing from the 2026-08-30 sheet conversion -> instant crash.
Game.steamInputFilter = true;
Game.goldbergInvites = true;      // in-game invite joins running instances (validated)
Game.kwinKeepAbove = false;

// Re-flow (dynamic players): Janus keeps the game's window at its tile size
// (resize.py). That needs displayMode = Windowed: Borderless (the game's
// default) always fills gamescope's whole, monitor-sized screen and snaps
// back from every resize. The home is the profile's own (isolation full).
Game.resizeGame = "window";

Game.play = function (ctx) {
  var cfg = ctx.home + "/.config/Necesse/cfg/settings.cfg";
  if (!Janus.exists(cfg)) return;   // first run: the game writes it (Borderless)
  if (Janus.replaceLine(cfg, "^\\s*displayMode\\s*=", "\t\tdisplayMode = Windowed, // Windowed, Borderless, Fullscreen") > 0)
    Janus.log("Necesse: windowed (Janus resizes it with its tile)");
};

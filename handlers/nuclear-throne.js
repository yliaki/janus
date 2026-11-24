// Nuclear Throne — GameMaker, 64-bit but scout-era: it links
// libcrypto.so.1.0.0, which only the scout runtime still ships, so force the
// scout-on-soldier stack (Game.runtime). Launch the ELF directly — the
// shipped run.sh hardcodes DISPLAY=:0, which is wrong inside gamescope.
// On X11 sessions the game may additionally want gamescope "-g
// --force-grab-cursor" (see GAMES.md); the Wayland config doesn't need it.
Game.appid = 242680;
Game.name = "Nuclear Throne";
Game.exeName = "nuclearthrone";
Game.runtime = "scout";
Game.isolation = "prefix";
Game.goldberg = true;
Game.kwinKeepAbove = false;

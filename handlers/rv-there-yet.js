// RV There Yet? — Unreal Engine, Proton. Must force -dx11: the D3D12/VKD3D
// default rubber-bands hard under gamescope on NVIDIA (NOTES.md). Wired pad
// preferred (wireless Xbox pads renumber evdev on reconnect). Sheet mirrors
// the tuned GUI config; -ResX/-ResY come from the tile in play().
Game.appid = 3949040;
Game.name = "RV There Yet?";
Game.launchMessage =
  "To play together: one player hosts a game and uses the invite button; " +
  "the others join automatically.";
Game.gameArgs = "-dx11 -fullscreen";
Game.exeName = "Ride-Win64-Shipping.exe";   // UE4SS (unrealUiFit) goes next to it
Game.isolation = "prefix";
Game.borderless = false;
Game.goldberg = true;
Game.goldbergNoOverlay = false;
Game.goldbergDisableLan = false;
Game.goldbergInvites = true;      // in-game invite joins running instances (validated)
Game.unrealUiScale = true;        // "UI scale" options: Engine.ini (unreal.py)
Game.unrealUiFit = true;          // ... fitted per screen via UE4SS (unreal.py); main menu
Game.resizeGame = "unreal";       // re-flow: the UE4SS mod runs r.SetRes to its new tile
                                  // 1.05 in 1920x540 (its sign stack fills the height at 1.0);
                                  // in-game HUD not checked yet (2026-10-25)
Game.steamInputFilter = true;
Game.kwinKeepAbove = false;

Game.play = function (ctx) {
  if (ctx.width && ctx.height) {
    ctx.gameArgs.push("-ResX=" + ctx.width, "-ResY=" + ctx.height);
  }
};

// Palworld — Unreal Engine, Proton. Multiplayer needs a GENUINE Steam login:
// Palworld logs into Epic Online Services with a Steam ticket, so under
// Goldberg the multiplayer menu is greyed out ("multiplayer not available"),
// and it also needs Proton on the runtime it declares (fixed in steam.py).
// So: no Goldberg, all instances on the user's real account, and co-op via a
// LOCAL Palworld Dedicated Server (Steam tool, app 2394010) with
// bUseAuth=False, joined at 127.0.0.1:8211. That server keys players by the
// reported steam_<SteamID64>, so every player but the Steam account's gets
// their profile's Steam ID via JANUS_STEAM_ID_OVERRIDE (the hook changes only
// ISteamUser::GetSteamID in that process) — otherwise the second is rejected
// as "already logged in", and each profile keeps its own character on the
// server. See NOTES.md.
//
// The shipping exe is SteamStub-wrapped (.bind) and relaunches through Steam;
// Game.steamStubExes makes Janus unpack it with Steamless (cached, redone after
// a game update) and launch the unpacked copy from a per-instance mirror.
Game.appid = 1623730;
Game.name = "Palworld";
Game.maxPlayers = 4;
Game.isolation = "prefix";        // own Wine prefix, shared home -> Steam reachable
Game.goldberg = false;
Game.exeName = "Palworld-Win64-Shipping.exe";
Game.steamStubExes = ["Pal/Binaries/Win64/Palworld-Win64-Shipping.exe"];

Janus.addOption("server", {
  label: "Start local dedicated server",
  description: "Start the Palworld Dedicated Server (Steam tool 2394010)" +
               " with the session and stop it afterwards. " +
               "Join each instance at 127.0.0.1:8211.",
  type: "bool", default: true,
});

Game.launchMessage = Janus.getOption("server")
  ? "To play together: every player chooses \"Join Multiplayer Game\" and " +
    "joins the address 127.0.0.1:8211 (this should be in the input box by default)."
  : "";

// Make sure no stray server outlives the session (the binary, not just the
// spawned process, in case it was started some other way).
Game.killOnClose = ["PalServer-Linux-Shipping"];
Game.unrealUiScale = true;        // "UI scale" options: Engine.ini (unreal.py)
// No unrealUiFit: UE4SS (experimental 2026-10-17) crashes Palworld at startup,
// whatever the settings (NOTES.md "Unreal UI fit on other games").

// The server's install dir, in whichever Steam library it went to.
function serverRoot() {
  return Janus.appInstallDir(2394010);
}

function serverBin(root) {
  return root + "/Pal/Binaries/Linux/PalServer-Linux-Shipping";
}

// Every instance checks, so none starts without the server (the identical
// error is shown once per session).
function requireServer() {
  var root = serverRoot();
  if (root && Janus.exists(serverBin(root))) return;
  Janus.error((root ? "Palworld Dedicated Server is incomplete (no " + serverBin(root) + ")."
                    : "Palworld Dedicated Server is not installed.") + "\n\n" +
              "Install it from Steam (Library → Tools → \"Palworld Dedicated " +
              "Server\", or steam://install/2394010), or turn off \"Start local " +
              "dedicated server\" in the game's options.");
}

// Start the server once per session, from instance 1, before the clients
// spawn (it is ready long before a client reaches the main menu). Runs the
// binary directly: PalServer.sh starts it as a CHILD, so terminating the
// script would leave the server running. Like Nucleus, the server plays its
// OWN default world (PalServer/Pal/Saved/SaveGames/0/<id>), created on first
// start — not the host's single-player world.
function startServer() {
  var root = serverRoot();
  var bin = serverBin(root);
  if (Janus.run(["pgrep", "-f", "PalServer-Linux-Shipping"]).code === 0) {
    Janus.log("dedicated server already running — using it");
    return;
  }
  // What PalServer.sh does before starting the binary.
  var sc = root + "/Pal/Binaries/Linux/steamclient.so";
  if (!Janus.exists(sc)) Janus.copy(root + "/linux64/steamclient.so", sc);
  // bUseAuth=False: the instances are not distinct Steam sessions (see header).
  // Keep any other settings the user made; only flip bUseAuth.
  var cfg = root + "/Pal/Saved/Config/LinuxServer/PalWorldSettings.ini";
  var text = Janus.exists(cfg) ? Janus.readFile(cfg) : "";
  if (text.indexOf("OptionSettings") < 0) text = Janus.readFile(root + "/DefaultPalWorldSettings.ini");
  Janus.writeFile(cfg, text.replace("bUseAuth=True", "bUseAuth=False"));
  var pid = Janus.spawn([bin, "Pal", "-port=8211"], {cwd: root});
  Janus.log("dedicated server started (pid " + pid + "), join at 127.0.0.1:8211");
}

Game.play = function (ctx) {
  if (ctx.options.server) {
    requireServer();
    if (ctx.instance === 1) startServer();
  }
  // The profile's fixed SteamID64 (a string: SteamID64s exceed 2^53), so the
  // server sees distinct players. The Steam account plays as itself.
  if (!ctx.profile.owner) ctx.env.JANUS_STEAM_ID_OVERRIDE = ctx.profile.steamId;
};

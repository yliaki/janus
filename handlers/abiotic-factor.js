// Abiotic Factor — Unreal Engine 5.4, Proton. Co-op via Goldberg + the game's
// LAN server list. Validated 2026-10-27 with two fake pads, 1920x540 tiles:
// host Load Game → LAN Server ticked → Host Game; joiner Join a Server →
// Switch to LAN → the host's row → Join → job/traits → in the host's world.
//
// - Online subsystem: the game ships EOSPlus (EOS on top of Steam). Under
//   Goldberg the EOS login fails ("Invalid session ticket",
//   EOS_Connect_ExternalTokenValidationFailed), the game reports "Single
//   Player and LAN play are the only options", and even the LAN list then
//   says "Search failed (reason: you are offline)". play() switches the
//   instance's Engine.ini to plain OnlineSubsystemSteam (= Goldberg) with
//   UE's IpNetDriver: the session is created through Goldberg, the server
//   listens on UDP 7777 and the LAN list finds it. SteamNetDriver (Steam P2P
//   sockets) can't bind under Goldberg ("SteamSockets: binding to port 7777
//   failed"), so bUseSteamNetworking is off.
// - Invites don't work: with the Steam subsystem the pause menu shows
//   "Invite friends" and a lobby code, and gbe delivers the invite (the joiner
//   calls JoinLobby), but the joiner either doesn't travel or fails with
//   "Unable to connect to the server" (AddressResolutionFailed: the Steam
//   session advertises no IP). Hence no goldbergInvites.
// - Menus use a pad-driven cursor (left stick, A clicks); the D-pad does
//   nothing there. A new world needs a typed name (keyboard).
// - Saves: AppData/Local/AbioticFactor/Saved/SaveGames/<steamid>/ in the
//   prefix, keyed by Steam ID. `secondary`: the owner keeps the real prefix
//   (its Engine.ini is backed up and restored), the others get their own.
// - UI is readable at 1920x540 (menus, character screens, HUD).
//   unrealUiScale offers the Engine.ini UI options; UE4SS (unrealUiFit)
//   crashes this game (NOTES.md "Unreal UI fit on other games").
Game.appid = 427410;
Game.name = "Abiotic Factor";
Game.maxPlayers = 6;
Game.launchMessage =
  "Menus: move the cursor with the left stick, A clicks.\n\n" +
  "To play together: one player hosts a world with \"LAN Server\" ticked " +
  "(untick \"Singleplayer Only\" for a new world; its name needs a keyboard). " +
  "The others choose Join a Server → Switch to LAN, pick the host's server " +
  "and Join.";
// The real game, not the AbioticFactor.exe bootstrapper.
Game.exeName = "AbioticFactor-Win64-Shipping.exe";
Game.isolation = "secondary";     // saves are keyed by Steam ID: the owner keeps theirs
Game.borderless = false;
Game.goldberg = true;
Game.goldbergDisableLan = false;  // the LAN server list goes through Goldberg's LAN
Game.unrealUiScale = true;        // "UI scale" options (Engine.ini)

Janus.addOption("dx11", {
  label: "Use DirectX 11",
  description: "Lighter than DirectX 12 when several copies of the game run at once.",
  type: "bool", default: true});

Game.play = function (ctx) {
  // Plain Steam (= Goldberg) online subsystem with IP networking, see above.
  const ini = `${ctx.winePrefix}/drive_c/users/steamuser/AppData/Local/AbioticFactor/Saved/Config/Windows/Engine.ini`;
  if (ctx.profile.owner) Janus.backup(ini);
  Janus.iniSet(ini, "OnlineSubsystem", "DefaultPlatformService", "Steam");
  Janus.iniSet(ini, "OnlineSubsystemSteam", "bEnabled", "true");
  Janus.iniSet(ini, "OnlineSubsystemSteam", "bUseSteamNetworking", "false");
  Janus.iniSet(ini, "OnlineSubsystemEOS", "bEnabled", "false");
  Janus.iniSet(ini, "/Script/Engine.GameEngine", "!NetDriverDefinitions", "ClearArray");
  Janus.iniSet(ini, "/Script/Engine.GameEngine", "+NetDriverDefinitions",
    '(DefName="GameNetDriver",DriverClassName="OnlineSubsystemUtils.IpNetDriver",' +
    'DriverClassNameFallback="OnlineSubsystemUtils.IpNetDriver")');

  if (ctx.options.dx11) ctx.gameArgs.push("-dx11");
  if (ctx.width && ctx.height)
    ctx.gameArgs.push("-windowed", "-ResX=" + ctx.width, "-ResY=" + ctx.height);
};

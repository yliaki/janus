// Factorio — native Linux (2.0, statically linked; only libsteam_api.so is a
// NEEDED lib). Multiplayer is Factorio's OWN UDP netcode, not Steam's, so the
// whole thing runs on one host over loopback. There is no in-game "invite"
// button (Steam invites only exist in the Steam overlay, which Goldberg
// doesn't have), and a running Factorio can't be told to connect. Join mode
// "auto" (default) hosts a save chosen up front (`--host <save>`) without any
// clicks. "menu" (reported not working, 2026-10-17) does the nearest thing
// to an invite: instance 1 starts at the main
// menu and hosts any save through Factorio's own Multiplayer > Host dialog
// (preset to LAN-only, no verification); the other instances are held back
// until it is hosting, then start with `--mp-connect` straight into its game.
// "manual" starts everyone at the menu (join via Browse LAN games).
//
// Why each piece exists (all found empirically 2026-10-08, see NOTES.md
// "Factorio (427520)"):
// - Goldberg: every real-Steam instance logs in as the SAME Steam account ->
//   same Factorio name -> host refuses the 2nd ("UserWithThatNameAlreadyInGame").
// - Goldberg libsteam_api is NEEDED via RUNPATH $ORIGIN/../../lib; $ORIGIN is
//   the symlink-resolved binary dir (the real install), so the mirror's lib/
//   is only reached through LD_LIBRARY_PATH -> bare launch (Game.runtime
//   "none"; Factorio needs nothing beyond glibc).
// - Per-instance write-data via -c config.ini (Factorio holds a .lock in it).
//   The file needs the "; version=N" header or Factorio calls it invalid.
// - The Steam build reads player-data.json ONLY from Steam Cloud (the local
//   copy is ignored). Under Goldberg that is <saves folder>/427520/remote/, so
//   each instance gets its own saves folder (steam_settings saves_folder_name)
//   and the LAN player name is seeded there; otherwise a "LAN username" modal
//   blocks --host.
// - --host ignores --port/config and binds a RANDOM UDP port, so clients find
//   it from the host's sockets (same detection for a menu-hosted game). Its
//   default server settings are Public + "verify user identity", which offline
//   LAN names can't pass, hence the --server-settings file; the menu's Host
//   dialog takes the same defaults from config.ini [multiplayer-lobby].
Game.appid = 427520;
Game.name = "Factorio";
Game.maxPlayers = 4;
Game.launchCommand = "bin/x64/factorio";
Game.runtime = "none";
Game.isolation = "none";          // the handler separates instances itself
Game.profileSaves = true;         // ...per player profile (ctx.saveRoot)
Game.borderless = false;
Game.goldberg = true;
// Pads: Steam Input. Factorio's static SDL reads each instance's Steam virtual
// pad (the hook allows SDL to open it and blocks the physical one), and the
// session forces Factorio's Steam Input layout (steam_input.force_layout), so
// Steam's per-game remapping applies. Factorio never requests ISteamInput.
Game.steamInputFilter = false;

Janus.addOption("join", {
  label: "Joining",
  description: "auto: instance 1 hosts the save below right away, no clicks. " +
               "menu: instance 1 starts at the main menu; host any save with " +
               "Multiplayer > Host saved game (or Host new game) and the other " +
               "instances open and join as soon as it is hosting. manual: everyone starts " +
               "at the main menu; join with Multiplayer > Browse LAN games.",
  type: "choice", choices: ["auto", "menu", "manual"], default: "auto"});
Janus.addOption("save", {
  label: "Save to host (auto)",
  description: "Save file name in your saves folder (e.g. \"MP\" or \"MP.zip\"). " +
               "Empty = most recent save. \"new\" = generate a fresh map.",
  type: "string", default: ""});
Janus.addOption("shareSaves", {
  label: "Use my real saves folder (~/.factorio/saves)",
  description: "Host your existing saves. The host autosaves there too, just " +
               "like normal play. Off = a separate saves folder per instance.",
  type: "bool", default: true});
// How the players get into one game, for the chosen join mode (options are
// already resolved at load time).
Game.launchMessage = {
  auto: "To play together: nothing to do. Player 1's game hosts the save " +
        "straight away (see the \"Save to host\" option); the other players' " +
        "games open once it has loaded and join it by themselves.",
  menu: "To play together: player 1's game starts at the main menu. Choose " +
        "Multiplayer > Host saved game (or Host new game). The other players' " +
        "games only open once player 1 is hosting, and join it by themselves.",
  manual: "To play together: one player hosts with Multiplayer > Host saved " +
          "game (or Host new game); the others choose Multiplayer > Browse " +
          "LAN games and join it.",
}[Janus.getOption("join")];
Janus.addOption("controllerInput", {
  label: "Controller input method",
  description: "Set Settings > Controls > Input method to \"Game controller\" " +
               "(Factorio defaults to keyboard and mouse) for instances with a pad.",
  type: "bool", default: true});
Janus.addOption("useMods", {
  label: "Use my mods (~/.factorio/mods)",
  description: "Every instance loads the same mod folder, so mods always match " +
               "the host (downloading mods on join needs a factorio.com login).",
  type: "bool", default: true});

const GBE_PORTS = "^4758[0-9]$";  // Goldberg's own listen socket(s)

// The real game's data dir (Steam build: ~/.factorio).
function realData(ctx) {
  return `${ctx.home}/.factorio`;
}

// A player's Factorio write-data dir: part of their profile, so it follows
// them whatever their player number.
function profileDir(ctx, profileId) {
  return `${ctx.profilesDir}/${profileId}/427520/factorio`;
}

// Factorio's LAN name: the profile's name, letters/digits/._- only.
function lanName(name, fallback) {
  const n = String(name || "").replace(/[^A-Za-z0-9._-]/g, "");
  return n.length >= 2 ? n : fallback;
}

function playerName(ctx) {
  return lanName(ctx.profile.name, `Player${ctx.instance}`);
}

// The mirror's Goldberg lib/ dir: the player's identity + own "cloud" folder.
Game.onMirrorReady = function (ctx) {
  const gse = `${profileDir(ctx, ctx.profile.id)}/goldberg`;
  Janus.writeFile("lib/steam_settings/steam_appid.txt", "427520\n");
  Janus.writeFile("lib/steam_settings/configs.user.ini",
    "[user::general]\n" +
    `account_name=${playerName(ctx)}\n` +
    `account_steamid=${ctx.profile.steamId}\n` +
    "language=english\n" +
    "[user::saves]\n" +
    `saves_folder_name=${gse.slice((ctx.home + "/.local/share/").length)}\n`);
};

function gameVersion(ctx) {
  const r = Janus.run([`${ctx.installDir}/bin/x64/factorio`, "--version"]);
  const m = /Version: (\d+\.\d+\.\d+) \(build (\d+), (\w+), (\w+)\)/.exec(r.stdout);
  return m ? {game_version: m[1], build_version: Number(m[2]),
              build_mode: m[4], platform: m[3]} : null;
}

// player-data.json lives in Goldberg's remote storage (see header).
function seedPlayerData(ctx, dir) {
  const pd = `${dir}/goldberg/427520/remote/player-data.json`;
  const name = playerName(ctx);
  if (Janus.exists(pd)) {
    Janus.jsonSet(pd, "service-username", name);
    Janus.jsonSet(pd, "service-token", "");
  } else {
    const data = {"service-username": name, "service-token": ""};
    const v = gameVersion(ctx);
    if (v) data["last-played-version"] = v;   // no changelog popup on first start
    Janus.writeJson(pd, data);
  }
  Janus.log(`instance ${ctx.instance} (${ctx.profile.name}): LAN name ${name}`);
}

function writeConfig(ctx, dir) {
  const cfg = `${dir}/config.ini`;
  if (!Janus.exists(cfg))
    Janus.writeFile(cfg, "; version=13\n[path]\n");
  Janus.iniSet(cfg, "path", "read-data", "__PATH__executable__/../../data");
  Janus.iniSet(cfg, "path", "write-data", dir);
  // Defaults of the menu's Host dialog: the stock Public + "Verify user
  // identity" reject offline LAN names, and an empty server name is refused.
  for (const [k, v] of [["name", "Janus"], ["visibility-public", "false"],
                        ["visibility-steam", "false"], ["visibility-lan", "true"],
                        ["verify-user-identity", "false"],
                        ["enable-authserver-side-bans", "false"],
                        ["only-admins-can-pause", "false"]])
    Janus.iniSet(cfg, "multiplayer-lobby", k, v);
  if (ctx.options.controllerInput && ctx.controller && ctx.controller.index != null)
    Janus.iniSet(cfg, "input", "input-method", "game-controller");
  return cfg;
}

function setupSaves(ctx, dir) {
  const saves = `${dir}/saves`;
  const real = `${realData(ctx)}/saves`;
  if (ctx.options.shareSaves) {
    Janus.mkdir(real);
    if (!Janus.exists(saves)) Janus.symlink(real, saves);
    else if (Janus.run(["test", "-L", saves]).code !== 0)
      // A real dir: saves from a session with shareSaves off. Never touch it.
      Janus.warn(`Factorio: ${ctx.profile.name} still uses their own saves ` +
                 `folder ${saves} (from a session with "Use my real saves folder" ` +
                 `off). Move its saves to ${real} and delete it to share again.`);
  } else {
    // Drop a link left by a shared session — with rm, which never follows it
    // into the real saves.
    if (Janus.run(["test", "-L", saves]).code === 0) Janus.run(["rm", saves]);
    Janus.mkdir(saves);
  }
  return saves;
}

function newestSave(saves) {
  const files = Janus.glob(`${saves}/*.zip`);
  if (!files.length) return null;
  const r = Janus.run(["stat", "-L", "-c", "%Y %n"].concat(files));
  return r.stdout.trim().split("\n").map(l => [Number(l.split(" ")[0]), l.slice(l.indexOf(" ") + 1)])
    .sort((a, b) => b[0] - a[0])[0][1];
}

function hostSave(ctx, saves, cfg, extra) {
  const want = String(ctx.options.save || "").trim();
  if (want && want !== "new") {
    const p = `${saves}/${want.endsWith(".zip") ? want : want + ".zip"}`;
    if (!Janus.exists(p)) Janus.error(`Factorio: save "${want}" not found in ${saves}.`);
    return p;
  }
  const newest = want === "new" ? null : newestSave(saves);
  if (newest) return newest;
  const stamp = new Date().toISOString().slice(0, 19).replace(/[-:]/g, "").replace("T", "-");
  const p = `${saves}/janus-${stamp}.zip`;
  Janus.log(`creating new map ${p}`);
  const r = Janus.run([`${ctx.installDir}/bin/x64/factorio`, "-c", cfg, ...extra, "--create", p],
                      {env: {LD_LIBRARY_PATH: `${ctx.gameDir}/lib`}, timeout: 180});
  if (r.code !== 0 || !Janus.exists(p))
    Janus.error(`Factorio: creating a new map failed:\n${(r.stdout + r.stderr).slice(-800)}`);
  return p;
}

// Instance 1's write-data dir (whoever plays it).
function hostDir(ctx) {
  const host = (ctx.players || []).find(p => p.instance === 1);
  return profileDir(ctx, host ? host.profile.id : "owner");
}

// Block until instance 1 is hosting, then return its UDP port. Hosting binds
// a random port, so read it from the host process's sockets. Returns null on
// timeout, "gone" if instance 1 exits first (e.g. Stop All while waiting).
function waitForHost(ctx, seconds) {
  const script = `
    dir="$1"; n=0; seen=0
    while [ $n -lt "$2" ]; do
      if pgrep -f -- "$dir/config.ini" >/dev/null; then seen=1
      elif [ $seen = 1 ] || [ $n -ge 60 ]; then echo gone; exit 2; fi
      if grep -aq 'Hosting game at' "$dir/factorio-current.log" 2>/dev/null; then
        # Menu hosting binds the configured port and logs it; only --host logs
        # :0 (random port), which must be read off the process's sockets.
        p=$(grep -a 'Hosting game at' "$dir/factorio-current.log" | tail -1 | sed -n 's/.*:\\([0-9][0-9]*\\)}).*/\\1/p')
        if [ -n "$p" ] && [ "$p" != 0 ]; then echo "$p"; exit 0; fi
        for pid in $(pgrep -f -- "$dir/config.ini"); do
          ss -Hlunp 2>/dev/null | grep "pid=$pid," | awk '$4 ~ /^0\\.0\\.0\\.0:/ {n=split($4,a,":"); print a[n]}'
        done | grep -Ev '${GBE_PORTS}' | head -1 | grep . && exit 0
      fi
      sleep 1; n=$((n+1))
    done
    exit 1`;
  const r = Janus.run(["sh", "-c", script, "sh", hostDir(ctx), String(seconds)],
                      {timeout: seconds + 15});
  if (r.code === 2) return "gone";
  return r.code === 0 ? r.stdout.trim() : null;
}

Game.play = function (ctx) {
  const dir = profileDir(ctx, ctx.profile.id);
  Janus.mkdir(dir);
  const cfg = writeConfig(ctx, dir);
  seedPlayerData(ctx, dir);
  const saves = setupSaves(ctx, dir);

  const extra = [];
  const mods = `${realData(ctx)}/mods`;
  if (ctx.options.useMods && Janus.exists(mods)) extra.push("--mod-directory", mods);

  ctx.env.LD_LIBRARY_PATH = `${ctx.gameDir}/lib`;   // reach Goldberg (see header)
  ctx.gameArgs.push("-c", cfg, ...extra);

  const join = ctx.options.join;
  if (join === "manual") return;
  if (ctx.instance === 1) {
    // Fresh log, so the clients' "Hosting game at" check can't hit a stale one.
    Janus.remove(`${dir}/factorio-current.log`, {force: true});
    if (join === "menu") {
      Janus.log("instance 1: host a game from Multiplayer > Host saved game; " +
                "the other instances join once it is hosting");
      return;
    }
    const save = hostSave(ctx, saves, cfg, extra);
    const names = (ctx.players || [{profile: ctx.profile}])
      .map((p, i) => lanName(p.profile.name, `Player${i + 1}`));
    const ss = `${dir}/server-settings.json`;
    Janus.writeJson(ss, {
      name: "Janus", description: "Split-screen session", tags: [],
      max_players: 0,
      visibility: {public: false, lan: true, steam: false},
      require_user_verification: false,
      game_password: "",
      allow_commands: "admins-only",
      admins: names,
      only_admins_can_pause_the_game: false,
      autosave_only_on_server: true,
      auto_pause: false,
    });
    Janus.log(`instance 1 hosts ${save}`);
    ctx.gameArgs.push("--server-settings", ss, "--host", save);
  } else {
    // menu: the user may take a while picking a save; auto: just load time.
    const wait = join === "menu" ? 3600 : 300;
    Janus.log(`instance ${ctx.instance}: waiting for instance 1 to host...`);
    const port = waitForHost(ctx, wait);
    if (port === "gone")
      throw new Error(`Factorio: instance 1 exited before hosting; not starting instance ${ctx.instance}.`);
    if (port) {
      Janus.log(`instance ${ctx.instance}: joining 127.0.0.1:${port}`);
      ctx.gameArgs.push("--mp-connect", `127.0.0.1:${port}`);
    } else {
      Janus.warn(`Factorio: instance 1 didn't start hosting within ${wait / 60} minutes; ` +
                 `instance ${ctx.instance} starts at the main menu (join with ` +
                 "Multiplayer > Browse LAN games).");
    }
  }
};

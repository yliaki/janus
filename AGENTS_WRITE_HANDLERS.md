# Writing a Janus handler

How to get a game from "installed in Steam" to "a handler that works for 2–4 players on one
machine". Written so a person or an AI coding agent can follow it, mostly without anyone at
the controllers. [SCRIPTING.md](SCRIPTING.md) is the API reference (every `Game.*` field,
callback, `ctx` field and `Janus.*` function); this file is the **workflow** and the
**lessons**.

**Where things are.** `<janus>` below is the Janus program folder:
`~/.local/opt/janus` (zip install), `/usr/lib/janus-split-screen` (.deb), or the repo root in
a git checkout. Commands are written for an installed Janus (`janus-cli …`); in a checkout use
`python3 src/host/main.py …` instead. Your handlers go in **`~/.config/janus/games/`**
(`<game-name>.js`, or `<game-name>/handler.js` + `payload/`), next to the ones installed from
the handler hub. Notes marked *(checkout)* only apply to Janus's own git repository (its
`handlers/` development folder, `GAMES.md`, tests; `NOTES.md` is the maintainer's own notes),
which an installed Janus doesn't include.

A handler is done when, in a real `steam-split` session with one pad per tile:

1. every instance starts, at its tile's size, without error dialogs;
2. each pad drives **only** its own tile (check it, don't assume);
3. the players can get into **one game together**, and the launch message says how;
4. saves/settings land per player profile, and the real install and the Steam account's
   real saves are never written (unless the handler deliberately uses them for the owner);
5. the UI is readable in a small tile (or you know why it can't be);
6. it's documented: a header comment in the handler saying what works and why.

Ground rules:
- **Handler first.** Game quirks belong in the handler (`~/.config/janus/games/<game>.js`, or
  `<game>/handler.js` + `payload/`), never in Janus itself. If Janus has a genuine bug, report
  it (*(checkout)*: fix it, and test that the fix doesn't break other games' paths; ask the
  maintainer before changing the hook, `steam.py` or other core code).
- **Wayland/KWin only.** Janus places its tiles with KWin; no X11-host tricks.
- **No handler = no launch**, and a broken handler fails loudly. Don't add default behaviour.

## 1. Investigate the game first (10 minutes that save hours)

```bash
grep -E 'name|installdir' ~/.local/share/Steam/steamapps/appmanifest_<appid>.acf   # every library
ls "<install dir>"                       # exe, *_Data/, Engine/, steam_api*.dll, libsteam_api.so
python3 <janus>/scripts/pe_sections.py "<exe>"   # ".bind" section = SteamStub DRM -> Game.steamStubExes
```

Work out:
- **Engine / platform.** Unity Mono (`*_Data/Managed/Assembly-CSharp.dll`), Unity IL2CPP
  (`GameAssembly.dll`, `il2cpp_data/`), Unreal (`Engine/`, `<Project>/Binaries/Win64/*-Shipping.exe`),
  Source, Java (no ELF entry point: read the launch command from Steam's `appinfo.vdf` and
  set `Game.launchCommand`, see `necesse.js`), native Linux vs Proton.
- **Networking.** Steam lobbies + P2P (Goldberg covers it), the game's own LAN, a third-party
  backend that validates Steam tickets (Photon, EOS: Goldberg's fake ticket fails, see Content
  Warning / Palworld), or a dedicated server.
- **Steam API usage.** `strings steam_api64.dll | grep -E 'SteamInput|SteamController'` shows
  the interface versions; `grep -c -a ActivateGameOverlayInviteDialog GameAssembly.dll` etc.
  hints which flows exist (invites, `+connect_lobby`).
- **Where saves and settings live** (Proton prefix `AppData/LocalLow/<company>/<game>`,
  `AppData/Local/<Project>/Saved`, Steam Cloud `userdata/<account>/<appid>/remote`, the install
  dir itself, the registry `user.reg`).
- **Prior art.** The Nucleus Co-op hub has handlers for many games:
  `https://hub.splitscreen.me/api/v1/handlers/<search>` → package
  `https://hub.splitscreen.me/cdn/storage/packages/<currentPackage>/original/handler-<_id>-v<currentVersion>.nc?download=true`
  (a zip with `handler.js`). Its `Game.*` settings (symlink exclusions, Goldberg mode, registry
  edits, save paths) are a strong hint of what the game needs — Gunfire Reborn's revealed the
  ColdClient approach in one line. Steam community threads explain the game's own error codes.
- **The Steam Input setting** (Steam: game Properties → Controller). Janus input, the default
  mode, needs **"Disable Steam Input"**; Steam Input mode needs "Enable Steam Input". Janus
  refuses to launch otherwise (and offers the other mode when that one would work), unless the
  handler sets `Game.requireSteamInput = false`. Change it in Steam yourself; a script can't
  (Steam rewrites `localconfig.vdf` from memory).

## 2. Start minimal, from the closest existing handler

Copy the handler of the most similar game (same engine + same networking; get it with
`janus-cli hub get <appid>` and read it in `~/.config/janus/games/`) and strip it down.
The usual starting sheet for a Proton co-op game:

```js
// <Game> — <engine>, Proton. <one paragraph: how co-op works, what was validated, when>
Game.appid = <appid>;            // literal on its own line: discovery greps for it
Game.name = "<Game>";
Game.maxPlayers = 4;
Game.isolation = "full";         // own prefix + home per player profile
Game.borderless = false;
Game.steamInputFilter = false;   // only for games that call ISteamInput natively (rare)
Game.goldberg = true;
Game.goldbergDisableLan = false; // invites need LAN discovery
Game.goldbergInvites = true;     // "Invite" in one instance pulls the others in
Game.launchMessage = "To play together: ...";   // short, read from the couch
Game.play = function (ctx) {
  if (ctx.width && ctx.height)   // Unity: tile size
    ctx.gameArgs.push("-screen-fullscreen", "0", "-screen-width", String(ctx.width),
                      "-screen-height", String(ctx.height));
};
```

Check it loads: `python3 -c "import sys; sys.path.insert(0,'<janus>/src/host'); import scripting; scripting.load_handler(<appid>)"`
(*(checkout)*: also `python3 tests/test_scripting.py`).

## 3. Launch, and read what happened

```bash
janus-cli steam-split <appid> --tile 0:0,0,1920,540 --tile 1:0,540,1920,540 -v
janus-cli list                            # real js indices for --tile (Steam's virtual pads hidden)
python3 <janus>/scripts/hooklog.py <appid>  # per-instance hook decisions: which pads each game may open
```

- **Screenshot before you conclude anything** (`spectacle -b -n -f -o shot.png`, then look at
  it). A "hang" is usually an error dialog; a "crash" is often a game message ("integrity
  verification failed", "installation corrupted"). Read the game's own log too (Unity:
  `output_log.txt`/`Player.log` in the instance prefix's `LocalLow`; crash dumps in
  `AppData/Local/Temp/<company>/<game>/Crashes`).
- The instance prefixes/homes are under `~/.local/share/janus/profiles/<profile>/<appid>/`;
  the per-instance game mirror under `~/.local/share/janus/goldberg_mirrors/<appid>/<n>/`.
- Stop a session with **SIGINT to the launcher's PID** (teardown restores panel, scale, KWin
  rules, backups, Steam's forced layout). See "Pitfalls" for killing leftovers.

## 4. Test autonomously with virtual pads

No need for someone at the controllers for routine checks. `<janus>/scripts/fakepad.py`
creates uinput pads that Steam treats as real ones:

```bash
python3 <janus>/scripts/fakepad.py /tmp/pads 'Logitech Gamepad F310:046d:c21d' \
                                             'Microsoft Xbox One S Controller:045e:02ea' &
janus-cli list                           # e.g. [4] F310, [5] Xbox One S -> --tile 4:... --tile 5:...
echo 'press 0 a' > /tmp/pads             # a b x y lb rb back start l3 r3 (never guide)
echo 'dpad 1 down' > /tmp/pads
echo 'stick 0 l -1 0 1.0' > /tmp/pads    # left stick full left for 1 s (r = right stick)
echo 'trigger 0 rt 0.5' > /tmp/pads
```

- Loop: screenshot → decide → a few inputs → screenshot. Watch for **button glyphs**: a game
  that switches its prompts to pad glyphs has received pad input; a screen that doesn't react
  at all usually means no input reached it (check `hooklog.py`), not "wrong button".
- Read the glyphs: confirm is not always A (Gunfire Reborn confirms popups with Y).
- **Prove isolation**: input on pad 0 only, screenshot, only tile 1 may change; then pad 1.
- For long menu journeys (tutorial, host + invite + join), spawn a **cheap sub-agent** (e.g.
  Sonnet) with: the tile layout, the FIFO path and command list, what you already know about
  the menus, the goal, a screenshot budget, and "never kill processes / never press keys or the
  mouse". Ask for the exact menu path, what worked, and the paths of the telling screenshots.
  Verify its key screenshot yourself.
- Test with Steam Input **on** (the default). If the virtual pads seem to carry nothing, check
  them directly (open `/dev/input/eventN` of the `X-Box 360 pad N` nodes **fresh** and press;
  a long-running reader keeps dead fds when Steam recreates its pads), before blaming the game.
- Things fake pads can't tell you: real-pad Steam slot behaviour, identical physical pads,
  Bluetooth drop-outs. Test those with real pads at the end.

## 5. Co-op: getting the instances into one game

In order of preference (all with Goldberg unless noted):

1. **In-game invites** (`Game.goldbergInvites = true`, `goldbergDisableLan = false`): all
   instances start together; "Invite" in one pulls the others in via `GameLobbyJoinRequested`.
   PEAK, Lethal Company, DRG, How to Fish, PAYDAY 2, Gunfire Reborn. Verify it with fake pads.
2. **Lobby ID**: `Game.goldbergDebug = true` shows the host's lobby ID in the GUI; auto-join
   (`goldbergAutoJoin`, `lobbyConnectArg`) only if the game parses `+connect_lobby` (many don't).
3. **The game's own LAN / direct connect / dedicated server**: Factorio, Terraria (Host & Play
   server, `JANUS_ARGV_ENV`), Palworld (local dedicated server, no Goldberg, `killOnClose`),
   Borderlands 2 (friends list). Keep `goldbergDisableLan = true` when LAN discovery makes the
   game auto-join itself into a deadlock (Lethal Company's first attempt).
4. **Backends that check real Steam tickets** (Photon/EOS): `Game.goldbergRealTicket` (Proton,
   Content Warning) or real Steam + per-player Steam ID override (Palworld).

Goldberg variants:
- **Replace `steam_api(64).dll`** in a per-instance mirror (the default, `Game.goldberg`).
- **The game checks its Steam DLL** ("installation corrupted", "integrity verification failed"
  — test: does the plain mirror with Valve's DLL pass?): keep Valve's DLL
  (`Game.mirrorCopyInstead` its folder) and run gbe_fork as the Steam **client** through
  ColdClientLoader (`payload/coldclient/`, `Game.buildCommand` returns the loader). Copy
  the `gunfire-reborn` (or `payday-2`) handler from the hub. Janus writes its `steam_settings/` (invite
  switches, overlay) beside every replaced `steam_api` — move them beside `steamclient64.dll`,
  where the emulator then reads them (Gunfire Reborn's `onMirrorReady`).
- **SteamStub DRM** (`.bind`): `Game.steamStubExes` (How to Fish, Palworld).
- DLCs: write `[app::dlcs]` in `steam_settings/configs.app.ini` (DRG, PAYDAY 2, Gunfire Reborn
  offer an "Unlock all DLC" option, default off).

## 6. Saves, settings, identity

- `ctx.profile` is the **person** (name, Steam ID, `owner` = the real account); `ctx.instance`
  is only the session role (host, music, ports). Key saves by profile, never by instance.
- `isolation: "full"` gives each profile its own prefix/home; `none` + `Game.profileSaves`
  when the handler keeps per-profile saves itself (Terraria, Factorio).
- Import the real save once, as a **copy** (option `importSave`, all/first/none): DRG
  (`<steamid>_Player.sav`), Gunfire Reborn (Steam Cloud `remote/` → `GSE Saves/<appid>/remote`).
- Settings that must differ per tile go in `play()`: resolution, windowed/borderless, pause
  and **mute on focus loss off** (Borderlands 2 `bPauseOnLossOfFocus`/`bMuteAudioWhenNotInFocus`),
  per-instance user dirs (DRG `-UserDir=`).
- Anything you edit that the real game also uses: `Janus.backup(path)` first (restored at
  session end; DDNet, Terraria's real `config.json`).
- **Music: one soundtrack, not N.** Mute music in every instance but the first, and remember
  the player's own volume so it comes back when that profile plays as P1 (Terraria/tModLoader
  `writeConfig`: `VolumeMusic = 0` for instance > 1, original kept in `janus-music-volume.txt`).
- Resolution memory outside config files: Unity keeps it in the registry
  (`HKCU\Software\<company>\<game>`, `Screenmanager Resolution Width_h182942802` etc.). The
  prefix isn't running before launch, so `play()` can edit `<prefix>/user.reg` as text
  (Gunfire Reborn `setUnityResolution`). Left alone, Unity in a portrait tile may pick
  gamescope's largest mode rotated (1080x960 in 960x1080), letterboxed.
- Check the **game actually fills the tile**: compare the game's screen size (Unity log, the
  game's own settings, or a screenshot's black bars) with the tile. Test at least one wide
  (1920x540) and one tall (960x1080) tile. Up to 8 players are supported (Big Picture
  `MAX_PLAYERS`); `Game.maxPlayers` is informational, set it to what the game's own lobby allows.
- **Say what each profile has saved: `Game.profileInfo(ctx)`.** The profile menus show a line
  per profile so players can pick the one with their saves; the default is the size of
  `ctx.saveRoot` + last change, which says little when the game's saves are small files inside a
  big prefix, live outside `saveRoot` (the owner's real saves, `saves: "real"`), or are named
  things the player recognises. Whenever the save location is known, implement it: list
  characters/worlds/save slots by name, a count, or hours/level read from a save or settings
  file (`Janus.glob`/`readJson`/`readFile` on the same paths the handler's `play()` uses — factor
  them into one function so they can't drift). Read-only (mutating `Janus.*` throws), cheap (runs
  for every profile each time a menu opens), return `null` to keep the default, at most ~60
  characters, and cover both the owner's real saves and Janus's own. Test with
  `save_info.profile_texts(...)` (*(checkout)*: see `tests/test_profiles.py`). SCRIPTING.md "Player profiles";
  example: `terraria.js` / `tmodloader.js`.

## 7. UI scale (small tiles)

In a 1920x540 tile most games draw their UI at half size. Options, cheapest first:

- **Nothing to do**: games whose UI is sized in pixels get bigger UI from the GUI's render
  scale (Settings → Game → Render scale). Most Unity/Unreal games scale UI with the screen, so this
  doesn't help them.
- **Unity Mono, Proton**: `Game.unityUiScale = true` (BepInEx 5 plugin, per-menu fit). Extras:
  `unityUiScaleGroups` (Content Warning's corner HUD), `unityUiScaleDisable`,
  `unityUiScaleIgnore` (see SCRIPTING.md). A HUD that doesn't scale while the menus do is
  usually a separate canvas: list every canvas with `JANUS_UI_DUMP=<seconds>` (set it so the
  dump happens once you're in game; the BepInEx `LogOutput.log` shows render mode, camera,
  target texture and scaler). A HUD drawn into a screen-sized texture is handled (Mycopunk).
- **Unreal**: `Game.unrealUiScale = true` (`Engine.ini` `ApplicationScale`, "size by height").
- **Unity IL2CPP** (BepInEx/MelonLoader need `global-metadata.dat`; often embedded/encrypted):
  inject `src/il2cpp_uiscale` (`janus_uiscale.dll`) with ColdClientLoader's
  `DllsToInjectFolder`, `JANUS_UI_SCALE`, and `JANUS_SCREEN_SIZE=WxH` to hold the game at the
  tile size. It works through the il2cpp runtime API, so it is game-agnostic for uGUI games;
  rebuild with `<janus>/src/il2cpp_uiscale/build.sh` (needs `gcc-mingw-w64-x86-64`; copy the
  output into your handler's payload).
  Log: `janus_uiscale.log` beside the DLL; `-v` adds why each menu stopped where it did.
- Whatever the mechanism, **verify per tile shape with screenshots**: main menu, lobby, and
  in-run HUD at 1920x540, 960x1080, 960x540, 640x1080 (three columns), 1920x1080, and for
  5–8 players 640x540 (3×2), 640x360 (3×3) and 480x540 (4×2). Look for
  text cut at the edges, HUD pieces covering each other's numbers, popups clipped. A fit that
  stops low has a reason in the log ("bigger would break: …") — decide whether the rule or the
  game is at fault before changing anything.
- **A scale that jumps around** while playing: the log shows the same canvas re-fitted again
  and again ("overflow" / "settled" with different factors). The culprit is named in "bigger
  would break": typically markers that follow world points and get pinned to the screen edge,
  a box sliding in, or a decoration hanging off screen by design. List those GameObject names
  in `Game.unityUiScaleIgnore` (Mycopunk: `Ping(Clone)`, `DialogueBox(Clone)`, `BKGFade`).
  Turn the camera in game while checking: edge markers only move then.

## 8. Input notes

- Proton games read pads through Wine (XInput/SDL over evdev); the hook gives each instance its
  own Steam virtual pad. `steamInputFilter` is only for native games that call ISteamInput.
- **Keyboard/mouse games in Steam's eyes** ("Controllers Not Supported" on the store page,
  or `virtualgamepadinfo.txt` stays empty while the game runs and every pad moves the
  desktop pointer): set `Game.requireGamepadLayout = true`. The launch then refuses until
  each player's pad has a Gamepad template for the game in Steam, and the game gets one
  Steam virtual pad per instance like any other (Sven Co-op, Gamble With Your Friends).
  If a pad has no layout yet, set it in Steam (it's per physical controller).
- If Steam Input can't work for a game, offer `steamInput` as an option that sets
  `JANUS_NO_STEAM_INPUT=1` + `requireSteamInput = false` (Gunfire Reborn), rather than turning
  it off for everyone.
- Some games lock to the first device that answers "press any key" (PAYDAY 2): say so in
  `launchMessage`.
- `Game.oskEnabled = true` for games that ask for text (lobby names, codes).
- Mouse-trapping games: `forceGrabCursor`/`grab`, Alt+1 releases focus (Nuclear Throne).
- Keyboard/mouse-only games: `Game.keyboardMouse` (Supermarket Together). It runs in GUI
  sessions only (`LauncherService`), not in CLI `steam-split`: test through a GUI or Big Picture.
  Look for the game's own pad support first, even if half-hidden (DDNet: `inp_controller_*`, binds
  only): a stick emulating a mouse aims badly in games that clamp the cursor. `keepPad: true`
  keeps the game's pad and adds only the missing keys/clicks (DDNet: Start = Esc, RT = click).

## 9. Options and messages

- `Janus.addOption(key, {label, description, type: "bool"|"choice"|"string", choices, default})`;
  read with `ctx.options.key` in hooks, `Janus.getOption(key)` at top level (e.g. to switch
  `Game.*` fields or the launch message by option: Palworld, Factorio, Gunfire Reborn).
- `Game.launchMessage`: only what players must know **before** starting (how to join, which
  button, tutorial skip). Short.
- `Janus.warn` / `Janus.error` for problems the user must act on (missing tool, game update
  broke a patch); `Janus.log` for everything else.
- Try both splits (top/bottom vs side by side) once it runs; set
  `Game.sideBySideFirst = true` only if side by side is better.

## 10. Pitfalls when testing

- **Never `pkill -f <pattern>`** with a pattern that can appear in your own command line (the
  game's name, "gamescope", "fakepad"): it kills your shell mid-command. Kill by PID
  (`pgrep -x` on the exact process name, or the launcher's PID with `kill -INT`).
- Wine processes are named after the exe (`Gunfire Reborn.`), not always findable by their
  `.exe` command line; SIGKILLed sessions can leave **orphaned pressure-vessel containers**
  (`srt-bwrap` → `pv-adverb` → game) that keep running, hold the prefix and break the next
  launch. Check `ps -eo pid,lstart,comm` and `pstree -p` after every forced stop. Steam's own
  `steam-runtime-launcher-service` container is not yours.
- One game per prefix at a time: a leftover instance silently blocks a new launch on the same
  profile.
- A SIGKILLed launcher skips teardown: re-release Steam's forced layout
  (`steam steam://forceinputappid/413080` then `/0`) and check `~/.config/kwinrulesrc`.
- Steam rewrites `virtualgamepadinfo.txt` about a second after launch; the very first press
  can land before the hook follows it. Press again before concluding input is broken.
- Someone may be using the machine (Big Picture, Steam open): don't close their windows.

## 11. Document and finish

- Handler header comment: engine, what co-op path works and when it was validated, every
  non-obvious setting with its reason (the next person to change it reads this first).
- Payload binaries: a README beside them with their exact source/version and licence.
- Saves known? Then `Game.profileInfo` is implemented (section 6).
- Share it: upload it on the handler hub (https://janus-split-screen.org/upload/, with an
  account; a moderator approves it before others can install it).
- *(checkout)*: a dated NOTES.md section with the investigation (what failed and why, error
  strings, file paths, offsets), the validated setup and a `steam-split` command line in
  GAMES.md, CLAUDE.md only for new components, env vars or architecture; run
  `python3 tests/test_scripting.py`; commit only when the maintainer asks.

## Handler cookbook (where to look)

Get any of these from the hub to read them (`janus-cli hub get <appid>` → `~/.config/janus/games/`).

| Technique | Handler |
|---|---|
| Goldberg in-game invites | `peak.js`, `lethal-company.js`, `deep-rock-galactic.js` |
| ColdClient (game checks its steam_api), DLL injection, IL2CPP UI scale, Unity registry resolution | `gunfire-reborn/` |
| ColdClient, "press any key" input lock, renderer XML per tile | `payday-2/` |
| Real Steam ticket for Photon, binary patch of a game DLL | `content-warning.js` |
| SteamStub unpacking | `how-to-fish.js`, `palworld.js` |
| Local dedicated server, `killOnClose`, real Steam account | `palworld.js` |
| Per-profile saves with `isolation: "none"`, music muting, Host & Play server env | `terraria.js`, `tmodloader.js` |
| `Game.profileInfo` (save names in the profile menus) | `terraria.js`, `tmodloader.js` |
| Join-mode options, map creation with `Janus.run`, Factorio's own LAN | `factorio.js` |
| `-UserDir=` per player, save + config import, DLC config | `deep-rock-galactic.js` |
| Unreal UI scale | `deep-rock-galactic.js`, `rv-there-yet.js`, `kletka.js`, `palworld.js` |
| Unity Mono UI scale (BepInEx) | `how-to-fish.js`, `lethal-company.js`, `content-warning.js` |
| Explicit launch command (Java) | `necesse.js` |
| Scout runtime for old native games | `nuclear-throne.js` |
| GoldSrc listen server + `+connect` with no menus, mirror cfg/userconfig edits, `requireGamepadLayout` | `sven-coop.js` |
| Source engine, `steamInputFilter`, unfocused-pause/mute ini settings | `left-4-dead-2.js`, `sven-coop.js`, `borderlands-2.js` |
| Controller as keyboard/mouse | `supermarket-together.js` |
| Game's own pad support + a few keys (`keyboardMouse.keepPad`), game's own server via `Janus.spawn`, settings as command-line console commands | `ddnet.js` |

# Janus Split Screen Handler Scripting — Environment Reference

Status: **implemented** — CLI (`steam`, `steam-split`) **and** GUI. The GUI
treats the handler sheet as the default for every setting and stores only
explicit user overrides — see [GUI integration](#gui-integration).

Each game Janus launches needs a *handler script*: a JavaScript file that encodes
everything Janus needs to know about that game — a property sheet of launch
options (`Game.*` globals) plus optional lifecycle callbacks invoked at fixed
points in the launch pipeline. Handlers generalize what used to be hardcoded
per-game quirks (Necesse's JVM command, Lethal Company's LAN-broadcast
deadlock, Source's `-game` detection, Nuclear Throne's scout-era runtime).
The design is modeled on Nucleus Co-Op's Jint-based handlers.

- Engine: **QuickJS** (quickjs-ng) via the `quickjs-ng` Python binding,
  vendored in `vendor/quickjs/` (see its README). ~ES2023 JavaScript.
- Implementation: `src/host/scripting.py` (`HandlerScript`), hooks wired in
  `src/host/steam.py` (`launch_steam_game`), orchestration hooks in
  `src/host/main.py` (`cmd_steam`) and `src/host/split_session.py`.
- Unit tests: `python3 tests/test_scripting.py`.

## 1. Handler files

Handlers are named after the game and declare the Steam appid they handle
on a line of their own:

```js
// handlers/content-warning.js
Game.appid = 2881650;
Game.name = "Content Warning";
```

Janus finds a game's handler by reading that `Game.appid = <number>;` line
(a plain number literal at the start of a line; the script isn't run for
this). Search order, first match wins:

```
~/.config/janus/games/<game-name>.js       # hub installs and your own (flat form)
~/.config/janus/games/<game-name>/handler.js   # (directory form)
```

That is the only place an **installed** Janus (zip or .deb) looks: Janus ships
no handlers, they come from the handler hub, and you write your own or
modified copies there too. A git checkout also reads the repo's development
`handlers/` (same two forms), which wins over `~/.config/janus/games/` for the
same appid; both GUIs then warn which one was chosen
(`scripting.handler_conflict`). If two
files in one directory claim the same appid, the first in name order wins
and a warning is logged. Files from before the rename, named `<appid>.js`
(or `<appid>/handler.js`), still match without a `Game.appid` line. A
handler whose `Game.appid` disagrees with the game it is run for (e.g. via
`--handler`) logs a warning.

A handler may bundle a **payload** directory (mods, config templates, DLLs)
deployed at runtime with `Janus.deployPayload(dst)`:

```
handlers/<game-name>/handler.js  ->  handlers/<game-name>/payload/
<anything>/<game-name>.js        ->  <anything>/<game-name>.payload/
```

**Non-Steam games** (Minecraft through Prism Launcher) declare a string id
instead of an appid, read the same way: `Game.id = "minecraft";` (lowercase
letters, digits, `-`, `_`; a handler with a `Game.appid` is a Steam game even
if it also sets `Game.id`). The id is used wherever an appid would be:
`main.py steam minecraft …`, `~/.config/janus/games/minecraft.json`,
`profiles/<id>/minecraft/`. See [Non-Steam games](#13-non-steam-games).

`--handler PATH` overrides the search. **No handler → no launch**: Janus has
no default behaviour for a game, so `scripting.load_handler`,
`LauncherService.launch_config` and `steam.launch_steam_game` all refuse a
game without one (Big Picture lists such games as "No handler" and offers the
hub's). Get handlers from the handler hub (`main.py hub`, Big Picture Y). A *broken* handler (syntax error, hook exception
before spawn) **fails the launch loudly** by design — it never silently
degrades to defaults.

## 2. Execution model

The script file is evaluated **once per launch invocation** in a fresh
QuickJS context. Top-level statements set the property sheet
(`Game.* = value`) and declare GUI/CLI options (`Janus.addOption`).
Functions assigned to `Game.*` become lifecycle callbacks.

All data crosses the Python↔JS boundary as **plain data** (JSON): numbers,
strings, booleans, arrays, objects. `ctx` is a plain object; host
functionality is exposed only through the `Janus.*` API. `console.log` is
aliased to `Janus.log`; `console.warn`/`console.error` to `Janus.warn`.

Sandboxing: the script can only do what `Janus.*` exposes (no `require`, no
raw filesystem, no network), plus a memory limit
(`JANUS_SCRIPT_MEMORY_MB`, default 256). **There is no CPU watchdog**: the
quickjs binding cannot arm a time limit while Python callables are
registered ("Can not call into Python with a time limit set"), so a handler
stuck in an infinite loop hangs the launch — Ctrl-C it. The API itself is
powerful on purpose (file editing, process spawning — real handlers need
both, exactly as in Nucleus Co-Op); trust comes from reading handlers before
using them, not from the engine.

## 3. Configuration precedence

Lowest to highest:

1. **Built-in defaults** — current behavior with no handler.
2. **Handler sheet** (`Game.*`) — the per-game "correct" configuration.
3. **CLI flags** — a flag *changed from its argparse default* overrides the
   sheet. Limitation: explicitly passing a flag equal to its default (e.g.
   `--isolation none` when the sheet says `secondary`) cannot override the
   script — edit the handler (or pass `--handler` with a variant) instead.
4. **User environment** — a variable already exported in your real
   environment always beats the sheet. This covers `Game.extraEnv`
   entries (`HandlerScript.apply_env`). GUI launches write the GUI's Extra
   env into the environment explicitly, so the GUI's resolved values always
   win there.
5. **Per-instance mutation in `play(ctx)`** — `ctx.gameArgs` / `ctx.env`
   are the last word for computed values, by design.

The GUI's JSON config slots between 2 and 3: tri-state overrides
(Default/On/Off), with only explicit choices stored (`override_keys`). The
GUI's **Dry Run** prints the equivalent CLI; because that CLI reloads the
handler and the flag rule in 3 can't express "override back to the
default", it lists any such GUI choices in a `# NOT reproduced by this CLI`
comment instead of silently printing a command that behaves differently.

## 4. Property sheet (`Game.*`)

Unknown non-function keys get a load-time typo warning but are preserved
(handlers may stash their own state on `Game`).

### Wired everywhere (CLI paths, effective today)

| Global | Type | Maps to | Notes |
|---|---|---|---|
| `Game.appid` | int | handler discovery | **Required.** The Steam appid this handler is for. Must be a literal on its own line (`Game.appid = 550;`): discovery reads it without running the script. |
| `Game.name` | str | display | Shown in the `[script] Loaded handler` line. |
| `Game.minimumJanusVersion` | str | version check | The oldest Janus that can run the handler, e.g. `"0.2"` (numeric parts; `0.2` = `0.2.0`; a build suffix like `+git…` is ignored). A literal on its own line, read **before** the script runs (`src/host/version.py`): an older Janus refuses to load the handler ("needs Janus 0.2 or newer — update Janus"). The handler hub stores it per version, and Janus's hub client installs the newest version it can run, never offering a version it can't as an update. Janus's own version: `version.BASE` (`1.0.0` + `+dev` in a checkout, the `VERSION` file in an installed copy). |
| `Game.launchMessage` | str | launch message | Crucial information the players need **before** the game starts (e.g. "press a button on the controller, not the keyboard, at the title screen", or how to get into each other's game). Plain text, `\n` for line breaks. Big Picture shows it after Play (A = Play, B = back to the players screen; Start doesn't skip it); the desktop GUI asks Play/Cancel after Launch; the CLI prints it. Shown once per session, after the launch checks. Keep it short — it's read from the couch. Can depend on an option: set it after `Janus.addOption`, from `Janus.getOption(key)` (Factorio: one text per join mode). |
| `Game.isolation` | str | `--isolation` | `none`/`prefix`/`secondary`/`full`. |
| `Game.verbose` | bool | `-v` | |
| `Game.nvidia` | bool | `--nvidia` | |
| `Game.copyPrefix` / `Game.copyUserdata` | bool | `--copy-prefix` / `--copy-userdata` | |
| `Game.exeName` | str | `--exe` | Executable filename searched in the install dir. It also says which build the handler is written for: a `.exe` = the Windows build, anything else = the Linux build (same for the first token of `Game.launchCommand`). When Steam installed the other one (games with both builds get the Windows one when Proton is forced in Properties → Compatibility), every launch path refuses with how to fix it (`prereqs.build_problem`). A handler that copes with both builds itself defines `Game.onGameResolved` (no check then). |
| `Game.launchCommand` | str | `--launch-cmd` | Full command relative to install dir; overrides all detection. |
| `Game.gameArgs` | str | game args | shlex-split, **prepended** to CLI game args. |
| `Game.extraGamescopeArgs` | str | gamescope args | Appended when gamescope is in use (always in `steam-split`; only with `--gamescope` in `steam`). |
| `Game.useRuntime` | bool | `--no-runtime` | `false` = skip the Steam Linux Runtime. Honored by the GUI too (it has no runtime toggle of its own). |
| `Game.runtime` | str | runtime selection | Native games only: `"scout"` forces scout-on-soldier (scout-era games — even 64-bit ones like Nuclear Throne, which needs scout's `libcrypto.so.1.0.0`), `"soldier"`/`"sniper"` pin a runtime, `"none"` = bare. Default: auto (sniper/soldier; scout for 32-bit). |
| `Game.steamInputFilter` | bool | `--steam-input-filter` | Per-instance Steam Input vtable filter (`JANUS_STEAM_INPUT_FILTER`). |
| `Game.requireSteamInput` | bool | launch check | What the game's Steam setting (Properties → Controller; `UseSteamControllerConfig` in the account's `localconfig.vdf`) must be, checked by every launch path (CLI `steam`/`steam-split`, desktop GUI, Big Picture), which refuses with instructions otherwise — and, when the other input mode would work with the current setting, offers it for this launch ("Play with Steam Input / Janus input"; CLI: `--input`). Default `true`: **Janus input** needs **Disable Steam Input** (`"0"`), **Steam Input** mode needs **Enable Steam Input** (`"2"`). `false` = the game doesn't deal with Steam Input at all: any setting. `"disabled"` = Steam Input mode needs **Disable Steam Input** too, and the session doesn't force its Steam layout: for games whose Steam layout turns every pad into the shared desktop mouse/keyboard (Gamble With Your Friends); pair with `extraEnv: {JANUS_NO_STEAM_INPUT: "1"}`. An unreadable setting doesn't block. `src/host/steam_input.py`. |
| `Game.janusInput` | bool | input mode | Default `true`: the game works with **Janus input** (the default mode): the session's input daemon (`src/host/padd.py`) turns each player's pad into its own virtual Xbox 360 pad and each instance runs in a private `/dev` holding only that one (`src/host/input_ns.py`). `false` for a game that needs Steam Input (it only reads `ISteamInput`, or players rely on Steam layouts). `src/host/controller_input.py`. |
| `Game.steamInput` | bool | input mode | Default `true`: the game works with **Steam Input** mode (Steam's virtual pads, filtered per instance by the hook). `false` = Janus input only. The user picks between the allowed modes (`SessionConfig.input_mode`: desktop GUI Settings → Game → Controller input, Big Picture Y → Controllers, CLI `--input janus|steam`); with only one allowed there's no choice. |
| `Game.requireGamepadLayout` | bool | launch check | For games Steam treats as keyboard/mouse games ("Controllers Not Supported"): Steam's default layout for them drives the shared desktop pointer/keyboard, so every pad steers the focused tile and Steam makes no virtual pads. `true` = every launch path warns, naming the pads, unless each pad in the session has a **Gamepad** template for the game: the desktop GUI and Big Picture offer **Launch anyway** / Cancel (Big Picture: "Play anyway"), the CLI prints the warning and launches (Properties → Controller → layout → Templates → Gamepad). Steam stores that per physical controller in `steamapps/common/Steam Controller Configs/<account id>/config/configset_<vid>-<pid>-*.vdf` (`configset_N*<mac>.vdf` for Bluetooth Nintendo pads): `"<appid>" { "template" "…gamepad…" }`. Pads with no configset file or a workshop config aren't blocked (logged). `steam_input.check_gamepad_layout()`. Sven Co-op, Gamble With Your Friends. |
| `Game.goldberg` | bool | `--goldberg` | |
| `Game.goldbergNoOverlay` / `goldbergDisableLan` / `goldbergExperimental` / `goldbergDebug` | bool | matching flags | |
| `Game.profileSaves` | bool | Big Picture labels | With `isolation = "none"`: the handler keeps saves per player profile itself (under `ctx.saveRoot`). Without it, Big Picture labels a `none` game's tiles "Shared saves". Set by Terraria, tModLoader, Factorio. |
| `Game.goldbergLanguage` | str | goldberg config | Default `english`. |
| `Game.goldbergInvites` | bool | goldberg steam_settings | In-game invites join **running** instances: "Invite players" in any instance makes gbe_fork send a real lobby invite to every other instance, which joins via `GameLobbyJoinRequested`. Writes empty `auto_send_invite.txt` + `auto_accept_invite.txt` into every `steam_settings/` gbe_fork reads (next to the Goldberg lib, and under `GseAppPath` with the debug build). Needs `goldbergDisableLan = false`. Replaces `goldbergAutoJoin` for games that call `ActivateGameOverlayInviteDialog`. |
| `Game.unityUiScale` | bool | auto option + BepInEx | Unity (**Mono**, **Proton**) games: adds a **"UI scale"** handler option (`uiScale`, choices `"1.0"`…`"2.5"` step 0.1, default `"1.0"`) to both GUIs. Above 1.0, Janus builds the per-instance mirror (also without Goldberg), copies vendored BepInEx 5 (`vendor/bepinex/`) + the committed `vendor/unity_uiscale/JanusUiScale.dll` next to the exe, and sets `WINEDLLOVERRIDES=winhttp=n,b` + `JANUS_UI_SCALE`. The plugin enlarges every UI canvas up to the factor, shrinking each menu only as far as needed to stay on screen (never below the game's own size). At 1.0 nothing is installed. IL2CPP / native games: warning, launch without it. `src/host/bepinex.py`. |
| `Game.unityUiScaleGroups` | bool | with `unityUiScale` | **Experimental.** When a canvas can't take the whole UI scale (e.g. a HUD whose corner pieces sit at the screen edges), the plugin also scales the canvas's separate element groups individually, each growing from its screen edge toward the centre, as far as it stays on screen and doesn't overlap the others (`JANUS_UI_GROUPS=1`). Off by default; Content Warning's helmet HUD uses it. |
| `Game.unityUiScaleDisable` | [string] | with `unityUiScale` | Game `MonoBehaviour` type names to switch off on UI canvases, for scripts that keep re-positioning UI and undo the fit (Content Warning: `["AspectRatioUISetter"]`, which applies saved per-aspect HUD positions every frame) (`JANUS_UI_DISABLE`). |
| `Game.unityUiScaleIgnore` | [string] | with `unityUiScale` | UI GameObject names whose graphics (and children) never limit the fit: markers that follow world points and get pinned to the screen edge, decorations meant to hang off screen. Without it each such move shrinks and regrows the canvas (Mycopunk: `["Ping(Clone)", "BKGFade", "DialogueBox(Clone)"]`) (`JANUS_UI_IGNORE`). |
| `Game.unrealUiScale` | bool | auto options + Engine.ini | Unreal (UE4/UE5; Proton or native Linux) games: adds **"Size UI by window height"** (`uiScaleByHeight`, default on → `UIScaleRule=Vertical`: UE sizes UI from the shortest side, which in a side-by-side tile is the width) and **"UI scale"** (`uiScale` 1.0–2.5 → `ApplicationScale`). Written to `[/Script/Engine.UserInterfaceSettings]` of the instance's user `Engine.ini` (Saved dir from a `-UserDir=` game arg, else `AppData/Local/<Project>/Saved` in the instance prefix, or `~/.config/Epic/<Project>/Saved` in the instance home for native builds; `Config/Windows[NoEditor]` / `Linux[NoEditor]`), backed up and restored at session end. One factor for all UI — no per-menu fit (see `unrealUiFit`). `src/host/unreal.py`. |
| `Game.unrealUiFit` | bool | with `unrealUiScale`, UE4SS | **Proton** Unreal games: makes "UI scale" a **maximum fitted per screen**, like the Unity plugin. Above 1.0 Janus builds the per-instance mirror, copies vendored UE4SS (`vendor/ue4ss/`, `dwmapi.dll` proxy, `WINEDLLOVERRIDES=dwmapi=n,b`) and the Lua mod `src/unreal_uifit/main.lua` next to the `*-Shipping.exe` (set `Game.exeName` to it) and passes the factor as `JANUS_UI_SCALE`; `ApplicationScale` is then not written to Engine.ini, nor `UIScaleRule` ("Size UI by window height" is ignored while the fit runs: the fit grows the UI from the game's own size as far as each screen fits). Twice a second the mod computes every visible widget's on-screen rect from canvas-slot anchors/offsets and desired sizes and sets `ApplicationScale` to the largest value that keeps them on screen without new HUD overlaps (never below 1.0). Engine builds UE4SS can't scan by itself need the upstream per-game config (`UE4SS-settings.ini` + `UE4SS_Signatures/` from its `zCustomGameConfigs.zip`) in the handler's `payload/ue4ss/`, copied over the defaults. Log: `ue4ss/UE4SS.log` beside the exe in the mirror (`[JanusUiFit]` lines; every screen with `-v`). |
| `Game.bepinexPlugins` | [path] | BepInEx | Extra BepInEx 5 plugin DLLs to deploy (relative to the handler's payload dir, or absolute) into `BepInEx/plugins/` — same deployment and checks as `unityUiScale`, always on when listed. A BepInEx already in the real install is used (copied into the mirror) instead of the vendored one. |
| `Game.goldbergRealTicket` | bool | Goldberg mirror + host helper | Hybrid auth for **64-bit Proton** Goldberg games whose backend validates the Steam session ticket with Valve (Content Warning's Photon app). The mirror's Goldberg `steam_api64.dll` becomes `steam_api64_gbe.dll` behind a proxy (committed `vendor/ticketproxy/steam_api64_ticket.dll`; source `src/ticketproxy/`) that forwards everything except `SteamAPI_ISteamUser_GetAuthSessionTicket`, whose bytes it replaces with a real ticket from `janus_ticket.bin`. `src/host/steam_ticket.py` writes that ticket from the running Steam client (account must own the game) and keeps it open until the game exits. Lobbies/SteamIDs stay on Goldberg. |
| `Game.steamStubExes` | [str] | per-instance mirror | Install-relative `.exe`s wrapped in **SteamStub DRM** (PE section `.bind`, check with `scripts/pe_sections.py`), which otherwise relaunch the game through real Steam. Janus unpacks each with the vendored Steamless (under a Proton's Wine; first time ~1 min) into `~/.local/share/janus/steamless/<appid>/`, redone when the original changes, and the per-instance mirror (made even without Goldberg) gets the unpacked copy at the original's path. The Steam install is never written. An exe that's no longer wrapped is used as is; a failed unpack aborts the launch. `src/host/steamstub.py`. |
| `Game.mirrorCopyInstead` | [str] | goldberg mirror | Install-dir-relative paths **copied** into the mirror instead of symlinked (games that write inside their install dir). steam_api files inside a copied tree are NOT goldberg-replaced. |
| `Game.mirrorExclude` | [str] | goldberg mirror | Paths omitted from the mirror entirely. |
| `Game.staggerSeconds` | num | instance stagger | `steam-split` delay between instance launches (default 3.0). |
| `Game.extraEnv` | {k:v} | spawn env | Applied unless the variable is already in the user's environment (GUI Extra env included). |
| `Game.sideBySideFirst` | bool | default split | Both GUIs offer **top and bottom** before side by side (desktop split button; Big Picture's first 2- and 3-player layout = the default). `true` = side by side first, for games whose UI suits tall tiles. A layout the user picked is remembered either way. |
| `Game.reflow` | bool | `true` | **Dynamic players**: when one player's game quits (or is removed from Big Picture's running screen) while others play on, the remaining tiles are moved/resized to the layout for one player fewer (Big Picture: the layout remembered for that count, else the default). Each game keeps its resolution and is scaled into its new tile (letterboxed). `false` = the tiles stay where they are. |
| `Game.lateJoin` | bool | `true` | **Dynamic players** (both GUIs): a controller that isn't playing joins the running session by holding **Start** for 1.5 s (with Steam Input only if nobody plays an identical pad), up to `Game.maxPlayers`: it gets a virtual pad, a new instance (lowest free instance number, never 1: that's the host role, the profile it played last if free) starts and every tile re-flows to the layout for one more player. With `Game.goldbergAutoJoin` it is launched with the host's lobby. `false` = nobody joins mid-session (for games whose instances can't meet once started). |
| `Game.resizeGame` | string | none | **Dynamic players**: how the game follows a tile that changes size (else it keeps its resolution, letterboxed). `"window"`: Janus resizes the game's X window inside gamescope (native games that re-lay out on a resized window: Terraria/FNA). `"unity"`: the JanusUiScale BepInEx plugin (installed even at UI scale 1.0) calls `Screen.SetResolution` and keeps the game windowed (Proton Unity Mono games). `"unreal"`: UE4SS + the JanusUiFit Lua mod (installed even without the UI fit) run `r.SetRes <w>x<h>w` (Proton Unreal games). `"file"`: only `$JANUS_SIZE_FILE` (`WxH`, rewritten on every re-flow) for the handler's own mod. With any of them gamescope's own screen is monitor-sized and the game's window is its tile, so the game must start windowed at `ctx.width`×`ctx.height`. |
| `Game.keyboardMouse` | object | none | For **keyboard-and-mouse-only games** (Janus input only): each player's controller drives a keyboard and mouse *inside their own gamescope* (`src/host/kbm.py`: XTest into that instance's X server, so nothing reaches the desktop or the other tiles, focus or not); the game gets no pad. `{leftStick: "wasd"\|"arrows"\|"mouse"\|null, rightStick: "mouse"\|…, mouseSpeed: 1400 (px/s at full tilt), deadzone: 0.2, keepPad: false, buttons: {a, b, x, y, lb, rb, lt, rt, back, start, guide, l3, r3, up, down, left, right: target}}`; a target is an X keysym name (`"space"`, `"Escape"`, `"Shift_L"`, `"Tab"`, `"e"`), `"mouse1"`/`"mouse2"`/`"mouse3"` (left/middle/right button), `"wheelup"`/`"wheeldown"` (repeats while held) or `"type:<text>"` (types it on press, for names a pad can't type). Set `Game.steamInput = false` with it. `keepPad: true`: the game **also** gets the pad (its private `/dev` keeps the virtual pad), for games with their own pad support that only lack a few keys or clicks (DDNet: Start = Esc, RT = click; set unused sticks to `null`). |
| `Game.unityRunInBackground` | bool | false | Unity (Mono, Proton): the JanusUiScale plugin keeps `Application.runInBackground` on (a Unity game that pauses while unfocused would freeze in every tile but the focused one). |
| `Game.killOnClose` | [str] | session teardown | `pkill -f <name>` for each entry at `finish()`. |

### GUI-session fields (drive GUI sessions; inert on the CLI)

`Game.borderless`, `Game.hidePanel`, `Game.forceGrabCursor`,
`Game.grab`, `Game.kwinKeepAbove`, `Game.kwinAcceptFocus`,
`Game.kwinDisableFsp`, `Game.oskEnabled`, `Game.goldbergAutoJoin`,
`Game.goldbergHostInstance` — these default the matching SessionConfig
fields in GUI sessions (tri-state overrides on top). The CLI has no
equivalent flags, so there they parse but do nothing.

`Game.lobbyConnectArg` (GUI sessions): template for the auto-join args
appended to non-host instances, default `"+connect_lobby %LOBBY%"`
(`%LOBBY%` = detected lobby ID). For full control define
`Game.onLobbyDetected` instead. `Game.maxPlayers` is informational for now.

## 5. Lifecycle callbacks

All optional; plucked off `Game` after evaluation. Per-instance callbacks
receive `ctx` (section 6). An exception in a **pre-spawn** hook aborts that
launch loudly; **post-spawn** hooks (`onInstanceStarted` onward) log the
error and continue. Hook invocations are serialized (thread-safe).

Firing order per instance, with the exact seam in `launch_steam_game`
(`src/host/steam.py`):

| Callback | Fires | May do |
|---|---|---|
| *(top level)* | Script load, before anything | Set sheet, `Janus.addOption`. No `ctx` here. |
| `Game.onGameResolved(info)` | After `SteamGame` manifest resolution. `info = {appid, gameName, installDir, isNative, isProton, is32bit}` | Return `{exeName: ..., launchCommand: ...}` to override either based on what's installed. |
| `Game.buildEnvironment(ctx, env)` | After `build_environment()` and the sheet's env toggles. `env` is the complete spawn environment | Mutate `env` freely (add/change/delete) — the mutated copy becomes the spawn env. Highest-leverage hook: nearly every Janus feature is an env var. |
| `Game.onMirrorReady(ctx)` | After mirror creation (Goldberg, or BepInEx via `unityUiScale`/`bepinexPlugins`; BepInEx itself is deployed after this hook). `ctx.gameDir` now points at the mirror; relative `Janus.*` paths resolve against it | Install payloads/mods/config into the mirror. The mirror is **wiped and rebuilt every launch** — files written before this hook are destroyed. |
| `Game.buildCommand(ctx)` | At command build, before runtime wrapping | Return an argv array (first token resolved relative to the install dir, then runtime-wrapped) to replace exe detection + `launchCommand` + auto args. Return nothing for default behavior. |
| `Game.play(ctx)` | Immediately before spawn — the workhorse | Push to `ctx.gameArgs` (appended to the final command line), set `ctx.env` (merged into spawn env), edit files with tile geometry via `Janus.*`. |
| `Game.onInstanceStarted(ctx)` | Right after `Popen`; `ctx.pid` set | Companion processes, logging. Soft (errors ignored). |
| `Game.onAllStarted(session)` | `steam-split`: all instances launched. `session = {appid, numPlayers, pids}` | Session-wide post-setup. Soft. |
| `Game.onInstanceExit(ctx)` | Instance exited; `ctx.exitCode` set | Cleanup, save syncing. Soft. |
| `Game.onResize(ctx)` | The tiles re-flowed (a player joined or left; dynamic players), once per instance still playing; `ctx.width`/`height` = its new render size, `ctx.posX`/`posY` its tile | Resize the game yourself (a mod, a config file) when `Game.resizeGame` doesn't cover it. Soft. GUI thread. |
| `Game.onStop(session)` | Session end (normal exit or Ctrl-C), before `finish()` | Final cleanup. Soft. |
| `Game.onLobbyDetected(ctx)` | GUI sessions with Goldberg auto-join: fires when the lobby watcher detects the host's lobby; `ctx = {appid, instance, lobbyId, numPlayers}` | Return `false` to launch the queued instances with **no** join args; return an argv array to replace `Game.lobbyConnectArg`; return nothing for the default. Soft. |

After `onStop`, the host runs `HandlerScript.finish()` automatically:
`Janus.backup` files are restored (reverse order), `Janus.spawn` helpers are
killed, `Game.killOnClose` names are pkilled.

**Caveat**: teardown lives in the *launcher process* (`main.py`). Stop a
session with Ctrl-C / SIGINT to the launcher — SIGKILLing it (or killing it
via a pattern that matches its own `--gamescope=` argument…) skips restore.

## 6. The `ctx` object

Plain data, built per instance, updated between hooks:

| Field | Meaning |
|---|---|
| `ctx.appid`, `ctx.gameName` | Identity. |
| `ctx.instance` | 1-based instance number; `ctx.rank` = `instance - 1` (== `JANUS_INSTANCE_RANK`). The **session role** only (host, music, ports): never build a save path or an identity from it — see `ctx.profile`. |
| `ctx.profile` | Who plays this instance: `{id, name, steamId, owner}`. `owner` = the real Steam account (only it plays on the real saves). `steamId` is fixed per profile, so saves a game keys by Steam ID follow the person. Goldberg already uses `name`/`steamId`. See "Player profiles" below. |
| `ctx.saveRoot` | Host path of this profile's storage for this game (`~/.local/share/janus/profiles/<id>/<appid>`, also holding the isolated `home/`/`compatdata/` when Janus isolates). Keep per-player handler data here (create subdirs; avoid `home`, `tmp`, `var`, `compatdata`). |
| `ctx.profilesDir` | `~/.local/share/janus/profiles` (to look at other profiles' data, e.g. `${ctx.profilesDir}/*/${appid}/…`). |
| `ctx.players` | Everyone in the session: `[{instance, profile}]`. Only this instance for a single `steam` launch. |
| `ctx.numPlayers` | Tile count in `steam-split`; `null` for single `steam` launches (each instance is its own invocation). |
| `ctx.controller` | `{index, vid, pid, name}` — assigned pad (identity fields 0/null when unknown). In Janus input mode the physical pad; the game sees a virtual Xbox 360 pad. |
| `ctx.inputMode` | `"janus"` or `"steam"`: how this launch's controllers reach the game (see `Game.janusInput`). |
| `ctx.width`, `ctx.height`, `ctx.posX`, `ctx.posY` | Tile geometry. `steam-split`: from `--tile`. `steam`: width/height from `--gamescope` `-w`/`-h` when given, else `null`; no position. |
| `ctx.installDir` | Real game install dir (treat as read-only). |
| `ctx.gameDir` | Effective game dir: the Goldberg mirror once created, else `installDir`. Relative `Janus.*` paths resolve against this. |
| `ctx.home` | Effective `$HOME`: the isolated instance home when isolation applies, else the real home. |
| `ctx.winePrefix` | Proton: `<compatdata>/pfx` for this instance (isolation-aware). `null` for native games. |
| `ctx.isNative`, `ctx.isProton`, `ctx.is32bit` | Platform flags. |
| `ctx.options` | Resolved `Janus.addOption` values (defaults + `-o` overrides). |
| `ctx.env` | Written by hooks: merged into the spawn environment after `play`. |
| `ctx.gameArgs` | Written by hooks: appended to the final command line after `play`. |
| `ctx.pid` | Set from `onInstanceStarted` onward. |
| `ctx.exitCode` | Set in `onInstanceExit`. |
| `ctx.lobbyId` | Reserved (null until lobby wiring lands). |
| `ctx.verbose` | Session verbose flag. |

Only `ctx.gameArgs` and `ctx.env` mutations flow back to the host; other
fields are snapshots.

## 7. Host API (`Janus.*`)

Any `Janus.*` failure throws a catchable JS `Error` carrying the Python
error (`Janus.readFile: FileNotFoundError: ...`).

**Paths**: absolute, or `~`-expanded, or resolved relative to `ctx.gameDir`
(mirror-aware; install dir before the mirror exists).

Logging & options:
- `Janus.log(...args)` — prints `[script] ...` to the launch log.
- `Janus.warn(...args)` — tell the **user** about a problem: logged as
  `[script] WARNING: ...` and, in the GUI, shown as a **modal** warning
  dialog: the call blocks (and with it the hook and every other instance's
  hooks) until the user closes it, then the script continues. Identical
  messages are shown once per session (so a warning raised by every
  instance's hook pops up once). Allowed at load time too (shown, blocking,
  when the session launches). Stop All while a box is open makes the
  blocked call throw, aborting that hook. On the CLI it only logs (no wait).
- `Janus.error(...args)` — like `warn` (critical dialog), then **throws**,
  aborting the current hook — so from `play`/`buildCommand`/... the
  instance's launch fails. In post-spawn hooks (`onInstanceStarted`,
  `onAllStarted`, `onInstanceExit`, `onStop`) the throw is logged and
  ignored. Use for "cannot possibly work" conditions; prefer `warn` when
  the game is still usable. Palworld errors from every instance's `play`
  when its dedicated server is selected but not installed, so no instance
  starts (one dialog: identical messages are shown once).
- `Janus.addOption(key, {label, description?, type, choices?, default})` —
  declare a per-game option. `type`: `"choice"` (validates against
  `choices`), `"bool"` (`1/true/yes/on`), `"int"`, `"string"`. Load-time
  only. Users set values with `-o key=value` (repeatable); resolved values
  appear in `ctx.options` / `Janus.getOption(key)`. Unknown `-o` keys warn
  but pass through.

Text files (UTF-8):
- `Janus.readFile(p)` / `Janus.writeFile(p, text)` (creates parent dirs)
- `Janus.readLines(p)` / `Janus.writeLines(p, lines)`
- `Janus.findLine(p, regex)` → 1-based line number or -1
- `Janus.replaceLine(p, regex, newLine)` → replaces the first matching line, returns its number or -1

INI (line-preserving; section `""` = keys before any `[section]` header):
- `Janus.iniGet(p, section, key)` → value or null
- `Janus.iniSet(p, section, key, value)` — edits in place, appends key/section if missing

JSON:
- `Janus.readJson(p)` / `Janus.writeJson(p, obj)`
- `Janus.jsonSet(p, "dotted.key.path", value)` — creates intermediate objects

Filesystem:
- `Janus.exists(p)`, `Janus.glob(pattern)` (recursive `**` supported), `Janus.mkdir(p)`
- `Janus.appInstallDir(appid)` — install dir of another Steam app (a dedicated
  server, the base game of a mod…) in **any** Steam library, or `null` if it isn't
  installed. Don't derive it from `ctx.installDir`: Steam may put it in another library.
- `Janus.copy(src, dst)` (file or tree), `Janus.move(src, dst)`, `Janus.symlink(target, link)`
- `Janus.remove(p, {force?})` — refuses paths outside Janus-managed roots
  (`ctx.gameDir`, install dir, `~/.local/share/janus`, `~/.config/janus`,
  `/tmp`) unless `{force: true}`
- `Janus.deployPayload(dst)` — copy the handler's payload tree into `dst`
- `Janus.binaryPatch(p, findHex, replaceHex)` — equal-length hex
  find/replace over the whole file, returns match count
- `Janus.backup(p)` — snapshot now, **auto-restored at session end** (also
  restores "file did not exist"). Use before any edit you don't want to
  survive the session (see the DDNet handler).

Processes (argv arrays only — no shell strings, by design):
- `Janus.run(argv, {cwd?, env?, timeout?=20})` → `{code, stdout, stderr}`
- `Janus.spawn(argv, {cwd?, env?})` → pid; killed automatically at session end

Actions and the web (mainly for non-Steam games' setup, section 13; the
network calls are refused at load time, like the mutating ones):
- `Janus.addAction(key, {label, description?, menu?=true, run: function (ctx) {...}})`
  — load time only. A button in the game's options (desktop GUI: Settings →
  Game → Setup; Big Picture: Y → Maintenance) when `menu` is true, and/or a
  setup step's fix (`Game.setupCheck` names it). `run(ctx)` gets the setup
  ctx (section 13) and may return a string, shown to the user when it ends.
  `Janus.error` inside it fails the action with exactly that message (no JS
  stack; any other exception is shown as `<label>: TypeError: …`). Runs on a
  worker thread behind a progress popup; CLI: `main.py setup GAME --action KEY`.
  A setup step that fails offers **Try again** (both GUIs); in Big Picture, B
  there (or on any setup step reached by picking the game) goes back to the
  games list, so nobody joins players for a game that can't start.
- `Janus.progress(text)` — the progress popup's text while an action runs
  (logged too).
- `Janus.fetchJson(url, {timeout?=30})` → the parsed JSON (http/https only).
- `Janus.download(url, dst, {sha1?, sha256?, sha512?, label?, timeout?=60, retries?=4})`
  → `dst`'s path. Through a temp file renamed on success. A dropped
  connection (checked against Content-Length) is resumed with a Range request
  (or restarted if the server ignores it); network errors, HTTP 5xx/408/429
  and a hash mismatch are retried, then it throws with the reason. HTTP 4xx
  throws at once. Reports progress every 5 % (`Downloading <label>: 40%`).
- `Janus.extract(archive, dst)` — unpack a `.tar.*`/`.tgz`/`.zip` into `dst`;
  refuses entries and symlinks that would land outside `dst`.

## 8. File editing vs. pressure-vessel visibility

The script engine runs in the **launcher process on the host** — never
inside the pressure-vessel container. `Janus.*` file operations therefore
see the host filesystem with the user's full permissions; the container
never restricts what a script can *edit*. What matters is whether the game
will *see* the edit — i.e. whether the path is mapped into the container:

| Host path | Visible to the game? |
|---|---|
| `ctx.installDir` / `ctx.gameDir` (mirror) | Yes — on `PRESSURE_VESSEL_FILESYSTEMS_RW`. |
| `ctx.home` | Yes — for isolated instances this is the *host* path of the isolated home (`~/.local/share/janus/profiles/<id>/<appid>/home`), mounted as `$HOME` in the container. Build home-relative paths from `ctx.home`, never `~`, and they work in every isolation mode (see the DDNet handler). |
| `ctx.winePrefix` | Yes — host path of the instance's compatdata `pfx/`, shared into the container (smart-mode prefixes bind-mount the source compatdata too). |
| Real `~` while home isolation is on | Editable, but **hidden from the game** — the container only sees `PRESSURE_VESSEL_HOME`. Writing `~/.config/...` for an isolated instance silently does nothing in-game. |
| `/tmp` | Shared only when *not* isolating; isolated instances get a private tmp (`TMPDIR` → the instance's `var/tmp`). |
| Container-internal paths (runtime `/usr`, overlays) | Unreachable — the script has no view inside the vessel. |

Timing follows the same rule: pre-spawn hooks edit files before the
container exists; post-spawn hooks (`onInstanceStarted` etc.) edit live,
which works for bind-mounted paths (same files) but not unshared ones.
**Rule of thumb: derive every path from `ctx.gameDir`, `ctx.home`, or
`ctx.winePrefix` — they are isolation-aware; a hardcoded `~` is the trap.**

### Player profiles

A profile is a person ("Alice"), independent of the player number. Janus
stores per-player data by profile: the isolated home / Wine prefix live in
`profiles/<id>/<appid>/`, the Goldberg account is the profile's name and
Steam ID, and `secondary` isolation leaves **only the owner** (the real Steam
account) on the real saves, whatever their player number. So a handler:

- decides "real saves or not" with `ctx.profile.owner`, never `ctx.instance === 1`;
- keeps its own per-player data under `ctx.saveRoot`, never `…/${ctx.instance}`;
- takes names/IDs from `ctx.profile`, never `"Player " + ctx.instance`.

`ctx.instance` stays right for session roles (the host, music in one
instance only, which port to join). When per-player state lives in a file a
profile carries to any number (Terraria's config.json), make role-based
edits reversible (Terraria remembers the music volume it muted for P2+).
Isolation `none` games share all data: the profile is then only a name/ID,
unless the handler separates saves itself (and sets `Game.profileSaves`).

**`Game.profileInfo(ctx)` — the save line in the profile menus.** Both GUIs
list every profile with a line saying what it has saved for the game, so a
player can tell which profile holds the saves they want (Big Picture: X on
the players screen; desktop: right-click a slot → Profile). The default line
(`src/host/save_info.py`) is the size of `ctx.saveRoot` (symlinks not
followed, `tmp/` skipped) and when it last changed — `1.4 GB · played 3 days
ago`, `No saves yet` — or `Real Steam saves` / `Shared saves` when the profile
plays on the real or shared data. It's an estimate: an isolated Wine prefix
alone is a few MB. Return a string to replace it (characters, worlds, hours
played), `null` for the default. Called for every profile each time a menu
opens, on its own copy of the handler (Big Picture: on a worker thread), so
it is **read-only**: `writeFile`, `run`, `spawn`, `download` and the other
mutating `Janus.*` calls throw (`readFile`/`readJson`/`glob`/`exists` are
fine). An exception keeps the default line. Its `ctx` (no instance):

| Field | Meaning |
|---|---|
| `profile`, `saveRoot`, `profilesDir`, `home`, `options`, `appid`, `gameName`, `installDir` | As in a launch's ctx (`installDir`: null when the game isn't installed). |
| `saves` | `"real"` (the owner on the real saves), `"shared"` (isolation `none`, no `profileSaves`) or `"own"`: what the session's isolation means for this profile. |
| `saveSize` / `lastPlayed` | Bytes under `saveRoot` and the newest file's mtime (Unix seconds); `null` when it doesn't exist. |
| `defaultText` | The line Janus would show, to extend (`` `3 worlds · ${ctx.defaultText}` ``). |

`handlers/terraria.js` and `handlers/tmodloader.js` list the profile's characters
and world count from the saves their `saves` option gives it.

Deleting a profile (Big Picture: Back on it in the panel, desktop: Profile →
Delete profile) removes it **and everything under `profiles/<id>/`** for every
game, after the user types `delete`; the owner and a profile another player
is using can't be deleted, nor anything while games are running. Real saves
are never touched.

## 9. CLI usage

```bash
# Handler picked up automatically (handlers/necesse.js supplies the JVM
# command, steam-input filter and isolation — no flags needed):
python3 src/host/main.py steam 1169040 -c 0

# Two-tile split, same handler:
python3 src/host/main.py steam-split 1169040 --tile 0:0,0,1920,2160 --tile 1:1920,0,1920,2160

# Handler options / explicit path:
python3 src/host/main.py steam 1966720 -c 0 -o mods=yes
python3 src/host/main.py steam 550 -c 0 --handler /path/to/experiment.js

# Non-Steam game: set it up (asks before each fix; -y doesn't), or run an action
python3 src/host/main.py setup minecraft
python3 src/host/main.py setup minecraft --action update
python3 src/host/main.py steam-split minecraft --tile 0:0,0,1920,540 --tile 1:0,540,1920,540
```

Env vars: `JANUS_SCRIPT_MEMORY_MB` (default 256).

## 10. Shipped handlers (all tested 2026-08-30 unless noted)

| Handler | What it encodes | Verified |
|---|---|---|
| `handlers/ddnet.js` DDNet | DDNet's own pad support (absolute aim, binds on the command line) + `Game.keyboardMouse` `keepPad` for Start = Esc / RT = click; `play` starts the bundled `DDNet-Server` on 127.0.0.1 (`Janus.spawn`) and passes size, binds, name and `connect` as console commands on the command line; the owner's `settings_ddnet.cfg` `Janus.backup`'d | 2-tile session 2026-11-22 (fake pads): both on the local server, each pad drives only its tile, menus; online server |
| `handlers/necesse.js` Necesse | `launchCommand` (JVM), `steamInputFilter`, isolation | single + 2-tile `steam-split`, JVM survives the steamworks4j crash window |
| `handlers/left-4-dead-2.js` L4D2 | `steamInputFilter`, `-novid`, `play` adds `-w/-h` from tile | launch |
| `handlers/nuclear-throne.js` Nuclear Throne | `exeName` (bypasses `run.sh`, which hardcodes `DISPLAY=:0`), **`runtime: "scout"`** (64-bit but needs scout's `libcrypto.so.1.0.0`), gamescope `-g --force-grab-cursor` | launch (fails without the handler) |
| `handlers/rv-there-yet.js` RV There Yet? | `-dx11` (D3D12 rubber-bands under gamescope/NVIDIA) | launch; UE log shows `Command Line: -dx11` |
| `handlers/lethal-company.js` Lethal Company | `goldberg` + `goldbergDisableLan` (netcode-deadlock fix), Unity screen args from tile | launch; mirror + `disable_lan_only=1` written with no CLI goldberg flags |
| `handlers/peak.js` PEAK | isolation defaults only | not individually launched (identical path to other Proton games) |
| `handlers/deep-rock-galactic.js` Deep Rock Galactic | Goldberg invites, per-profile `-UserDir=` in the prefix (save/config copied in once; save named by the profile's Steam ID), `mirrorExclude` for `FSD/Saved` + intro movies, `-dx11 -nohmd -nosplash -disablemodding`, tile `-ResX/-ResY` + ini, `onMirrorReady` writes `configs.app.ini` (`unlockAllDlc`, default off) | 2-tile `steam-split` 2026-10-08: invite → join → in host's rig |
| `handlers/factorio.js` Factorio | Goldberg with per-profile `saves_folder_name` (Goldberg's emulated cloud holds player-data.json) seeded with the profile name as LAN name, `runtime: "none"` + `LD_LIBRARY_PATH` to the mirror's Goldberg lib, per-profile `-c config.ini` write-data (`ctx.saveRoot`), join mode `auto` (default): instance 1 `--host` (LAN-only `--server-settings`); `menu`: host picks a save in Factorio's Host dialog (preset via config.ini `[multiplayer-lobby]`), others are held in `play()` until it hosts, then `--mp-connect`; `buildEnvironment` drops `SteamAppId` so Steam doesn't apply Factorio's Steam Input config to the tiles | 2× `main.py steam`, headless gamescope 2026-10-08 (both modes); 2-tile KWin session: join + controllers |
| `handlers/terraria.js` Terraria, `handlers/tmodloader.js` tModLoader | Same code in both (tModLoader adds its launch). Goldberg identity = the player's profile via `GseAppPath` steam_settings + a separate one for the Host & Play server (`JANUS_ARGV_ENV_MATCH=-autoshutdown`), `JANUS_GOLDBERG_INVITE_FIX` (gbe skips every invitee when the inviter is in no lobby), `saves` firstReal (the Steam account's player real, other profiles own saves) / isolated / copy-once, incl. Steam Cloud, under `ctx.saveRoot`; config.json (`Janus.backup`'d when real) always gets the tile size, windowed, and `VolumeMusic=0` for instances 2+ (restored when that profile plays as P1). tModLoader: `dotnet/dotnet tModLoader.dll`, .NET installed in `onGameResolved`, `mirrorCopyInstead` keeps Valve's md5-checked `libsteam_api.so`, `[app::paths]` for Terraria assets + workshop | 2-tile sessions 2026-10-09 (both games): Join via IP, Invite Friends, instance 2 music off |
| `handlers/borderlands-2.js` Borderlands 2 | Native 32-bit Aspyr port: Goldberg + `onMirrorReady` writes `steam_interfaces.txt` (old SDK; crashes without), `configs.app.ini` listing the DLC with installed depots in the app manifest (`unlockDlc`, default on; listing all DLC crashes after character select), `mirrorExclude` of `megaintro.bik` (`skipNewGameIntro`, default on; works only because `mirrorCopyInstead` makes the binary + `steamassets/binaries` real copies — the game resolves its data dir through them), `play` sets `GseAppPath` to the mirror for the regular build, `-nostartupmovies -windowed -ResX/-ResY`, owner's `willowengine.ini` backed up | 2-tile `steam-split` 2026-10-16: both reach the menu, identities + friends correct |
| `handlers/payday-2/` PAYDAY 2 | Valve's `steam_api64.dll` kept (`mirrorCopyInstead`, the exe verifies its DLLs) + gbe_fork ColdClientLoader/`steamclient64.dll` payload deployed into the prefix and launched via `buildCommand`; `-steammm`; per-tile `renderer_settings_dx11.xml`; `unlockAllDlc` | 2-tile `steam-split` 2026-10-16: both pads, lobby invite → join. Press a pad button at "Press any key" (a click/key locks the instance to keyboard+mouse) |

`mirrorCopyInstead` / `mirrorExclude` / `goldbergLanguage` were verified directly against the
Lethal Company install (real copies in the mirror, exclusions absent,
identity INI written).

## 11. GUI integration

Implemented ("script is default, JSON stores overrides"):

- **Tri-state settings** (`src/gui/widgets/global_settings.py`): every
  session setting defaults to the handler sheet's value (built-in default
  when absent). Checkboxes are tristate — the square *partial* state means
  "default (from handler)"; combos gain a leading `default (...)` item;
  line edits use empty = default with the handler value as placeholder.
  A "Use handler defaults" button clears all overrides. Tooltips show the
  resolved default and its source.
- **JSON schema** (`SessionConfig`): only explicit choices are recorded, as
  `override_keys` (plus the effective values top-level, so files stay
  readable). A JSON without `override_keys` is a **legacy config: every
  stored value counts as an override** — pre-scripting behavior is
  preserved exactly; it migrates to the new schema on next save.
  `Janus.addOption` answers persist under `script_options`.
- **Resolution** (`src/gui/models/config.py`): `SHEET_FIELD_MAP` +
  `apply_script_defaults(config, sheet)` — user override > sheet >
  built-in. `MainWindow._build_config` returns a config whose plain
  attributes are always the effective launch values.
- **Options UI**: the Settings panel has two tabs. **Game** (the default)
  shows only the handler's `Janus.addOption` declarations (choice→combo,
  bool→checkbox, int→spinbox, string→line edit; answers reach
  `ctx.options`), or a "no game-specific options" note. **All settings**
  is the tri-state list above, scrolled; its title shows the number of
  overrides ("All settings (2)") so they aren't invisible while hidden.
- **Session hooks**: `LauncherService` creates one `HandlerScript` per
  session and passes it into `launch_steam_game` per instance (all
  per-instance hooks fire from worker threads; `invoke()` serializes and
  re-anchors path resolution from each ctx's `gameDir`, so parallel
  instances with different mirrors are safe). `onAllStarted` fires when
  every instance has reported a pid; `onInstanceExit` per exit;
  `onStop` + `finish()` (backup restore, spawned-helper cleanup) exactly
  once — on Stop All, on natural session end, and before a new launch.
- **Lobby**: the hardcoded `+connect_lobby` append is replaced by
  `Game.lobbyConnectArg` / `Game.onLobbyDetected` (see §5).
- **User messages**: `Janus.warn`/`Janus.error` reach a modal dialog via
  `HandlerScript.set_notify(cb, ui_thread)` → `LauncherService.scriptMessage`
  (level, text, done) → `MainWindow._on_script_message`, which `exec()`s
  the box then calls `done()`. The worker-thread hook waits on that (holding
  the script lock); GUI-thread hooks (`onInstanceExit`, `onAllStarted`, ...)
  that would block on the lock meanwhile are **skipped** instead of
  deadlocking the dialog. `stop_all()` calls `release_waiters()` first. On
  the CLI the `[script] WARNING:`/`ERROR:` log line is the only output.
- A broken handler shows a red indicator in Settings (tooltip = error) and
  blocks launch with a clear message.

Not wired: the CLI still ignores per-game JSON configs (unchanged).

## 12. Design notes & known limitations

- **No CPU watchdog** (binding limitation above). Memory limit works.
- **`onMirrorReady` only fires with a mirror** (Goldberg, or BepInEx via
  `Game.unityUiScale`/`bepinexPlugins`) — there is no mirror otherwise; use `play` for non-goldberg file drops (but only into dirs you
  own — never the real install dir — and pair with `Janus.backup`).
- `buildCommand` return values are runtime-wrapped like `launchCommand`;
  `Game.runtime` applies to both.
- The `+connect_lobby` append and lobby detection remain GUI-only; the
  engine-side hook exists but never fires on the CLI today.
- Sheet fields are merged into CLI args by comparing flags against their
  argparse defaults (`scripting.effective`); an explicit flag equal to its
  default cannot override the sheet.
- In the GUI, overriding a text field *to empty* isn't expressible (empty =
  "use handler default") — use a user-local handler for that.
- `Game.extraEnv` never overrides variables already present in the user's
  real environment (or injected by the GUI) — user env > script, matching
  the toggle fields.
- The design is modelled on Nucleus Co-Op's handlers.

## 13. Non-Steam games

A handler with `Game.id` (no `Game.appid`) is a non-Steam game
(`src/host/nonsteam.py`). Nothing comes from Steam: no manifest, Steam
Linux Runtime, Proton, isolation, Steam Input check or forced layout. The
handler sets the game up and starts it; Janus keeps profiles, the hook,
gamescope tiles, KWin placement, Stop all. Both GUIs list these games next to
the Steam ones (Big Picture art: what `Game.art` points at, else `cover`,
`hero`, `logo` `.jpg`/`.png` in the handler's payload dir, else a name card). Janus keeps the
game under `~/.local/share/janus/games/<id>/` (`ctx.gameDir` =
`ctx.installDir`); per-player data goes under `ctx.saveRoot` as usual. Set
`Game.isolation = "none"` and `Game.profileSaves = true`.

| Global | Meaning |
|---|---|
| `Game.id` | **Required.** The game's id, a literal on its own line. |
| `Game.setupCheck(ctx)` | Is the game ready to launch? Return `{ready: true, status?}` (status: a line the GUIs show, e.g. the installed version), or `{ready: false, message, action?}`: *message* says what's missing, *action* the `Janus.addAction` key that fixes it. No hook = always ready. Run when the game is selected, before every launch and after each action. The GUIs show the message with the action as a button (Big Picture: A runs it, then the next step is checked; desktop: a question, and the Settings → Game → Setup box); the CLI: `main.py setup GAME [-y]` runs the fixes in order. A launch refuses while not ready. |
| `Game.owner(ctx)` | `{name, label}` of the account that plays as the **owner** profile instead of the Steam account (Minecraft: the Microsoft account signed into Prism, label `"Microsoft account"`). The GUIs show the name and label for the owner, and `ctx.profile.name` / `ctx.players[].profile.name` carry it. |
| `Game.art(ctx)` | `{cover, hero, logo}`: https URLs of the game's artwork (cover = portrait like Steam's library capsule, hero = wide banner without title, logo = transparent title logo; any may be missing). Big Picture downloads them in the background into `<game dir>/art/` when missing or a month old (`nonsteam.fetch_art`), like Steam's own art cache: fetched to the user's machine, never shipped. They win over art in the handler's payload. Minecraft: the Microsoft Store catalog's Poster and SuperHeroArt. |
| `Game.buildCommand(ctx)` | **Required**: the argv to run (absolute paths: there's no install-dir resolution). Session game args and `ctx.gameArgs` from `play` are appended. |
| `Game.launcherStartsGame` | `true` = the command is a launcher that starts the game itself (Prism): Janus runs it **outside** gamescope, with the plain desktop environment, logging to `~/.local/share/janus/run/<id>/i<N>.log`, and the launcher must start the game through **`ctx.wrapper`**: a script (written just before the spawn, so `play`'s `ctx.env` is in it) that sets the game's environment (the hook, controller, `ctx.env`) and runs its arguments in this instance's gamescope tile. The launcher's process stands for the instance: it must exit when the game does. Stop kills the game too (its processes carry `JANUS_INSTANCE_TAG`). Without it, the command is the game and goes into gamescope like a Steam game's. |

The setup ctx (setupCheck, owner, actions) has `appid` (= the id),
`gameName`, `gameDir`, `installDir`, `profilesDir`, `options`, `memTotalMb`,
`verbose`. The per-instance ctx is the usual one (section 6) with
`installDir` = `gameDir` = Janus's game dir, `winePrefix` null, plus
`wrapper` and `memTotalMb` (total RAM in MB, e.g. for a JVM heap).

Hook order per instance: `buildEnvironment` → `buildCommand` → `play` →
spawn → `onInstanceStarted`; session hooks as for Steam games.

`handlers/minecraft/handler.js` is the worked example: downloads Prism
Launcher's portable build, signs in (or copies the account of the user's
own Prism), installs the newest Minecraft that Controlify supports from
Modrinth + Fabric meta, and gives every player their own Prism data dir.

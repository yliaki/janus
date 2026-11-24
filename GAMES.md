# Game Setups

Tested split-screen configurations for specific games.

Most of the per-game flags below are now encoded in **handler scripts**
(`handlers/<game-name>.js`, see [SCRIPTING.md](SCRIPTING.md)) and applied
automatically by the `steam` / `steam-split` subcommands — the commands here
still work, but the special flags are no longer required where a handler
exists.

## Left 4 Dead 2 (550)

Source Engine (Native Linux, 32-bit). Uses SDL2 linked via ELF NEEDED in `inputsystem.so`. Controller input works through evdev filtering — our SDL2 hooks must be hidden (not exported via LD_PRELOAD) or they interfere with the real SDL2 library. Disable Steam Input in Steam UI (Properties → Controller → Disable Steam Input).

```bash
# Instance 1
python3 src/host/main.py steam 550 -c 0 -i 1 --isolation secondary --gamescope="-w 960 -h 1080 --borderless" --nvidia

# Instance 2
python3 src/host/main.py steam 550 -c 1 -i 2 --isolation secondary --gamescope="-w 960 -h 1080 --borderless" --nvidia
```

## Nuclear Throne (242680)

Native Linux (GameMaker). **Handler required on current runtimes**
(`handlers/nuclear-throne.js`): the binary is 64-bit but scout-era — it needs
`libcrypto.so.1.0.0`, which only the scout runtime ships, so the handler
forces `Game.runtime = "scout"`; it also launches the ELF directly because
the shipped `run.sh` hardcodes `DISPLAY=:0` (wrong inside gamescope). Uses `--force-grab-cursor` and `-g` (grab keyboard) so the game captures mouse/keyboard input correctly inside gamescope — without these, the cursor escapes the game window. Use the focus-release hotkey (Alt+1) to release the cursor when needed.

```bash
# Instance 1
python3 src/host/main.py steam 242680 -c 0 -i 1 --isolation secondary --gamescope="-w 1600 -h 900 --borderless -g --force-grab-cursor" --nvidia

# Instance 2
python3 src/host/main.py steam 242680 -c 1 -i 2 --isolation secondary --gamescope="-w 1600 -h 900 --borderless -g --force-grab-cursor" --nvidia
```

## Terraria (105600) / tModLoader (1281930)

Handlers `handlers/terraria.js` / `handlers/tmodloader.js` set everything (Goldberg,
OpenGL for FNA, saves, window sized to the tile, music muted in instances 2+). Host with Multiplayer > Host & Play; others join via
Esc > **Invite Friends** (host) or Multiplayer > **Join via IP** `127.0.0.1`. tModLoader needs
Terraria installed (its assets); workshop mods load in every instance.

```bash
python3 src/host/main.py steam-split 105600 --tile 0:0,0,1280,1440 --tile 1:1280,0,1280,1440
python3 src/host/main.py steam-split 1281930 --tile 0:0,0,1280,1440 --tile 1:1280,0,1280,1440
```

Options (`-o`): `saves=firstReal|isolated|copy`, tModLoader `terrariaDir=PATH`.
See NOTES.md "Terraria (105600) / tModLoader (1281930)".

## RV There Yet? (appid 3949040) — KWin Wayland, solo via Janus

Unreal Engine game. **Must force `-dx11`** or it defaults to D3D12/VKD3D
and rubber-bands hard under gamescope on NVIDIA (see NOTES.md). Config:
`game_args: "-dx11"`, `--isolation secondary` for solo (real prefix →
keeps settings + DXVK shader cache). Wired pad preferred — the Xbox
*wireless* pad renumbers its `/dev/input/event*` on every reconnect,
which leaves a stale fd that pins a winedevice.exe thread at ~100% CPU.

```bash
python3 src/host/main.py steam-split 3949040 \
  --tile 0:0,0,2560,1440 --output HDMI-A-1 --isolation secondary -v -- -dx11
```

## Necesse (1169040)

Native Linux, but **Java** — the entry point is the bundled JRE, not a file you can
point the executable detector at (the only scripts in the install dir start the
*dedicated server*). Needs an explicit launch command; see NOTES.md for how it was
recovered from Steam's `appinfo.vdf`.

**Requires `--steam-input-filter`** (GUI: "Filter Steam Input per-instance"). Without
it the JVM segfaults ~10s in, before any window: Janus's default strategy nulls
`SteamController008` to force the SDL2/evdev path, and steamworks4j's
`SteamControllerNative_init` dereferences it without a null check. See NOTES.md.

GUI: set **Launch command** to
`./jre/bin/java -XX:+UseG1GC -Xms512m -Xmx4G -jar Necesse.jar`, tick
**Filter Steam Input per-instance**.

```bash
# KWin Wayland, 50/50 split
python3 src/host/main.py steam-split 1169040 \
  --tile 0:0,0,1920,2160 --tile 1:1920,0,1920,2160 \
  --isolation secondary --steam-input-filter \
  --launch-cmd "./jre/bin/java -XX:+UseG1GC -Xms512m -Xmx4G -jar Necesse.jar"
```

Verified: two tiles at 1280x1440 on a 2560x1440 output, both instances reach the menu,
both log `Detected 1 controllers`.

Steam's own args add `-XX:+UnlockExperimentalVMOptions -XX:+ExplicitGCInvokesConcurrent
-XX:G1NewSizePercent=20 -XX:G1ReservePercent=20 -XX:MaxGCPauseMillis=50
-XX:G1HeapRegionSize=32M`; they're GC tuning only, drop or keep as you like. Raise
`-Xmx` per instance if two JVMs at 4G each is too much for the box.

Input is LWJGL/GLFW reading `/dev/input/event*` directly (no SDL), so the evdev hook
layer is what does the per-instance filtering.

## How to Fish (4001890) — Unity/Proton, Goldberg co-op (validated: 3 players, KWin Wayland)

Handler: `handlers/how-to-fish.js` (modeled on PEAK: full isolation, Goldberg
debug build, OSK on, tile geometry via `-screen-width/-height
-screen-fullscreen 0` in `play()`). Netcode is FishNet over Heathen Steamworks
lobbies. **Join via in-game invites** (`Game.goldbergInvites`, validated
2026-10-07): all tiles launch at once; host (friends-only or public) in any
tile and use the invite button — the others join while running. Pasting the
lobby ID (GUI lobby panel) still works as a fallback. Auto-join is off: neither
gbe_fork nor this game parses `+connect_lobby <id>`. Validated
2026-08-30: two MACHENIKE pads + Xbox pad, three tiles; all three FishNet
clients connected to one instance's public lobby (`FishNet client 2
connected. Steam lobby members: 3`).

Install quirks: the build is nested one level (`How to Fish/How to Fish/How to
Fish.exe`) and `steam_api64.dll` sits in `How to Fish_Data/Plugins/x86_64/` —
the recursive exe search (name match) and the Goldberg mirror's recursive walk
both handle this. **The exe is SteamStub-DRM-wrapped (`.bind` section)**: the
stub relaunches the game through real Steam before Unity starts, so Goldberg
never loads. Unpack it once — `scripts/steamless.sh "<install>/How to Fish/How
to Fish.exe"` — and the handler's `onMirrorReady` copies the resulting
`How to Fish.exe.unpacked.exe` into the mirror (NOTES.md "SteamStub DRM").
Ships a `D3D12/` dir: if it stutters under gamescope on NVIDIA, try
`-force-d3d11` in game args (cf. RV There Yet?).

**Prerequisite:** launch the game once through Steam first. `is_proton` keys
off `compatdata/4001890/config_info`, which Steam writes on first run; without
it Janus treats the game as native and finds no executable.

```bash
python3 src/host/main.py steam-split 4001890 --tile 0:0,0,1920,2160 --tile 1:1920,0,1920,2160
```

## PEAK (3527290) — Unity/Proton, Goldberg co-op via in-game invites (validated 2026-10-07)

Handler: `handlers/peak.js` — full isolation, Goldberg debug build, LAN
discovery on, **`Game.goldbergInvites = true`**, OSK on. No Steam Input filter
(PEAK doesn't use the Steam Input API). All tiles launch at once; host in any
tile and click **Invite players** — every other instance joins on its own
(validated: invite sent → `Got Lobby Invite` → `JoinLobby` → in the host's game).

```bash
python3 src/host/main.py steam-split 3527290 --tile 2:0,0,2560,720 --tile 1:0,720,2560,720
```

Two identical pads (MACHENIKE) plus a spare are handled: each binds to its own
Steam virtual pad by the controller index in Steam's handle (NOTES.md).

## Palworld (1623730) — Unreal/Proton, real Steam + local dedicated server (validated 2026-10-07)

Handler: `handlers/palworld.js`. Multiplayer needs a genuine Steam login (Epic
Online Services), so **no Goldberg**: every instance runs on your real account
(`isolation = "prefix"`), instance 2+ get a synthetic Steam ID
(`JANUS_STEAM_ID_OVERRIDE`) so the server sees distinct players, and the handler
starts the **Palworld Dedicated Server** (Steam tool 2394010, same library) with
`bUseAuth=False` and stops it at session end (option "Start local dedicated
server", default on). In each tile: Join Multiplayer Game → `127.0.0.1:8211`.

One-time setup: install the dedicated server (`steam://install/2394010`) and
unpack the DRM-wrapped exe:
`scripts/steamless.sh "<install>/Pal/Binaries/Win64/Palworld-Win64-Shipping.exe"`.

```bash
python3 src/host/main.py steam-split 1623730 --tile 2:0,0,1280,1440 --tile 1:1280,0,1280,1440
```

Controllers: both pads work through Steam Input (Xbox Series + MACHENIKE validated
2026-10-09) thanks to the hook's virtual-pad rename (each instance's pad is XInput
player 1). The server plays its own world, not your single-player one (NOTES.md). Nucleus
Co-Op's alternative (Goldberg + Nemirtingas Epic emulator, invite codes on the
host's world) is a TODO.


## Content Warning (2881650) — Unity/Proton, Goldberg + real Steam ticket for Photon (validated 2026-10-08)

Handler: `handlers/content-warning.js`. Full isolation, Goldberg debug build, LAN
discovery on, `Game.goldbergInvites = true`, OSK on. Host in any tile and
**Invite**; the other instances join while running (validated: two
instances in one room).

Gameplay runs on Landfall's Photon Cloud app, which validates the Steam
ticket with Valve. **`Game.goldbergRealTicket`** puts a proxy
`steam_api64.dll` in front of Goldberg that hands Photon a real session
ticket from the running Steam client. Everything else stays on Goldberg. The
handler also patches PUN's `CheckUserOnJoin` off in the mirror's
`PhotonRealtime.dll`: every instance's ticket belongs to the same account, so
Photon gives them all the same UserId. No Photon account or AppId is
needed; leave the "Photon … AppId (optional)" options blank.

Requirements: Steam running and logged in to an account that owns the game,
plus at least one native Linux Steam game installed (the ticket helper
borrows its `libsteam_api.so`). The proxy DLL ships prebuilt in
`vendor/ticketproxy/`, so no compiler is needed.

## Deep Rock Galactic (548430) — UE4.27/Proton, Goldberg co-op via in-game invites (validated 2026-10-08)

Handler: `handlers/deep-rock-galactic.js` — full isolation, Goldberg debug build,
LAN discovery on, `Game.goldbergInvites = true`, launches
`FSD-Win64-Shipping.exe` directly with `-dx11 -nohmd -nosplash -fullscreen
-disablemodding` plus `-ResX/-ResY` from the tile. No Steam Input filter (DRG
requests no SteamInput/SteamController interface; pads arrive via XInput).
All tiles launch at once; open the Esc menu in the Space Rig and click
**Invite friends** — the other instance joins (validated: `ConnectP2P` →
host `AcceptConnection`, in the host's rig).

```bash
python3 src/host/main.py steam-split 548430 --tile 0:0,0,1280,1440 --tile 1:1280,0,1280,1440 --output HDMI-A-1
```

Per-instance saves/config via UE's `-UserDir=` (in each instance's Wine prefix,
`C:/users/steamuser/Janus/DRG-<N>/`); the real save + settings are copied in
once (options `importSave`, `importConfig`), the real install is never written.
Logo intro skipped by leaving `DRG_LogoIntro*.mp4` out of the mirror
(`skipIntro`). Goldberg DLCs off by default (`unlockAllDlc`). See NOTES.md.

## Gunfire Reborn (1217060) — Unity IL2CPP/Proton, gbe_fork ColdClient co-op via invites (validated 2026-10-18)

Handler: `handlers/gunfire-reborn/handler.js` (+ `payload/coldclient/`) — full
isolation, Valve's `steam_api64.dll` kept (the game verifies it) and gbe_fork run as
the Steam client through ColdClientLoader, LAN discovery on, `Game.goldbergInvites`.
Pads through Steam Input (each game its own Steam virtual pad); option
`steamInput=false` reads the pads directly instead, if Steam feeds nothing (seen once).
A new profile starts in the tutorial: Back → "Skip the
Tutorial", quit the run. Co-op: **New Adventure** → "+" slot → **Invite**; the other
instances are pulled into the room without a prompt (validated with two fakepad
pads, 2 tiles).

```bash
python3 src/host/main.py steam-split 1217060 --tile 0:0,0,1920,540 --tile 1:0,540,1920,540
```

Saves: gbe_fork's `GSE Saves/1217060/remote/user_1.dat` in each player's prefix,
optionally seeded once from the real Steam Cloud copy (`importSave`, default none).
DLC heroes off unless `unlockAllDlc`. Option `uiScale` (1.0–2.5): enlarges menus
and HUD as far as they fit (injected `janus_uiscale.dll`); the same DLL keeps the game
at the full tile size (it shrinks itself to 3/4 height otherwise). See NOTES.md.

## Factorio (427520) — native Linux, Goldberg + Factorio's own LAN netcode (auto-join validated headless 2026-10-08; controllers untested)

Handler: `handlers/factorio.js` — Goldberg (one distinct identity + emulated
"Steam Cloud" per instance), bare launch (`Game.runtime = "none"`, Goldberg
reached via `LD_LIBRARY_PATH`), per-instance write-data under
`~/.local/share/janus/factorio/<N>/` (`-c config.ini`). Factorio has no in-game
invite button and can't be told to connect while running, so the default join
mode `menu` does the nearest thing: instance 1 starts at the main menu and the
player hosts any save via **Multiplayer > Host saved game** (dialog preset to
LAN-only, no verification). The other instances are held back until it is
hosting, then start with `--mp-connect 127.0.0.1:<port>` straight into the game.
Mode `auto` hosts a save chosen up front (`--host <save>`, no clicks).

```bash
python3 src/host/main.py steam-split 427520 --tile 0:0,0,1280,1440 --tile 1:1280,0,1280,1440
```

Options: `join` (`menu`/`auto`/`manual`), `save` (`auto` only: empty = newest
save, `new` = fresh map, or a save name), `names` (LAN player names), `shareSaves` (host from
`~/.factorio/saves`, default on — the host autosaves there like normal play),
`useMods` (`~/.factorio/mods` for every instance), `controllerInput` (sets
the input method to game controller; on by default). See NOTES.md
"Factorio (427520)".

## Borderlands 2 (49520) — native Linux (Aspyr), Goldberg, LAN co-op (menus validated 2026-10-16; controllers/joining untested)

Handler: `handlers/borderlands-2.js` — Goldberg with the game's old
`steam_interfaces.txt`, `secondary` isolation (the Steam account plays its
real saves; saves are per Steam ID so they follow the profile), tile size via
`-windowed -ResX/-ResY`, no intro movies. Co-op: Options > Network > LAN in
every instance, host in one, Find games in the others (the instances also see
each other as friends). Use a top/bottom split — the 16:9 menus are cut off
in side-by-side tiles.

```bash
python3 src/host/main.py steam-split 49520 --tile 0:0,0,1920,540 --tile 1:0,540,1920,540
```

## Mycopunk (3247750) — Unity/Proton, Goldberg, lobby list co-op (validated 2026-10-25)

Handler: `handlers/mycopunk.js`: full isolation, Goldberg with LAN discovery on, BepInEx
UI scale option. Pads through Steam Input. Everyone lands on their own ship in the H.U.B.
The joining players press Start → CREW tab (LT/RT) → the host's "<name>'s Ship" under
Join a Crew → A, then pick character and weapons (validated with two fakepad pads, 2
tiles). Option `importSave` (default `first`) copies the real
`LocalLow/Pigeons at Play/Mycopunk/Profiles` into a profile once.

UI scale covers the HUD too: it scales steadily, ignoring edge-pinned ping markers, the
sliding dialogue box and the objective fade (`unityUiScaleIgnore`). A small plugin turns on
"Allow modded joiners", which the game resets at every start, so BepInEx-modded instances
can join each other.

```bash
python3 src/host/main.py steam-split 3247750 --tile 0:0,0,1920,540 --tile 1:0,540,1920,540
```

## Far Far West (3124540) — UE5/Proton, Goldberg, the game's LAN mode (validated 2026-10-25)

Handler: `handlers/far-far-west.js`: full isolation, Goldberg, `-dx11`, launches
`FarFarWest-Win64-Shipping.exe` directly (the bootstrapper stops at a VC++ prerequisite
prompt in fresh prefixes), Unreal UI scale with a per-screen fit (UE4SS, `unrealUiFit`): "UI scale" is the maximum, and each screen gets the largest scale that fits (e.g. 2.0 → notice 1.15, pause menu 1.6, hub up to 2.0 in 1920x540). Pads through Steam Input. The online
modes are greyed out under Goldberg ("Could not connect to Evil Raptor network"). Host:
Create Session → Offline / LAN. Others: Browse Sessions → tick LAN under Network →
Refresh List → the host's row → Yes (validated with two fakepad pads, 2 tiles). Menus
use a left-stick cursor.

```bash
python3 src/host/main.py steam-split 3124540 --tile 0:0,0,1920,540 --tile 1:0,540,1920,540
```

## Gamble With Your Friends (3892270) — Unity/Proton, Goldberg invites, Steam Input with a Gamepad layout (validated 2026-10-25)

Handler: `handlers/gamble-with-your-friends/` (+ `payload/JanusPadFocus.dll`, source in
`plugin/`): full isolation, Goldberg invites, `-force-d3d11`, Steam Input on.
- **Every controller needs a Gamepad layout** for the game in Steam (Properties →
  Controller → layout → Templates → Gamepad). Steam's default for it is a
  keyboard/mouse community layout, which can't be split. The launch message says so.
- The BepInEx plugin keeps pads working in unfocused tiles, skips the "FEELING LUCKY?"
  start screen (YES there can quit the game) and maps the pad's **View** to the "+"
  invite icon.
- Controller sensitivity defaults to 200 (the game's 9 is far too low).
- Co-op: on the main menu the host clicks + (or presses View). Once the others pop into
  the lobby, Host Game → Yes, I am → slot.
- Validated with an Xbox 360 pad and a Pro Controller.

```bash
python3 src/host/main.py steam-split 3892270 --tile 0:0,0,1920,540 --tile 1:0,540,1920,540
```

## Core Keeper (1621690) — native Linux, Goldberg, join by Game ID (validated 2026-10-27)

Handler: `handlers/core-keeper.js`: `secondary` isolation, Goldberg with LAN discovery
on, crossplay turned off in `prefs.json` (with it on, PlayFab rejects Goldberg's ticket
and multiplayer is disabled). Pads through Steam Input. The game has no invite button on
Steam: the host starts a world and opens the pause menu, where the Game ID is at the top
left (eye icon reveals it); the others choose Join Game → Join With Game ID, type it and
Join. **Text fields need a keyboard** (Game ID, world and character names): Steam's
gamepad text input isn't there under Goldberg. Music only in instance 1. The view is a
fixed 16:9 picture, so both two-player splits show 960x540 (validated with two fakepad
pads: host + Game ID join, both in one world, each pad moving only its own player).

```bash
python3 src/host/main.py steam-split 1621690 --tile 0:0,0,1920,540 --tile 1:0,540,1920,540
```

## Abiotic Factor (427410) — UE5/Proton, Goldberg, LAN server list (validated 2026-10-27)

Handler: `handlers/abiotic-factor.js`: `secondary` isolation, Goldberg with LAN on,
`-dx11` (option), launches `AbioticFactor-Win64-Shipping.exe`, Unreal UI scale options.
`play()` switches the instance's Engine.ini from EOSPlus to the plain Steam subsystem
with IP networking: under Goldberg EOS can't log in, and then even the LAN list fails.
Menus use a left-stick cursor, A clicks. Host: Enter the Facility → Load Game (or Start
New Game: untick Singleplayer Only, type a world name with a keyboard) → LAN Server
ticked → Host Game. Others: Join a Server → Switch to LAN → the host's row → Join, then
job and traits. The in-game "Invite friends" doesn't work (NOTES.md). Validated with two
fakepad pads, 2 tiles: both in the host's world.

```bash
python3 src/host/main.py steam-split 427410 --tile 0:0,0,1920,540 --tile 1:0,540,1920,540
```

## Sven Co-op (225840) — GoldSrc native 32-bit, listen server + auto-connect (validated 2026-10-27)

Handler: `handlers/sven-coop.js`. **Every controller needs a Gamepad layout for Sven Co-op
in Steam** (the launch checks and names missing pads). Instance 1 starts a LAN listen
server on the campaign hub (option `map`), the others connect automatically; no menu is
needed. Map briefings and the server MOTD are skipped (they can only be closed with the
mouse). Steam overlay off (copy of `svencoop.sh` in the mirror). Each pad moves only its
own player, also when that tile isn't focused (validated with two fakepad pads).

```bash
python3 src/host/main.py steam-split 225840 --tile 0:0,0,1920,540 --tile 1:0,540,1920,540
```

## Borderlands: The Pre-Sequel (261640) — native Linux (Aspyr), Goldberg (validated 2026-10-27)

Handler: `handlers/borderlands-the-pre-sequel.js`, same as Borderlands 2's. Host starts a
game; the others select the host under "Welcome to Helios" on the main menu → Join Game
(validated with two fakepad pads, 2 tiles).

```bash
python3 src/host/main.py steam-split 261640 --tile 0:0,0,1920,540 --tile 1:0,540,1920,540
```

## Don't Starve Together (322330) — native Linux, Goldberg offline mode + LAN (validated 2026-10-27)

Handler: `handlers/dont-starve-together.js`. Goldberg reports Steam offline, so the game
skips Klei's login and starts in offline mode. A at the start (Enable Controller). Host:
Host Game → … → Generate World (Menu button). Others: Browse Games → the host's world →
Join (Menu). Validated with two fakepad pads, 2 tiles.

```bash
python3 src/host/main.py steam-split 322330 --tile 0:0,0,1920,540 --tile 1:0,540,1920,540
```

Unreal per-screen UI fit (`unrealUiFit`, UE4SS) is also enabled in the Deep Rock Galactic
and RV There Yet? handlers. Validated 2026-10-25 at maximum 2.0 in 1920x540: DRG start
screen 1.2, space-rig HUD 1.9, escape menu 1.15; RV main menu 1.05 (its sign menu fills
the screen). Palworld and Abiotic Factor can't use it: UE4SS crashes them (NOTES.md
"Unreal UI fit on other games").

## Minecraft: Java Edition (`minecraft`, not a Steam game) — Prism Launcher, LAN (validated 2026-10-31)

`handlers/minecraft/handler.js`. Janus downloads its own portable Prism Launcher and installs
the newest Minecraft that Controlify (controller support) works with, on Fabric, with Sodium and
Mod Menu. One Microsoft account (the owner, playing online); everyone else plays offline under
their profile name. Each player has their own options, Controlify settings and worlds.

```bash
python3 src/host/main.py setup minecraft          # once: Prism, account, install
python3 src/host/main.py steam-split minecraft --tile 0:0,0,1920,540 --tile 1:0,540,1920,540
```

Co-op: player 1 opens a world, pause → World Options → Multiplayer → LAN: ON → Apply Changes;
the others find it under Multiplayer (offline players can join the online host's LAN world).


## Risk of Rain 2 (632360) — Unity/Proton, Goldberg invites (validated 2026-11-01)

Handler: `handlers/risk-of-rain-2.js`: full isolation, Goldberg invites, `--disableCrossplay`,
BepInEx UI scale option (1.6–1.8 suits 1920x540: menus and run HUD fit). Everyone starts
signed in to their profile (the owner's Steam Cloud profile is copied to every player once,
option `importSave`, so guests get the unlocks), windowed at the tile size, intro skipped,
music only in player 1. Co-op (validated, fake pads): Multiplayer → the "+" slot in the
lobby panel (D-pad right, A) invites the others, who join at once → host: Create Multiplayer
Game → Start Game → character select together.

```bash
python3 src/host/main.py steam-split 632360 --tile 0:0,0,1920,540 --tile 1:0,540,1920,540 -o uiScale=1.8
```

## Hollow Knight: Silksong (1030300) — native Linux, SSMP mod (launch validated 2026-11-01)

Handler: `handlers/hollow-knight-silksong.js`: Goldberg (own Steam ID per player), full
isolation; downloads BepInExPack for Silksong + SSMP on first launch (option `ssmp`).
Validated: both instances load SSMP, separate pads, names pre-filled. SSMP's multiplayer
menu needs the mouse: Start Multiplayer → Direct IP → HOST LOBBY / CONNECT (127.0.0.1).
Host/join itself not yet validated.

## Hollow Knight (367520) — Proton, Modding API + HKMP (launch validated 2026-11-01)

Handler: `handlers/hollow-knight.js`. Needs the game's `1.5.78.11833` beta branch (the
handler refuses otherwise). Downloads Modding API v77 + HKMP 2.4.3 on first launch (option
`hkmp`); `importSave` (default `first`) copies the owner's real saves once. HKMP's panel in
the pause menu needs the mouse: Start Hosting / Connect (127.0.0.1, name pre-filled).
Host/join itself not yet validated.

## The Forest (242760) — Unity 5.6/Proton, Goldberg, auto-join (launch validated 2026-11-01)

Handler: `handlers/the-forest.js`. The resolution dialog is patched away and gbe gets the old
SDK's interface list. GUI sessions auto-join: player 1 starts alone; Multiplayer → Host Game
→ New Game → difficulty → Create Lobby (all with the pad); the others then start with
`+connect_lobby <id>` and land in the lobby; player 1 presses Start Game. The lobby's
"Invite friends" and the Join list need the mouse. Auto-join itself not yet validated.

## 7 Days to Die (251570) — native Linux, Goldberg, Friends server list (validated 2026-11-01)

Handler: `handlers/7-days-to-die.js`: no EAC, no crossplay/EOS, no Discord, the host's game
is a real server. Accept the EULA once per player (Start). Host: Play Game → New/Continue Game
→ Start → spawn (D-pad up, A). Others: Play Game → RB → Join a Game → up to the icons → Friends
icon → the host's game → Start (Connect) → Spawn near a friend (validated, fake pads).

```bash
python3 src/host/main.py steam-split 251570 --tile 0:0,0,1920,540 --tile 1:0,540,1920,540
```

## Valheim (892970) — native Linux, Goldberg (menus validated 2026-11-01)

Handler: `handlers/valheim.js`: intro skipped, `importSave` copies real characters/worlds.
Host: character → world → "Start server" on, Crossplay off. Others: Join Game → Friends.
Naming a character needs a keyboard (click the name field). Join not yet validated.

## DDNet (412220) — native Linux, DDNet's own controller support + Start/RT from Janus, local server (validated 2026-11-22)

Handler: `handlers/ddnet.js`. **Janus input only** (set DDNet to **Disable Steam Input** in
Steam). The game reads its virtual Xbox 360 pad itself with **absolute aim**
(`inp_controller_absolute`, right stick = axes 3/4); binds are sent on the command line. DDNet's
menus move the cursor with the stick but no pad button clicks and only Esc opens the menu, so
`Game.keyboardMouse` with `keepPad: true` adds RT = left click and Start = Esc. Option `join` =
`local` (default): Janus starts the bundled `DDNet-Server` on 127.0.0.1 (option `map`) and every
tile connects on launch; `browser` = main menu; option `server` = `host:port` sends every tile to
that online server (validated 2026-11-22 on DDNet GER - Moderate; servers cap clients per IP,
usually 4). Pads: left stick / D-pad walk, right stick aim (menu cursor), A jump, RT fire
(click), LT or B hook, LB/RB weapons, X emote wheel, Y scoreboard, Start menu (View is unbound).
Validated with two fakepad pads: aim follows the stick direction exactly, each pad drives only
its own tee, Start opens only its own tile's menu, RT clicks menu buttons. The Start/RT helper
runs in **GUI sessions only** (desktop GUI / Big Picture); from the CLI `steam-split` the pads
play but can't open or click the menu.

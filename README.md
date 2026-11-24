# Janus Split Screen

> [!WARNING]
> **Third-party binaries are vendored in `vendor/`. Check every licence before making this repo public or distributing it.**
>
> | Path | What | Licence | Constraint |
> |------|------|---------|------------|
> | `vendor/steamless/` | [Steamless](https://github.com/atom0s/Steamless) v3.1.0.5 (prebuilt .NET exes/DLLs) | **CC BY-NC-ND 4.0** | Redistributed unmodified and free of charge, with attribution and the licence notice (`LICENSES/`). Anyone selling a Janus build must leave it out. |
> | `vendor/goldberg/` | [gbe_fork](https://github.com/Detanup01/gbe_fork) prebuilt `steam_api` libs (regular + debug) | **LGPL-3.0** | Redistributing binaries means shipping the licence text and offering the corresponding source. Licence texts are in `LICENSES/`; the releases the files came from are recorded there and in `vendor/goldberg/README.md`. |
> | `vendor/quickjs/` | [quickjs-ng](https://github.com/quickjs-ng/quickjs) Python wheel (`_quickjs.abi3.so`) | MIT | Keep the bundled `LICENSE` (in `dist-info/licenses/`). |
>
> Every bundled component's licence is listed in [LICENSES/README.md](LICENSES/README.md); both release packages ship that folder.
>
> The repo is currently **private**. Janus itself is licensed under **GPL-3.0-or-later** ([LICENSE](LICENSE)).
>
> Separately, Steamless strips Valve's SteamStub DRM and Goldberg emulates Steam. Only use them on games you own, for local co-op.

Split-screen local multiplayer for Steam games on Linux. Janus runs one copy of
the game per player, each in its own window on your screen(s), and each copy
only sees its own player's controller. Games that only support online
multiplayer can still be played together on one PC: the copies connect to each
other through an emulated Steam (Goldberg), and every player keeps their own
saves.

## Requirements

- **KDE Plasma on Wayland.** Windows are placed with KWin window rules.
  (Steam Deck desktop mode, Bazzite, Kubuntu, Fedora KDE, Arch + Plasma, ...)
- **Steam** (native install; Flatpak Steam isn't supported yet)
- **gamescope**, from your distribution — Janus doesn't bundle it (it's tied to
  the system's graphics stack): `pacman -S gamescope`, `dnf install gamescope`,
  `zypper install gamescope`, Ubuntu 26.04+ `apt install gamescope`
  (multiverse), Debian 13 `apt install -t trixie-backports gamescope`
  (contrib); SteamOS/Bazzite have it. Both UIs show an error with the right
  command when it's missing.
- Recommended: the **Steam Linux Runtimes** (Steam → Library, show Tools:
  scout, sniper and the one your Proton uses). Steam installs most on first
  use; Janus warns before launching a game whose runtime is missing.
- **Steam Input enabled for the game** (game Properties → Controller → "Enable
  Steam Input"). Janus refuses to launch otherwise and says how to fix it.
- A **handler** for the game (see below). Games without one can't be launched.

## Install

**From a release:** unzip `janus-split-screen-<version>.zip` and run
`./install.sh`. It installs for your user only (`~/.local/opt/janus`, no root,
any distribution, Qt included), adds "Janus Split Screen" and "Janus Big
Picture" to the application menu and the commands `janus`, `janus-bigpicture`,
`janus-cli` and `janus-steam-shortcut`, and offers to add Big Picture to Steam.
On Debian/Ubuntu there is also a `.deb`:
`sudo apt install ./janus-split-screen_<version>_amd64.deb`.

**From source:**

```bash
scripts/compile_hooks.sh      # build/libjanus64.so + libjanus32.so (32-bit: sudo apt install gcc-multilib)
scripts/check_gui_deps.sh     # what's missing, and how to install it
scripts/package.py            # build the release zip + .deb into dist/
```

Run the source-tree versions of the commands below with `scripts/run_gui.sh`,
`scripts/run_bigpicture.sh` and `scripts/run_cli.sh`.

## Usage

**Big Picture** (`janus-bigpicture`, or add it to Steam with `janus-steam-shortcut`):
fullscreen and driven entirely by controller, made for the couch. Pick a game,
every player presses **A** to join, choose the layout and who's playing, then
**Start**. **Y** gets handlers from the handler hub and opens the game's options.

**Desktop GUI** (`janus`): pick a game, split each monitor into slots, drag the
controllers onto the slots, Launch. Every setting, including per-game handler
options, is here.

**Command line** (`janus-cli`):

```bash
janus-cli list                                         # connected controllers and their numbers
janus-cli steam-split 105600 \
    --tile 0:0,0,1920,2160 --tile 1:1920,0,1920,2160    # two tiles, controllers 0 and 1
janus-cli steam-split 105600 --tile 0:0,0,1920,2160@Alice --tile 1:1920,0,1920,2160@Bob
janus-cli profiles list                                # who's who (see below)
janus-cli hub get --all-installed                      # handlers for every installed game
```

**Hotkeys** while playing: **Alt+1** releases the mouse from a game window,
**Alt+2** stops all game windows (GUI sessions).

## Handlers

Each game has a small JavaScript **handler** with what Janus needs to know
about it: launch options, whether it needs Goldberg for multiplayer, where its
saves are, how to fit its resolution to the tile, etc. Janus comes with
handlers for a number of games, and more come from the handler hub (Big
Picture: **Y**; `janus-cli hub`).

Write your own, or change a copy of a bundled one, in **`~/.config/janus/games/`**
(`<game-name>.js`, with a `Game.appid = <appid>;` line). A handler there wins
over a bundled one for the same game. [SCRIPTING.md](SCRIPTING.md) is the
reference.

## Player profiles

A profile is a person, not a player number: a name, a Steam ID for Goldberg,
and their own saves per game. Player 1 is your real Steam account by default
and plays on your real saves; other players get their own isolated saves, the
same ones every time, whatever number they play as. Choose who's playing with
**X** in Big Picture, right-click a slot in the GUI, or `@NAME` on a `--tile`.

## Troubleshooting

- `janus-cli diagnose` shows how Janus sees your Steam install and runtimes.
- Turn on verbose logging (GUI setting, or `-v`); each instance then logs to
  `~/.local/share/janus/logs/<appid>/`. `scripts/hooklog.py <appid>` summarises
  a session.
- `JANUS_NO_HOOK=1` launches exactly as usual but without the controller hook,
  to tell whether the hook is what breaks a game.

## How it works

Every game copy is started with an `LD_PRELOAD` hook library
(`src/hook/janus_hook.c`) that only lets it see its assigned controller, at
every layer a game might read pads from: SDL2, SDL3, evdev
(`/dev/input/event*`, `js*`), hidraw, and Steam Input (Steam's virtual pads
under Proton, the `ISteamInput` interface for native games). Pads are bound by
hardware identity (VID:PID), so a wireless pad that reconnects under a new
number stays with its player. Each copy runs in its own gamescope window,
placed by KWin, with its own Wine prefix and home directory where needed.

## Documentation

SCRIPTING.md and AGENTS_WRITE_HANDLERS.md come with an installed copy; the
others are in this repository.

| File | What |
|------|------|
| [SCRIPTING.md](SCRIPTING.md) | Handler reference: settings, callbacks, `Janus.*` API |
| [AGENTS_WRITE_HANDLERS.md](AGENTS_WRITE_HANDLERS.md) | How to write a handler for a new game (for people and AI agents) |
| [GAMES.md](GAMES.md) | Tested games and their setups |
| [WAYLAND.md](WAYLAND.md) | KDE Plasma Wayland window placement |

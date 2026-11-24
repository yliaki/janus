Janus Split Screen @VERSION@
=============================

Split-screen local multiplayer for Steam games on Linux: each player gets
their own game window and controller.

Requirements
------------
- KDE Plasma on Wayland (Steam Deck desktop mode, Bazzite, Kubuntu, Fedora
  KDE, Arch with Plasma, ...)
- Steam (the native package, not the Flatpak)
- gamescope, from your distribution (Janus doesn't include it):
    Arch/Manjaro/EndeavourOS:  sudo pacman -S gamescope
    Fedora:                    sudo dnf install gamescope
    Ubuntu/Kubuntu 26.04+:     sudo apt install gamescope  (multiverse)
    openSUSE:                  sudo zypper install gamescope
    Debian 13: enable trixie-backports (contrib), then
                               sudo apt install -t trixie-backports gamescope
    SteamOS and Bazzite already have it.
- Python 3.10 or newer (already installed on most systems)
- Recommended: the Steam Linux Runtimes (Steam > Library, show Tools:
  "Steam Linux Runtime 1.0 (scout)", "3.0 (sniper)" and the one your Proton
  version uses). Steam installs most of them the first time you play a game
  that needs them; Janus warns before a launch when one is missing.

Install
-------
Double-click install.sh in this folder (in Dolphin: choose "Execute" when
asked) and follow the dialogs. If it opens in a text editor instead,
right-click it > Properties > Permissions > tick "Is executable" and try
again, or run it from a terminal:  bash install.sh

It installs for your user only (no password needed) and adds
"Janus Split Screen" and "Janus Big Picture" to your application menu. It
also offers to add Janus Big Picture to Steam so you can start it with a
controller.

Uninstall
---------

Run ~/.local/opt/janus/uninstall.sh (double-click it, or from a terminal).
It asks whether to also remove Janus from Steam and whether to keep your
settings, player profiles, saves and the handlers you got from the handler
hub (~/.config/janus/games). Janus's cache (~/.cache/janus) always goes.

Licence
-------
Janus Split Screen is free software: GPL-3.0-or-later (app/LICENSE).
Bundled third-party components keep their own licences, listed in
app/LICENSES/README.md.

Game handlers
-------------
Every game needs a handler script. Janus comes with handlers for a number of
games; write your own (or modified copies) in ~/.config/janus/games/ (see
the README.txt there after installing).

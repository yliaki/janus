# gbe_fork (Goldberg Steam Emulator) — vendored DLLs

This directory holds the [gbe_fork](https://github.com/Detanup01/gbe_fork) builds
that Janus uses to replace Valve's `steam_api` library, enabling LAN multiplayer
without needing multiple logged-in Steam accounts.

## Layout

```
vendor/goldberg/
├── regular/
│   ├── steam_api.dll       # 32-bit Windows (Proton)
│   ├── steam_api64.dll     # 64-bit Windows (Proton)
│   ├── libsteam_api.so     # 64-bit Linux native
│   └── libsteam_api32.so   # 32-bit Linux native (e.g. L4D2)
└── debug/                  # OPTIONAL — only needed for lobby ID detection
    └── (same four files, from the debug build)
```

## Which build to use

- **regular**: smaller, more stable, **does not include the overlay** (Shift+Tab
  GUI for friends/invites). The binary advertises the overlay config keys but
  has no ImGui or rendering code compiled in — Shift+Tab will do nothing.
- **debug**: non-release build with `dbg_log` enabled — writes
  `STEAM_LOG_<random>.log` next to the loaded library. The Janus GUI tails
  this file to detect lobby IDs (manual Copy button + auto-join). Larger
  and slower than release; only enable when you need lobby ID detection.

Toggle in the GUI: **"Use debug gbe_fork build (enables lobby ID detection)"**
under global settings, `Game.goldbergDebug` in a handler, or CLI `--goldberg-debug`.
(An 'experimental' overlay build used to be selectable; it was dropped
2026-10-09 — it was never vendored.)

## Vendored versions

Matched by sha256 against the release archives (2026-10-16):

| Files | gbe_fork release |
|---|---|
| `regular/` (all four) | [release-2026_01_19](https://github.com/Detanup01/gbe_fork/releases/tag/release-2026_01_19) |
| `debug/` (all four) | [release-2026_05_16](https://github.com/Detanup01/gbe_fork/releases/tag/release-2026_05_16) |
| `handlers/payday-2/payload/coldclient/` | [release-2026_05_30](https://github.com/Detanup01/gbe_fork/releases/tag/release-2026_05_30) |

The LGPL source reference for each is the repository at that tag
(`LICENSES/README.md`). Update this table when you replace the files.

## Updating

Download the latest release from <https://github.com/Detanup01/gbe_fork/releases>:

- `emu-win-release.7z` → `release/regular/x64/steam_api64.dll`,
  `release/regular/x32/steam_api.dll` → `vendor/goldberg/regular/`
- `emu-linux-release.tar.bz2` → `release/regular/x64/libsteam_api.so`,
  `release/regular/x32/libsteam_api.so` (rename to `libsteam_api32.so`) →
  `vendor/goldberg/regular/`

For the **debug** variant (used by the lobby watcher), download
`emu-win-debug.7z` and `emu-linux-debug.tar.bz2` from the same release
and copy the equivalent four files (same names, debug-build versions)
into `vendor/goldberg/debug/`. The debug archive is much larger
(~80 MB Windows, ~280 MB Linux) but compresses well — only the four
`steam_api*` libraries need to land in `debug/`.

## Lookup order

`goldberg_dir()` in `src/host/steam.py` uses `<repo>/vendor/goldberg/<variant>/`
(this directory). If it's missing you'll get a warning at launch and Goldberg
won't activate.

## License

gbe_fork is [LGPL-3.0](https://github.com/Detanup01/gbe_fork/blob/main/LICENSE).
Redistribution is permitted.

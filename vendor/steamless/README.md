# Steamless (vendored)

[Steamless](https://github.com/atom0s/Steamless) v3.1.0.5 by atom0s — removes
Valve's **SteamStub DRM wrapper** (PE section `.bind`) from a game exe. Needed
for Goldberg on games whose exe is wrapped: the stub runs before the game,
talks to the Steam client directly (not through `steam_api`), and relaunches
the game via Steam when it wasn't started by Steam — so Goldberg never loads
(How to Fish, 4001890, is the first such game here; PEAK / Lethal Company are
not wrapped).

License: CC BY-NC-ND 4.0 (upstream). Redistributed unmodified, with
attribution; private repo.

Only `Steamless.CLI.exe` + `Plugins/` are used. It is a .NET Framework app;
`scripts/steamless.sh` runs it under Proton's Wine (wine-mono supplies the
runtime) in a dedicated prefix at `~/.local/share/janus/steamless_prefix`.

```bash
scripts/steamless.sh "<install dir>/<Game>.exe"     # -> <Game>.exe.unpacked.exe next to it
```

Check whether an exe is wrapped: `python3 scripts/pe_sections.py <exe>` lists
the PE sections — `.bind` means SteamStub. Janus runs it
automatically for a handler's `Game.steamStubExes` (`src/host/steamstub.py`),
on a copy of the exe, caching the output in `~/.local/share/janus/steamless/`;
the Steam install is never modified. Run by hand, the script writes
`<exe>.unpacked.exe` next to its input.

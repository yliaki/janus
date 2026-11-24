# Ticket proxy DLL (prebuilt)

`steam_api64_ticket.dll` is the **Real-ticket proxy** used by
`Game.goldbergRealTicket` (Content Warning). It's committed so that launching
never needs a Windows cross-compiler.

**It is entirely Janus's own code.** It's built from `src/ticketproxy/proxy.c`
plus jump stubs that `src/ticketproxy/gen_stubs.py` generates from the
export *names* of the Goldberg DLLs in `vendor/goldberg/`. It contains no
Valve or Goldberg code: at runtime it loads Goldberg (`steam_api64_gbe.dll`)
from beside itself. The only other code is the mingw-w64 CRT startup and
libgcc, which the compiler links into any Windows binary. Imports:
`KERNEL32.dll`, `msvcrt.dll`.

## Rebuilding

Rebuild after changing `src/ticketproxy/`, or after updating
`vendor/goldberg/*/steam_api64.dll` (the stub list comes from its exports):

```bash
sudo apt install gcc-mingw-w64-x86-64   # once
scripts/compile_hooks.sh ticketproxy    # or: make -C src/ticketproxy
git add vendor/ticketproxy/steam_api64_ticket.dll   # commit the result
```

`scripts/compile_hooks.sh` (default target) also rebuilds it automatically
when the sources are newer and the cross-compiler is installed. The build is
reproducible (`--no-insert-timestamp`), so rebuilding unchanged sources with
the same compiler doesn't produce a git diff.

## What it doesn't include

The ticket helper (`src/host/steam_ticket.py`) needs a native 64-bit Valve
`libsteam_api.so`. That's Valve's code, so it is **not** in this repo: the
helper borrows one at runtime from any native Linux game in your Steam
libraries (or `--lib PATH`).

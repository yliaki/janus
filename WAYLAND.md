# Janus Split Screen on Wayland (KDE Plasma 6 / KWin)

Empirically-verified reference for running gamescope split-screen on a **native
Wayland** KDE Plasma 6 / KWin host. Everything here was validated on a single 4K
display (`HDMI-A-1`, 3840×2160) at **fractional scale 2.5** (logical desktop
1536×864), gamescope 3.16, KWin 6.3.

The X11 host path (xdotool / wmctrl / xprop / `_NET_ACTIVE_WINDOW` focus-watch)
**does not work** on this host — see "Why the X11 path is dead" at the bottom. It
was removed from Janus on 2026-10-09; KWin Wayland is the only placement path.

---

## Toolchain (automated — `steam-split`)

The validated Path A flow is wired into the CLI. One command sets scale→1, hides
the panel, writes the KWin window rules, launches N tiled gamescope+game
instances (each pinned via `SDL_VIDEO_X11_WMCLASS`, with `-o == -r`), waits, and
restores the desktop on exit / Ctrl-C / crash:

```bash
python3 src/host/main.py steam-split <appid> \
  --tile 0:0,0,1920,2160 \
  --tile 1:1920,0,1920,2160 \
  [--goldberg] [--rate 60] [--output HDMI-A-1] [--isolation secondary]
```

`--tile controller:x,y,w,h` — native px at scale 1.0, repeatable. **Tested
working** with PEAK (appid 3527290), two physical Xbox pads, 50/50 split, clean
teardown.

Modules:
- `src/host/wayland_session.py` — `WaylandSession`: scale / panel / KWin rules
  with bulletproof restore (atexit + SIGINT/SIGTERM).
- `src/host/split_session.py` — `launch_split_session`: orchestrates N instances.
- `src/host/steam.py` — `launch_steam_game(..., wmclass=…)` sets the per-tile
  `SDL_VIDEO_X11_WMCLASS`.

The sections below document the underlying mechanics that this command automates.

---

## 0. Core concepts

### Physical vs logical pixels
- **Physical** = real panel dots: 3840×2160 (fixed hardware).
- **Scale** = KDE display magnification: 2.5 here.
- **Logical** = coordinate system after dividing by scale: 3840/2.5 = **1536**,
  2160/2.5 = **864**. The desktop, KWin geometry, and window positions are all in
  **logical** px. `physical = logical × scale`.

### Who controls what
| Thing | Owner | Units |
|-------|-------|-------|
| Window **position** | KWin (rule or script) | logical |
| Window **size** | **gamescope** (`-W/-H`), not KWin | see backend table |
| Decorations / above / skiptaskbar | KWin | — |
| Game framerate when unfocused | gamescope (`-o`) | — |

KWin will **not** resize a gamescope window (the client owns its surface size).
KWin force-rules can set position/noborder/above but a force *size* rule is ignored.

### gamescope `-W/-H` units depend on the backend
| Backend | `-W/-H` units | Per-instance identity | Sharpness @ 2.5 |
|---------|---------------|------------------------|-----------------|
| `--backend sdl` | **logical** px | **Yes** — `SDL_VIDEO_WAYLAND_WMCLASS` sets app_id | soft (KWin upscales) unless scale=1 |
| `--backend wayland` | **physical** px | No — app_id hardcoded `gamescope` | crisp (renders at physical, KWin 1:1) |

`-w/-h` = the resolution the **game** renders at (gamescope upscales `-w→-W` with
`-F` fsr/nis/linear). `-W/-H` = gamescope's output buffer / host-window size.

---

## 1. Launching multiple windows with correct scaling

Two validated paths. Both give crisp, borderless, positioned tiles.

### Path A — scale-1.0 session + SDL backend + window rules (no KWin script)
Flip the display to scale 1.0 so logical == physical and KWin does no upscaling;
then the SDL backend is crisp and `SDL_VIDEO_WAYLAND_WMCLASS` gives each instance a
distinct app_id that **window rules** can match. Restore scale afterward.

```bash
# 1. Save + set scale 1.0 (desktop UI becomes tiny; tiles cover it)
kscreen-doctor output.HDMI-A-1.scale.1

# 2. Write rules BEFORE launching (Force rules apply at window MAP time).
#    50/50 split of 3840x2160:  left 0,0 1920w  |  right 1920,0 1920w
cat > ~/.config/kwinrulesrc <<'EOF'
[General]
count=2
rules=p1,p2

[p1]
Description=janus p1
wmclass=janus-p1
wmclassmatch=1
wmclasscomplete=false
position=0,0
positionrule=4
noborder=true
noborderrule=4
above=true
aboverule=4

[p2]
Description=janus p2
wmclass=janus-p2
wmclassmatch=1
wmclasscomplete=false
position=1920,0
positionrule=4
noborder=true
noborderrule=4
above=true
aboverule=4
EOF
qdbus6 org.kde.KWin /KWin org.kde.KWin.reconfigure

# 3. Launch (scale=1 so -W/-H are physical == logical). -o == -r (see §3).
SDL_VIDEO_WAYLAND_WMCLASS=janus-p1 gamescope --backend sdl \
  -W 1920 -H 2160 -w 1920 -h 2160 -r 60 -o 60 -b -- <game> &
SDL_VIDEO_WAYLAND_WMCLASS=janus-p2 gamescope --backend sdl \
  -W 1920 -H 2160 -w 1920 -h 2160 -r 60 -o 60 -b -- <game> &

# 4. On exit: restore
rm -f ~/.config/kwinrulesrc
qdbus6 org.kde.KWin /KWin org.kde.KWin.reconfigure
kscreen-doctor output.HDMI-A-1.scale.2.5
```

`wmclassmatch=1` = exact, `wmclasscomplete=false` = match resourceClass only.
The rule `...rule=4` enum value is **Force** (`2`=Apply Initially, `3`=Remember,
`4`=Force, `5`=Apply Now, `6`=Force Temporarily). Match enum: `0`=unimportant,
`1`=exact, `2`=substring, `3`=regex.

**Pro:** declarative, no script. **Con:** flips the whole desktop scale for the
session.

### Path B — wayland backend + pid-matched KWin script (no scale change)
Keep the desktop at 2.5. The wayland backend renders at physical → crisp, but all
instances share app_id `gamescope`, so position by **unique pid** via a KWin script.

```bash
# -W/-H are PHYSICAL px = logical_tile × scale. 768x864 logical tile -> 1920x2160.
gamescope --backend wayland -W 1920 -H 2160 -w 1920 -h 2160 -r 60 -o 60 -b -- <game> & P1=$!
gamescope --backend wayland -W 1920 -H 2160 -w 1920 -h 2160 -r 60 -o 60 -b -- <game> & P2=$!

# Generate + load a placement script keyed on the launch pids (positions LOGICAL px):
cat > /tmp/place.js <<EOF
var L=$P1, R=$P2;
var ws = workspace.windowList();
for (var i=0;i<ws.length;i++){
  var w=ws[i]; if (w.resourceClass!=="gamescope") continue;
  w.noBorder=true; w.keepAbove=true;
  var g=w.frameGeometry;                       // Qt.rect() is NOT available — use a plain object
  if (w.pid===L) w.frameGeometry={x:0,   y:0, width:g.width, height:g.height};
  else if (w.pid===R) w.frameGeometry={x:768, y:0, width:g.width, height:g.height};
}
EOF
id=$(qdbus6 org.kde.KWin /Scripting org.kde.kwin.Scripting.loadScript /tmp/place.js)
qdbus6 org.kde.KWin /Scripting/Script$id org.kde.kwin.Script.run
```

For robustness, hook `workspace.windowAdded` in a persistent script so placement
happens at map time (no flash). **Note:** assigning `frameGeometry` **steals
focus** — place the *intended* focused tile last, or re-activate it afterward.

### Multi-monitor (one instance per display) — neither rules nor script needed
gamescope fullscreens itself natively on a chosen output:
```bash
gamescope -f --display-index 0 -r 60 -o 60 -- <game> &   # display 0
gamescope -f --display-index 1 -r 60 -o 60 -- <game> &   # display 1
```

---

## 2. Hiding the KDE panel

The old xdotool-unmap-plasmashell trick is **dead** (plasmashell is a native
Wayland client, invisible to X tools). Use plasmashell's own scripting API. Setting
`hiding=autohide` also **releases the panel's strut**, so KWin's work area becomes
the full screen and tiles get full height (otherwise they're clamped ~44px short).

```bash
PS="qdbus6 org.kde.plasmashell /PlasmaShell org.kde.PlasmaShell.evaluateScript"

# Read current state (save it — default is "none"):
$PS 'print(panels()[0].hiding)'                 # -> none

# Hide (releases strut; work area -> full 1536x864):
$PS 'panels()[0].hiding="autohide"; print(panels()[0].hiding)'

# Restore on exit:
$PS 'panels()[0].hiding="none"'
```

Notes:
- `evaluateScript` returns the **`print()` buffer**, not the last expression
  (a bare `1+1` returns `""`; `print(1+1)` returns `2`).
- `autohide` reveals on bottom-edge hover. Alternative: `dodgewindows` (hide only
  while a window covers it).
- `hiding` is **persistent** — always restore it (treat like the scale flip).
- Verify the work area expanded with a KWin script:
  `workspace.clientArea(KWin.MaximizeArea, workspace.activeScreen, workspace.currentDesktop)`.

---

## 3. Focus & framerate

**Focus faking is unnecessary under gamescope.** The game is the only client of
gamescope's internal Xwayland and is always focused *there*; it never sees a host
`FocusOut`, and gamescope sets `SDL_VIDEO_MINIMIZE_ON_FOCUS_LOSS` so SDL games don't
minimize. A backgrounded tile's game does **not** pause. The hook's
focus-spoofing / input-suppression / pointer-freeze / `JANUS_HOST_DISPLAY` focus-watch machinery targeted X11
host focus that doesn't exist on Wayland and was removed from the hook (2026-10-09).

**The one real focus-sensitive behavior is gamescope's unfocused throttle.**
`-o/--nested-unfocused-refresh` lowers the game's framerate when its host window is
unfocused. With two same-host tiles, only one is ever host-focused, so the other
would throttle. **Fix: set `-o` equal to `-r`** (e.g. `-r 60 -o 60`) so backgrounded
tiles stay at full framerate. Verified: with `-o 5` the unfocused cube visibly
stutters; with `-o 60` both stay smooth.

**Keyboard bleed** is handled by KWin natively (keyboard goes only to the focused
surface). Controllers are read from evdev regardless of focus — that's Janus's core
and it's focus-independent.

**Native focus tracking** (if ever needed, e.g. OSK): KWin exposes it directly —
`workspace.activeWindow` and per-window `.active`. This replaces the dead
`_NET_ACTIVE_WINDOW` focus-watch thread.

---

## 4. Gotchas / cheatsheet

- **Write KWin rules BEFORE launching** — Force rules apply at map time; a window
  already mapped when you `reconfigure` does **not** snap.
- **`frameGeometry` assignment steals focus** (Path B) — place the focused tile last.
- **Kill gamescope with `pkill -9 -f gamescope`** — gentler/exact-name kills leave
  zombie `gamescope`/`gamescopereaper`. Run it as its OWN command — `-f` matches any
  cmdline containing "gamescope", so it self-matches (and kills) a shell that also
  contains a `gamescope ...` launch in the same line.
- **Inspect what KWin sees** (native windows, invisible to xdotool) via a KWin
  script dumping `workspace.windowList()` → `resourceClass / resourceName / caption /
  frameGeometry / pid / output / active` to the journal (`journalctl --user`).
- **Set/restore display scale:** `kscreen-doctor output.HDMI-A-1.scale.1` /
  `.scale.2.5`. Read current: `kscreen-doctor -o`.

### Why the X11 path is dead on Wayland
- Native Wayland gamescope windows are **invisible to `xdotool`/`wmctrl`/`xprop`**.
- Host XWayland `_NET_ACTIVE_WINDOW` stays `0x0`; native windows never appear there
  → the hook's focus-watch thread can't track focus.
- KWin doesn't expose `wlr-foreign-toplevel` / plasma-window-management globally, so
  wlroots tools (`wlrctl`) can't drive windows either.
- ⇒ Host-side window control on this platform is **KWin only** (rules or scripting),
  in **logical** coordinates.

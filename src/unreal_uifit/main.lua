-- JanusUiFit: UE4SS Lua mod for Janus (src/host/unreal.py, Game.unrealUiFit).
--
-- Sets Unreal's UI scale (UserInterfaceSettings.ApplicationScale, read every
-- frame) to the largest value <= JANUS_UI_SCALE at which every visible
-- widget stays on screen and HUD pieces don't run into each other; never
-- below 1.0 (the game's own size). Re-evaluated twice a second, so each menu
-- gets its own fit, like the Unity plugin (src/unity_uiscale/).
--
-- Unreal's computed geometry isn't readable from Lua (FGeometry has no
-- reflected fields), so the layout is computed here from the widgets' own
-- data: canvas slots (anchors, offsets, alignment, auto size) and desired
-- sizes. Rules:
--   * a point-anchored canvas child is a rect of its own size;
--   * a stretched slot holding a box/panel spills its desired size right /
--     down from the slot's left / top edge when it doesn't fit the slot;
--   * images/blur/borders in stretched slots fill the slot, a ScaleBox
--     scales its content to its slot: ignored;
--   * nested canvases are laid out recursively, and UserWidgets, Overlay/
--     SafeZone/NamedSlot wrappers and switchers/CommonUI stacks (active page)
--     are looked through: their children fill their rect; anything else
--     (boxes, text) is one element of its minimum size in that rect;
--   * heights are minimum heights (minSize): a vertical ScrollBox counts as
--     MIN_SCROLL, not its whole list; a SizeBox as its content even with a
--     height override of 0 (pause menu); a WidgetSwitcher as its active page;
--   * a point-anchored element offset by more than 3/4 of the screen at 1.0
--     is placed for one fixed resolution (DRG's version text: bottom-right
--     anchor, 1030 up) and left out;
--   * everything else must stay 3% inside the screen (wrapped text grows
--     taller when scaled up than the 1.0 desired size says);
--   * HUD pieces must not run into each other (only point-anchored elements
--     of the same viewport root, each smaller than a quarter of the screen;
--     separate roots are separate layers: a menu over the HUD);
--   * small pieces already over the screen edge at 1.0 are the game's design
--     and ignored; large ones (a menu too big for a narrow tile) may not get
--     further over it; anything
--     overlapping at 1.0 is the game's own design and ignored.
-- The search uses this arithmetic only (no re-layout), so there is no flicker.

local MAX = tonumber(os.getenv("JANUS_UI_SCALE") or "") or 1.0
-- Development aid: a Lua file (Windows path, e.g. Z:\\tmp\\probe.lua) run after
-- every fit, edited while the game runs (widget dumps for new games).
local DEV = os.getenv("JANUS_UI_DEV")
local VERBOSE = os.getenv("JANUS_VERBOSE") == "1"
local FILL = {Image = true, BackgroundBlur = true, Border = true, Throbber = true,
              ScaleBox = true}   -- scales its content to its slot: never overflows

local function log(s) print("[JanusUiFit] " .. s .. "\n") end

local function hidden(w) local v = w:GetVisibility(); return v == 1 or v == 2 end
local function cls(o) return o:GetClass():GetFName():ToString() end

-- The smallest height a widget can be laid out in: a vertical ScrollBox in a Fill slot
-- shrinks to MIN_SCROLL (it scrolls), a VerticalBox stacks its children's
-- minimums, other containers take their largest child's, a WidgetSwitcher
-- only its active page; a SizeBox is as big as its content even when it
-- reports less (a height override of 0 still draws the content: pause menu).
local MIN_SCROLL = 200
local imageCap = math.huge      -- half the layout height at scale 1.0 (set per fit)
-- WidgetSwitcher and CommonUI activatable-widget stacks/switchers (RV There
-- Yet?'s menus) show only their active page.
local function isSwitcher(w)
  local c = cls(w)
  if c == "WidgetSwitcher" or c:find("Stack$") or c:find("Switcher$") then return true end
  local ok, super = pcall(function() return w:GetClass():GetSuperStruct():GetFName():ToString() end)
  return ok and super and (super:find("Stack$") or super:find("Switcher$")) and true or false
end
local function children(w)
  local out = {}
  if isSwitcher(w) then
    local ok, a = pcall(function() return w:GetActiveWidget() end)
    if ok and a and a:IsValid() then out[1] = a end
    return out
  end
  pcall(function() w.Slots:ForEach(function(_, e) local s = e:get()
    if s and s:IsValid() and s.Content:IsValid() then out[#out + 1] = s.Content end end) end)
  pcall(function() local r = w.WidgetTree.RootWidget; if r and r:IsValid() then out[#out + 1] = r end end)
  return out
end
local function minSize(w, depth)
  local d = w:GetDesiredSize()
  if depth > 14 then return d.X, d.Y end
  local c = cls(w)
  if c == "CanvasPanel" then return d.X, d.Y end   -- children are placed, not stacked
  if c == "ScaleBox" then return d.X, d.Y end   -- scales its content: its own size
  -- Images taller than half the screen are full-bleed decoration (scaled,
  -- clipped, or deliberately running off screen: RV There Yet?'s 2048-tall
  -- signpost pole); smaller ones (button backgrounds) size their container.
  if (c == "Image" or c == "Throbber") and d.Y > imageCap then return d.X, 0 end
  if c == "ScrollBox" then
    local vertical = true
    pcall(function() vertical = w.Orientation ~= 0 end)
    if vertical then return d.X, math.min(d.Y, MIN_SCROLL) end
    return d.X, d.Y
  end
  local kids = {}
  for _, k in ipairs(children(w)) do if not hidden(k) then kids[#kids + 1] = k end end
  if #kids == 0 then return d.X, d.Y end
  local sumD, sumM, maxD, maxM, maxX = 0, 0, 0, 0, 0
  for _, k in ipairs(kids) do
    local kd = k:GetDesiredSize()
    local kx, ky = minSize(k, depth + 1)
    -- In a VerticalBox only a Fill slot gives a child less than it asks for;
    -- an Auto slot lays it out at its desired height (FFW's accessibility
    -- list: a ScrollBox in an Auto slot runs off screen instead of scrolling).
    if c == "VerticalBox" then
      local ok, rule = pcall(function() return k.Slot.Size.SizeRule end)
      if ok and rule == 0 then ky = kd.Y end
    end
    sumD, sumM = sumD + kd.Y, sumM + ky
    maxD, maxM, maxX = math.max(maxD, kd.Y), math.max(maxM, ky), math.max(maxX, kx)
  end
  local m
  if c == "SizeBox" then
    -- An explicit height wins; an override of 0 (FFW's pause menu) still
    -- draws its content, so it counts as the content.
    m = maxM
    pcall(function()
      if w.bOverride_HeightOverride and w.HeightOverride > 0 then m = w.HeightOverride end
      if w.bOverride_MinDesiredHeight then m = math.max(m, w.MinDesiredHeight) end
      if w.bOverride_MaxDesiredHeight and w.MaxDesiredHeight > 0 then m = math.min(m, w.MaxDesiredHeight) end
    end)
  else
    local vbox = c == "VerticalBox"
    m = (vbox and sumM or maxM) + math.max(0, vbox and (d.Y - sumD) or (d.Y - maxD))
  end
  -- Never more than the widget asks for, unless it asks for nothing while
  -- its content still draws (SizeBox with a height override of 0).
  if d.Y > 0 then m = math.min(m, d.Y) end
  return (c == "SizeBox") and math.max(d.X, maxX) or d.X, m
end

-- Containers that only stack their children on top of each other in their
-- own rect: looked through, so canvases inside them are laid out too.
local PASS = {Overlay = true, SafeZone = true, NamedSlot = true}

local function hasCanvas(w, depth)
  if depth > 6 or hidden(w) then return false end
  local c = cls(w)
  if c == "CanvasPanel" then return true end
  local uw = false
  pcall(function() uw = w.WidgetTree ~= nil and w.WidgetTree:IsValid() end)
  if not PASS[c] and not uw and not isSwitcher(w) then return false end
  for _, k in ipairs(children(w)) do if hasCanvas(k, depth + 1) then return true end end
  return false
end

local collect

local place

-- GridPanel: rows/columns with fill ratios (fill 0 = auto: largest child),
-- each child in its cell minus padding, aligned (0 fill, 1 start, 2 centre,
-- 3 end). RV There Yet?'s main menu.
local function placeGrid(g, x0, y0, x1, y1, out, depth)
  local items, rf, cf = {}, {}, {}
  pcall(function() g.RowFill:ForEach(function(_, e) rf[#rf + 1] = e:get() end) end)
  pcall(function() g.ColumnFill:ForEach(function(_, e) cf[#cf + 1] = e:get() end) end)
  local nr, nc = #rf, #cf
  g.Slots:ForEach(function(_, e)
    local s = e:get()
    if not (s and s:IsValid()) then return end
    local c = s.Content
    if not (c and c:IsValid()) or hidden(c) then return end
    local it = {c = c, r = s.Row, cc = s.Column, rs = math.max(1, s.RowSpan), cs = math.max(1, s.ColumnSpan),
                p = s.Padding, h = s.HorizontalAlignment, v = s.VerticalAlignment}
    it.dx, it.dy = minSize(c, 0)
    nr = math.max(nr, it.r + it.rs); nc = math.max(nc, it.cc + it.cs)
    items[#items + 1] = it
  end)
  local function axis(fill, n, total, sizeOf)
    local size, sumF, auto = {}, 0, 0
    for i = 0, n - 1 do
      local f = fill[i + 1] or 0
      if f > 0 then sumF = sumF + f; size[i] = 0
      else size[i] = sizeOf(i); auto = auto + size[i] end
    end
    local rest = math.max(0, total - auto)
    for i = 0, n - 1 do local f = fill[i + 1] or 0; if f > 0 then size[i] = rest * f / sumF end end
    local off, acc = {}, 0
    for i = 0, n do off[i] = acc; acc = acc + (size[i] or 0) end
    return off
  end
  local rowOff = axis(rf, nr, y1 - y0, function(i)
    local m = 0
    for _, it in ipairs(items) do if it.r == i and it.rs == 1 then m = math.max(m, it.dy + it.p.Top + it.p.Bottom) end end
    return m end)
  local colOff = axis(cf, nc, x1 - x0, function(i)
    local m = 0
    for _, it in ipairs(items) do if it.cc == i and it.cs == 1 then m = math.max(m, it.dx + it.p.Left + it.p.Right) end end
    return m end)
  for _, it in ipairs(items) do
    local cx0 = x0 + colOff[it.cc] + it.p.Left
    local cx1 = x0 + colOff[math.min(nc, it.cc + it.cs)] - it.p.Right
    local cy0 = y0 + rowOff[it.r] + it.p.Top
    local cy1 = y0 + rowOff[math.min(nr, it.r + it.rs)] - it.p.Bottom
    local sx, sy = it.h == 0, it.v == 0
    if not sx then
      local w = it.dx
      if it.h == 2 then cx0 = (cx0 + cx1 - w) / 2 elseif it.h == 3 then cx0 = cx1 - w end
      cx1 = cx0 + w
    end
    if not sy then
      local h = it.dy
      if it.v == 2 then cy0 = (cy0 + cy1 - h) / 2 elseif it.v == 3 then cy0 = cy1 - h end
      cy1 = cy0 + h
    end
    place(it.c, cx0, cy0, cx1, cy1, sx, sy, nil, out, depth + 1)
  end
end

-- Place widget c in the rect its canvas slot gives it (sx/sy: stretched).
place = function(c, rx0, ry0, rx1, ry1, sx, sy, o, out, depth)
  if depth > 10 or hidden(c) then return end
  local kind = cls(c)
  if kind == "CanvasPanel" then collect(c, rx0, ry0, rx1, ry1, out, depth + 1); return end
  if kind == "GridPanel" then placeGrid(c, rx0, ry0, rx1, ry1, out, depth); return end
  local isUserWidget = false
  pcall(function() isUserWidget = c.WidgetTree ~= nil and c.WidgetTree:IsValid() end)
  if PASS[kind] or isUserWidget or isSwitcher(c) then
    -- Children of a wrapper fill its rect.
    for _, k in ipairs(children(c)) do place(k, rx0, ry0, rx1, ry1, true, true, nil, out, depth + 1) end
    return
  end
  if FILL[kind] then return end
  local dx, dy = minSize(c, 0)
  if dx <= 0 or dy <= 0 then return end
  if sx and dx > rx1 - rx0 then rx1 = rx0 + dx end
  if sy and dy > ry1 - ry0 then ry1 = ry0 + dy end
  -- Keyed by full path: short names repeat across widgets (VerticalBox_0).
  out[#out + 1] = {c:GetFullName(), rx0, ry0, rx1, ry1, short = c:GetFName():ToString(),
                   point = not (sx or sy),
                   ox = (o and not sx) and math.abs(o.Left) or 0, oy = (o and not sy) and math.abs(o.Top) or 0}
end

collect = function(canvas, x0, y0, x1, y1, out, depth)
  if depth > 10 then return end
  local W, H = x1 - x0, y1 - y0
  canvas.Slots:ForEach(function(_, e)
    local s = e:get()
    if not (s and s:IsValid()) then return end
    local c = s.Content
    if not (c and c:IsValid()) or hidden(c) then return end
    local L = s.LayoutData
    local amin, amax, o = L.Anchors.Minimum, L.Anchors.Maximum, L.Offsets
    local sx, sy = amax.X > amin.X, amax.Y > amin.Y
    local dx, dy = 0, 0
    if (not sx or not sy) and s.bAutoSize then dx, dy = minSize(c, 0) end
    local rx0, ry0, rx1, ry1
    if sx then rx0, rx1 = x0 + amin.X * W + o.Left, x0 + amax.X * W - o.Right
    else
      local w = s.bAutoSize and dx or o.Right
      rx0 = x0 + amin.X * W + o.Left - L.Alignment.X * w; rx1 = rx0 + w
    end
    if sy then ry0, ry1 = y0 + amin.Y * H + o.Top, y0 + amax.Y * H - o.Bottom
    else
      local h = s.bAutoSize and dy or o.Bottom
      ry0 = y0 + amin.Y * H + o.Top - L.Alignment.Y * h; ry1 = ry0 + h
    end
    place(c, rx0, ry0, rx1, ry1, sx, sy, o, out, depth)
  end)
end

-- A viewport root: its own rect is the whole screen.
local function layoutRoot(r, W, H, out)
  place(r, 0, 0, W, H, true, true, nil, out, 0)
end

local function overlap(a, b) return a[2] < b[4] and b[2] < a[4] and a[3] < b[5] and b[3] < a[5] end

local uis, wll
local lastSig
local lastRoots, stableTicks = nil, 0
local STABLE_TICKS = 3          -- x 0.5 s
local frozenUntil = 0
local loading = false

local function fit()
  uis = uis or StaticFindObject("/Script/Engine.Default__UserInterfaceSettings")
  wll = wll or StaticFindObject("/Script/UMG.Default__WidgetLayoutLibrary")
  if not (uis and uis:IsValid() and wll and wll:IsValid()) then uis, wll = nil, nil; return end
  local roots, ctx = {}, nil
  for _, w in ipairs(FindAllOf("UserWidget") or {}) do
    if w:IsValid() and w:IsInViewport() and not hidden(w) then roots[#roots + 1] = w; ctx = ctx or w end
  end
  if not ctx then return end
  -- Only on a settled screen: the same roots for STABLE_TICKS checks in a
  -- row and no map load in progress. Changing the scale while a level loads
  -- and widgets are being built/torn down crashed DRG (access violation
  -- ~3 s after a change during the space-rig load).
  local rnames = {}
  for _, r in ipairs(roots) do rnames[#rnames + 1] = r:GetFullName() end
  local rootSig = table.concat(rnames, ",")
  if rootSig ~= lastRoots then lastRoots, stableTicks = rootSig, 0; return end
  stableTicks = stableTicks + 1
  if stableTicks < STABLE_TICKS or os.time() < frozenUntil then return end
  local vp = wll:GetViewportSize(ctx)
  local cur = uis.ApplicationScale
  if vp.X <= 0 or vp.Y <= 0 or cur <= 0 then return end
  local base = wll:GetViewportScale(ctx) / cur
  imageCap = 0.5 * vp.Y / base

  local function layout(a)
    local W, H = vp.X / (base * a), vp.Y / (base * a)
    local out = {}
    for i, r in ipairs(roots) do
      local n0 = #out
      layoutRoot(r, W, H, out)
      for j = n0 + 1, #out do out[j].root = i end
    end
    return out, W, H
  end

  local ref, W1, H1 = layout(1.0)
  local off, edge, ovl = {}, {}, {}
  local fixed, big = {}, {}
  for _, e in ipairs(ref) do
    if e.ox > 0.75 * W1 or e.oy > 0.75 * H1 then fixed[e[1]] = true end
    -- Overlaps are only checked between compact pieces (HUD corners, lists);
    -- large containers are mostly empty space.
    if (e[4] - e[2]) * (e[5] - e[3]) > 0.25 * W1 * H1 then big[e[1]] = true end
  end
  for i, e in ipairs(ref) do
    local ov = math.max(-e[2], -e[3], e[4] - W1, e[5] - H1)
    if ov > 1 then
      if (e[4] - e[2]) * (e[5] - e[3]) < 0.1 * W1 * H1 then fixed[e[1]] = true   -- small: by design
      else off[e[1]] = math.max(off[e[1]] or 0, ov) end
    end
    if e[2] < 0.03 * W1 or e[3] < 0.03 * H1 or e[4] > 0.97 * W1 or e[5] > 0.97 * H1 then edge[e[1]] = true end
    for j = i + 1, #ref do
      if e.point and ref[j].point and overlap(e, ref[j]) then ovl[e[1] .. "|" .. ref[j][1]] = true end
    end
  end

  local function problem(a)
    local out, W, H = layout(a)
    -- Safe-area margin (scaled-up text wraps taller than predicted), except
    -- for things the game itself puts at the very edge at 1.0.
    for _, e in ipairs(out) do if not fixed[e[1]] then
      local mx, my = 0.03 * W, 0.03 * H
      if edge[e[1]] then mx, my = -1, -1 end
      if off[e[1]] then
        -- Already over the edge at 1.0: may not get any further over it.
        if math.max(-e[2], -e[3], e[4] - W, e[5] - H) > off[e[1]] + 1 then
          return string.format("%s further off screen (%.0f,%.0f-%.0f,%.0f in %.0fx%.0f; %.0f over at 1.0)",
                               e.short, e[2], e[3], e[4], e[5], W, H, off[e[1]])
        end
      elseif e[2] < mx or e[3] < my or e[4] > W - mx or e[5] > H - my then
        return string.format("%s off screen (%.0f,%.0f-%.0f,%.0f in %.0fx%.0f)", e.short, e[2], e[3], e[4], e[5], W, H)
      end end
    end
    for i = 1, #out do for j = i + 1, #out do
      local p, q = out[i], out[j]
      if p.point and q.point and p.root == q.root and not fixed[p[1]] and not fixed[q[1]]
         and not big[p[1]] and not big[q[1]] and overlap(p, q)
         and not ovl[p[1] .. "|" .. q[1]] then
        return string.format("%s overlaps %s (%.0f,%.0f-%.0f,%.0f / %.0f,%.0f-%.0f,%.0f)", p.short, q.short,
                             p[2], p[3], p[4], p[5], q[2], q[3], q[4], q[5])
      end
    end end
  end

  local best, why = MAX, nil
  if problem(MAX) then
    local lo, hi = 1.0, MAX
    for _ = 1, 8 do local mid = (lo + hi) / 2; if problem(mid) then hi = mid else lo = mid end end
    best = math.floor(lo * 20 + 1e-6) / 20
    why = problem(best + 0.05)
  end
  -- Desired sizes shift a little with the scale (text wrapping): keep a
  -- scale that still fits unless a clearly bigger one does.
  if cur >= 1.0 and cur <= MAX + 1e-6 and best < cur + 0.1 and not problem(cur) then best = cur end
  if math.abs(best - cur) > 1e-3 then uis.ApplicationScale = best end

  local names = {}
  for _, r in ipairs(roots) do names[#names + 1] = cls(r) end
  local sig = string.format("%s %.0fx%.0f -> %.2f", table.concat(names, ","), vp.X, vp.Y, best)
  if sig ~= lastSig then
    lastSig = sig
    if VERBOSE or math.abs(best - cur) > 1e-3 then
      log(sig .. (why and ("  (bigger: " .. why .. ")") or ""))
    end
  end
end

-- Map loads: no scale changes from the start of a load until 3 s after it.
local hooked = pcall(function()
  RegisterLoadMapPreHook(function() loading = true end)
  RegisterLoadMapPostHook(function() loading = false; frozenUntil = os.time() + 3 end)
end)

-- Resizing (dynamic players, src/host/resize.py): $JANUS_SIZE_FILE holds
-- "WxH", rewritten by Janus when the tiles re-flow; the game is set to that
-- size, windowed (a full-screen window would cover gamescope's whole
-- monitor-sized screen). Re-applied a few times after each change, in case
-- the game applies its own settings meanwhile.
local SIZE_FILE = os.getenv("JANUS_SIZE_FILE")
if SIZE_FILE and SIZE_FILE:sub(1, 1) == "/" then SIZE_FILE = "Z:" .. SIZE_FILE:gsub("/", "\\") end
local sizeWant, sizeLeft, sizeNext = nil, 0, 0
local function resize()
  local f = io.open(SIZE_FILE, "r")
  if not f then return end
  local s = f:read("*l") or ""
  f:close()
  if s ~= "" and s ~= sizeWant then sizeWant, sizeLeft, sizeNext = s, 4, 0 end
  if sizeLeft <= 0 or os.time() < sizeNext then return end
  sizeLeft, sizeNext = sizeLeft - 1, os.time() + 3
  local pc = FindFirstOf("PlayerController")
  local ksl = StaticFindObject("/Script/Engine.Default__KismetSystemLibrary")
  if not pc or not pc:IsValid() or not ksl or not ksl:IsValid() then sizeLeft = sizeLeft + 1; return end
  log("r.SetRes " .. sizeWant .. "w")
  ksl:ExecuteConsoleCommand(pc, "r.SetRes " .. sizeWant .. "w", pc)
end

log(string.format("loaded, UI scale up to %.2f%s%s", MAX, hooked and "" or " (no map-load hooks)",
                  SIZE_FILE and ", resizing" or ""))
local lastErr
LoopAsync(500, function()
  ExecuteInGameThread(function()
    local ok, err = true, nil
    if SIZE_FILE then ok, err = pcall(resize) end
    if ok and not loading and MAX > 1.0001 then ok, err = pcall(fit) end
    if ok and DEV then ok, err = pcall(dofile, DEV) end
    if not ok and tostring(err) ~= lastErr then lastErr = tostring(err); log("error: " .. lastErr) end
  end)
  return false
end)

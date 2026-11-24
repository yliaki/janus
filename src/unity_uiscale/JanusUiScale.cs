// Janus UI Scale — a BepInEx 5 plugin that makes a Unity game's UI bigger.
//
// Unity UIs (uGUI) are sized by a CanvasScaler on each root canvas. Every
// root canvas that gets enabled is registered and scaled by a factor:
//   ScaleWithScreenSize  -> referenceResolution / factor
//   other modes          -> scaleFactor * factor
// Only scene objects are touched (never prefab assets), so an instantiated
// prefab starts from its original values.
//
// The factor ($JANUS_UI_SCALE) is a maximum ("fit"): when a menu opens
// (checked every frame, before rendering) its canvas is binary-searched
// within that frame to the largest factor whose layout is no worse than the
// menu's intended one (its full design width — below 1 in tiles narrower than
// the design aspect): no text squeezed (more lines, spilling, auto-sized down)
// and nothing newly off screen. Re-fitted once the menu has
// settled (animations), and remembered per menu state so a menu seen before
// opens at once. Content that still overflows is logged (once per canvas +
// menu state) to BepInEx/LogOutput.log. The factor falls back to
// BepInEx/config/janus.uiscale.cfg when $JANUS_UI_SCALE is unset.
//
// Resizing (dynamic players, src/host/resize.py): with $JANUS_SIZE_FILE set
// the game is kept at the size in that file ("WxH", windowed), polled twice a
// second; Janus rewrites it when the tiles re-flow. At factor 1 the UI is
// left alone entirely (the plugin is then only there to resize).
using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text;
using BepInEx;
using BepInEx.Configuration;
using BepInEx.Logging;
using HarmonyLib;
using UnityEngine;
using UnityEngine.UI;

namespace Janus.UiScale
{
    [BepInPlugin("janus.uiscale", "Janus UI Scale", "0.9.0")]
    public class Plugin : BaseUnityPlugin
    {
        internal static ManualLogSource Log;
        internal static float Factor = 1f;
        // Experimental (JANUS_UI_GROUPS=1, Game.unityUiScaleGroups): when a
        // canvas can't take the whole factor, grow its separate element
        // groups (HUD corners) individually toward the screen centre.
        internal static bool Groups;
        // MonoBehaviour type names to switch off on UI canvases
        // (JANUS_UI_DISABLE, Game.unityUiScaleDisable): game scripts that keep
        // re-positioning UI and would undo the fit (Content Warning's
        // AspectRatioUISetter, which applies per-aspect HUD positions).
        internal static readonly HashSet<string> Disable = new HashSet<string>();
        // (JANUS_UI_IGNORE, Game.unityUiScaleIgnore): GameObject names whose
        // graphics (and children) never limit the fit — markers that follow
        // world points and get pinned to the screen edge, decorations meant to
        // hang off screen. Otherwise every such move shrinks and regrows the
        // canvas (Mycopunk's ping markers).
        internal static readonly HashSet<string> Ignore = new HashSet<string>();

        void Awake()
        {
            Log = Logger;
            Factor = Config.Bind("General", "Scale", 1f,
                "UI scale factor (1 = unchanged). $JANUS_UI_SCALE overrides it.").Value;
            Factor = EnvFloat("JANUS_UI_SCALE", Factor, 0.1f, 10f);
            Groups = Environment.GetEnvironmentVariable("JANUS_UI_GROUPS") == "1";
            foreach (string n in (Environment.GetEnvironmentVariable("JANUS_UI_DISABLE") ?? "").Split(','))
                if (n.Trim().Length > 0) Disable.Add(n.Trim());
            foreach (string n in (Environment.GetEnvironmentVariable("JANUS_UI_IGNORE") ?? "").Split(','))
                if (n.Trim().Length > 0) Ignore.Add(n.Trim());
            if (Ignore.Count > 0)
                Log.LogInfo("not fitting around: " + string.Join(", ", new List<string>(Ignore).ToArray()));
            if (Disable.Count > 0)
                Log.LogInfo("switching off on UI canvases: " + string.Join(", ", new List<string>(Disable).ToArray()));
            // Our own hidden object for Update(): some games destroy
            // BepInEx's manager object.
            GameObject go = new GameObject("JanusUiScale");
            go.hideFlags = HideFlags.HideAndDontSave;
            DontDestroyOnLoad(go);
            string sizeFile = Environment.GetEnvironmentVariable("JANUS_SIZE_FILE");
            if (!string.IsNullOrEmpty(sizeFile) && Environment.GetEnvironmentVariable("JANUS_RESIZE") == "1")
                go.AddComponent<Resizer>().Init(sizeFile);
            if (Environment.GetEnvironmentVariable("JANUS_RUN_IN_BACKGROUND") == "1")
                go.AddComponent<KeepRunning>();
            if (Factor <= 1.0001f)
            {
                Log.LogInfo("UI scale 1: UI left alone");
                return;
            }
            Log.LogInfo("UI scale factor " + F(Factor) + (Groups ? ", per-group scaling (experimental)" : ""));
            go.AddComponent<Watcher>();

            new Harmony("janus.uiscale").PatchAll(typeof(Plugin).Assembly);
            // Scalers enabled before the patch went in (the first scene).
            foreach (CanvasScaler s in Resources.FindObjectsOfTypeAll<CanvasScaler>())
                if (s.isActiveAndEnabled)
                    Watcher.Register(s);
        }

        static float EnvFloat(string name, float def, float min, float max)
        {
            string s = Environment.GetEnvironmentVariable(name);
            float v;
            if (!string.IsNullOrEmpty(s) && float.TryParse(s, NumberStyles.Float,
                    CultureInfo.InvariantCulture, out v) && v >= min && v <= max)
                return v;
            return def;
        }

        internal static string F(float v)
        {
            return v.ToString("0.###", CultureInfo.InvariantCulture);
        }
    }

    // Game.unityRunInBackground: only one tile has the desktop's focus, and a
    // Unity game without "Run In Background" pauses whenever its window
    // isn't focused (no Update, no rendering, input ignored): every other
    // tile froze. Kept on, as a game may switch it off again.
    class KeepRunning : MonoBehaviour
    {
        float next;

        void Awake()
        {
            Plugin.Log.LogInfo("run in background: " + Application.runInBackground + " -> True");
            Application.runInBackground = true;
        }

        void Update()
        {
            if (Time.unscaledTime < next) return;
            next = Time.unscaledTime + 1f;
            if (!Application.runInBackground)
            {
                Plugin.Log.LogInfo("run in background switched off by the game: on again");
                Application.runInBackground = true;
            }
        }
    }

    // Keeps the game at the size Janus asks for in $JANUS_SIZE_FILE.
    class Resizer : MonoBehaviour
    {
        string path;
        float next;
        int wantW, wantH;          // last size read from the file
        int tries;                 // consecutive re-applies that didn't take

        public void Init(string file)
        {
            // Under Wine a Unix path is a path on drive Z:.
            path = file.StartsWith("/") && System.IO.Path.DirectorySeparatorChar == '\\'
                ? "Z:" + file.Replace('/', '\\') : file;
            Plugin.Log.LogInfo("resizing from " + path);
        }

        void Update()
        {
            if (Time.unscaledTime < next) return;
            next = Time.unscaledTime + 0.5f;
            int w, h;
            if (!Read(out w, out h)) return;
            if (w != wantW || h != wantH)
            {
                wantW = w; wantH = h; tries = 0;
            }
            // Windowed too: a game that applies its own saved display mode
            // (Lethal Company: full-screen window) makes its X window the size
            // of the whole gamescope screen, and gamescope scales that into the
            // tile. Checked all along, as the game may do that at any time.
            if (Screen.width == wantW && Screen.height == wantH
                && Screen.fullScreenMode == FullScreenMode.Windowed)
            {
                tries = 0;             // right: a later change gets 6 tries again
                return;
            }
            if (tries >= 6) return;    // it won't take it: stop asking
            tries++;
            Plugin.Log.LogInfo("screen " + Screen.width + "x" + Screen.height + " "
                               + Screen.fullScreenMode + " -> " + wantW + "x" + wantH
                               + (tries > 1 ? " (again)" : ""));
            Screen.SetResolution(wantW, wantH, FullScreenMode.Windowed);
        }

        bool Read(out int w, out int h)
        {
            w = h = 0;
            string s;
            try { s = System.IO.File.ReadAllText(path).Trim(); }
            catch (Exception) { return false; }
            string[] p = s.Split('x');
            return p.Length == 2 && int.TryParse(p[0], out w) && int.TryParse(p[1], out h)
                   && w > 0 && h > 0;
        }
    }

    [HarmonyPatch(typeof(CanvasScaler), "OnEnable")]
    static class CanvasScalerOnEnable
    {
        static void Postfix(CanvasScaler __instance)
        {
            try { Watcher.Register(__instance); }
            catch (Exception e) { Plugin.Log.LogError(e); }
        }
    }

    // How a text is laid out: lines, whether it spills out of its box, font size.
    struct TextLayout
    {
        public int Lines;
        public bool Spills;
        public float Size;
    }

    // A menu laid out as intended (see Watcher.DesignFactor): what already
    // overflows the screen there (by design), and how each text is laid out.
    class Baseline
    {
        // Elements already sticking out at baseline -> how far (fraction of
        // the half screen); they may not stick out further.
        public readonly Dictionary<Graphic, float> Overflow = new Dictionary<Graphic, float>();
        public readonly HashSet<Graphic> OnScreen = new HashSet<Graphic>();
        public readonly HashSet<Graphic> Visible = new HashSet<Graphic>();
        public readonly Dictionary<Graphic, TextLayout> Text = new Dictionary<Graphic, TextLayout>();
    }

    // One root canvas we manage.
    class Managed
    {
        public CanvasScaler Scaler;
        public Canvas Canvas;
        public string Name;
        public Vector2 OrigRef;
        public float OrigFactor;
        public float Current;                 // factor applied now
        public int Sig;                       // menu-state signature
        public float RefitAt = -1;            // re-fit after the menu settled
        public int OverflowStreak;            // consecutive checks with overflow
        public readonly Dictionary<int, float> Learned = new Dictionary<int, float>();
        public readonly HashSet<int> Reported = new HashSet<int>();
        // The game's own layout (factor 1) per menu state, from the last fit.
        public readonly Dictionary<int, Baseline> Baselines = new Dictionary<int, Baseline>();
        // Per-group scaling (Plugin.Groups): each touched group's original
        // localPosition/localScale, and the placement learned per menu state.
        public readonly Dictionary<RectTransform, Vector3[]> GroupOrig = new Dictionary<RectTransform, Vector3[]>();
        public readonly Dictionary<int, List<KeyValuePair<RectTransform, Vector3[]>>> GroupLearned
            = new Dictionary<int, List<KeyValuePair<RectTransform, Vector3[]>>>();
        // Groups whose position the game sets itself (every frame: Content
        // Warning's helmet HUD): ours is lost, so they're left alone.
        public readonly HashSet<RectTransform> GamePositioned = new HashSet<RectTransform>();

        // A group the game moved away from where we put it -> it owns the
        // position. Returns true when one was found (newly).
        public bool DetectGamePositioned()
        {
            List<KeyValuePair<RectTransform, Vector3[]>> l;
            if (!GroupLearned.TryGetValue(Sig, out l)) return false;
            bool found = false;
            foreach (KeyValuePair<RectTransform, Vector3[]> kv in l)
            {
                RectTransform t = kv.Key;
                if (t == null || GamePositioned.Contains(t)) continue;
                if ((t.localPosition - kv.Value[0]).sqrMagnitude > 0.25f)
                {
                    GamePositioned.Add(t);
                    Plugin.Log.LogInfo("group '" + t.name + "' is positioned by the game: "
                                       + "leaving it at the game's size");
                    found = true;
                }
            }
            return found;
        }

        public void ResetGroups()
        {
            foreach (KeyValuePair<RectTransform, Vector3[]> kv in GroupOrig)
                if (kv.Key != null)
                {
                    if (!GamePositioned.Contains(kv.Key)) kv.Key.localPosition = kv.Value[0];
                    kv.Key.localScale = kv.Value[1];
                }
        }

        public void ApplyGroups(int sig)
        {
            List<KeyValuePair<RectTransform, Vector3[]>> l;
            if (!GroupLearned.TryGetValue(sig, out l)) return;
            foreach (KeyValuePair<RectTransform, Vector3[]> kv in l)
                if (kv.Key != null)
                {
                    if (!GamePositioned.Contains(kv.Key)) kv.Key.localPosition = kv.Value[0];
                    kv.Key.localScale = kv.Value[1];
                }
        }

        static readonly System.Reflection.MethodInfo Handle = typeof(CanvasScaler).GetMethod(
            "Handle", System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic
                      | System.Reflection.BindingFlags.Public);

        public void Apply(float f)
        {
            Current = f;
            if (Scaler.uiScaleMode == CanvasScaler.ScaleMode.ScaleWithScreenSize)
                Scaler.referenceResolution = OrigRef / f;
            else
                Scaler.scaleFactor = OrigFactor * f;
        }

        // Apply and lay the canvas out right now (the scaler would only pick
        // the change up in its next Update), so it can be measured this frame.
        public void ApplyNow(float f)
        {
            Apply(f);
            if (Handle != null)
                Handle.Invoke(Scaler, null);
            // Twice: in one pass TextMeshPro regenerates before the layout
            // pass resizes its box, so its text bounds lag one step behind.
            Canvas.ForceUpdateCanvases();
            Canvas.ForceUpdateCanvases();
        }
    }

    class Watcher : MonoBehaviour
    {
        const float Interval = 0.2f;          // safety-net overflow check period (s)
        const int Persist = 3;                // checks overflow must last before it counts
        const float Settle = 0.6f;            // re-fit this long after a menu opens
        const int SearchSteps = 7;            // binary-search iterations (~0.4 % precision)
        // Never shrinks below the game's own size (or the factor, if < 1).
        static float MinFactor { get { return Mathf.Min(1f, Plugin.Factor); } }
        // Fit below 1 on menus whose own layout is squeezed (DesignFactor).
        const bool BelowOne = true;
        static readonly Dictionary<CanvasScaler, Managed> All = new Dictionary<CanvasScaler, Managed>();
        static readonly List<Graphic> Graphics = new List<Graphic>();
        static readonly Vector3[] Corners = new Vector3[4];
        float next;
        static int screenW, screenH;
        // JANUS_UI_DUMP=1: dump once at 40 s; =N: at N s (e.g. once in game).
        float dumpAt = DumpTime(Environment.GetEnvironmentVariable("JANUS_UI_DUMP"));

        static float DumpTime(string v)
        {
            float t;
            if (v == "1") return 40f;
            return float.TryParse(v, System.Globalization.NumberStyles.Float,
                                  System.Globalization.CultureInfo.InvariantCulture, out t) ? t : -1f;
        }

        internal static void Register(CanvasScaler s)
        {
            if (s == null || !s.gameObject.scene.IsValid() || All.ContainsKey(s))
                return;                          // prefab asset, or known already
            Canvas c = s.GetComponent<Canvas>();
            if (c == null || !c.isRootCanvas || c.renderMode == RenderMode.WorldSpace)
                return;
            // Rendered into a texture smaller than the screen: an in-world
            // screen (Lethal Company's ship radar monitor), not screen UI. A
            // screen-sized texture is a full-screen UI pass composited later
            // (Mycopunk's HUD: 'UI Camera' -> 1920x540 texture in a 1920x540
            // tile), so it is screen UI.
            if (c.renderMode == RenderMode.ScreenSpaceCamera && c.worldCamera != null
                    && c.worldCamera.targetTexture != null
                    && (c.worldCamera.targetTexture.width < Screen.width - 1
                        || c.worldCamera.targetTexture.height < Screen.height - 1))
                return;
            Managed m = new Managed
            {
                Scaler = s, Canvas = c, Name = PathOf(s.transform),
                OrigRef = s.referenceResolution, OrigFactor = s.scaleFactor,
            };
            All[s] = m;
            string desc = s.uiScaleMode == CanvasScaler.ScaleMode.ScaleWithScreenSize
                ? "reference " + m.OrigRef.x + "x" + m.OrigRef.y + ", " + s.screenMatchMode
                  + " " + Plugin.F(s.matchWidthOrHeight)
                : "scaleFactor " + Plugin.F(m.OrigFactor);
            m.Apply(Plugin.Factor);
            string render = c.renderMode.ToString();
            if (c.renderMode != RenderMode.ScreenSpaceOverlay && c.worldCamera != null)
            {
                Camera cam = c.worldCamera;
                render += " camera '" + cam.name + "' " + cam.pixelWidth + "x" + cam.pixelHeight
                          + (cam.targetTexture != null ? " -> texture " + cam.targetTexture.width
                             + "x" + cam.targetTexture.height : " -> screen");
            }
            Plugin.Log.LogInfo("canvas '" + m.Name + "' (" + s.uiScaleMode + ", " + desc
                               + ", " + render + ") -> factor " + Plugin.F(Plugin.Factor));
        }

        // LateUpdate runs after the game's Update/LateUpdate logic opened or
        // closed its panels and before the canvases render, so a menu that
        // just opened is fitted before its first frame is shown.
        void LateUpdate()
        {
            if (dumpAt > 0 && Time.unscaledTime > dumpAt) { dumpAt = -1; Dump(); }
            if (Screen.width != screenW || Screen.height != screenH)
            {
                // Resolution/window change (some games start small): every
                // fitted size is stale, re-fit all menus.
                if (screenW != 0)
                    Plugin.Log.LogInfo("screen " + screenW + "x" + screenH + " -> "
                                       + Screen.width + "x" + Screen.height + ": re-fitting");
                screenW = Screen.width; screenH = Screen.height;
                foreach (Managed m in All.Values)
                {
                    m.ResetGroups();
                    m.Learned.Clear(); m.Baselines.Clear(); m.GroupLearned.Clear(); m.Sig = 0;
                }
            }
            bool tick = Time.unscaledTime >= next;
            if (tick)
                next = Time.unscaledTime + Interval;
            List<CanvasScaler> dead = null;
            foreach (KeyValuePair<CanvasScaler, Managed> kv in All)
            {
                if (kv.Key == null) { (dead ?? (dead = new List<CanvasScaler>())).Add(kv.Key); continue; }
                if (!kv.Key.isActiveAndEnabled) continue;
                try
                {
                    Managed m = kv.Value;
                    int sig = Signature(m.Canvas.transform);
                    if (sig != m.Sig)
                        MenuChanged(m, sig);
                    else if (m.RefitAt > 0 && Time.unscaledTime >= m.RefitAt)
                    {
                        // Opening animations may have pushed things out (or
                        // not yet in): fit again on the settled layout.
                        m.RefitAt = -1;
                        Fit(m, "settled");
                    }
                    else if (tick && Plugin.Groups && m.DetectGamePositioned())
                        Fit(m, "group moved by the game");
                    else if (tick)
                        Check(m);
                }
                catch (Exception e) { Plugin.Log.LogError(e); }
            }
            if (dead != null)
                foreach (CanvasScaler s in dead) All.Remove(s);
        }

        static void MenuChanged(Managed m, int sig)
        {
            m.Sig = sig;
            m.OverflowStreak = 0;
            float f;
            if (m.Learned.TryGetValue(sig, out f))
            {
                m.ResetGroups();                 // seen before: its fitted size at once
                m.ApplyNow(f);
                m.ApplyGroups(sig);
            }
            else
                Fit(m, "opened");
            m.RefitAt = Time.unscaledTime + Settle;
        }

        // The factor at which a ScaleWithScreenSize canvas gets its whole
        // designed frame (reference resolution): in a tile narrower than the
        // design aspect, < 1 — the game's own layout there (factor 1) is
        // already squeezed (Content Warning's settings in a 960x1080 tile).
        // 1 for other scale modes and for screens at least as wide as the design.
        static float DesignFactor(Managed m)
        {
            CanvasScaler s = m.Scaler;
            if (s.uiScaleMode != CanvasScaler.ScaleMode.ScaleWithScreenSize
                    || m.OrigRef.x <= 0 || m.OrigRef.y <= 0)
                return 1f;
            float rw = Screen.width / m.OrigRef.x, rh = Screen.height / m.OrigRef.y;
            float s0;                            // the game's own canvas scale
            switch (s.screenMatchMode)
            {
                case CanvasScaler.ScreenMatchMode.Expand: s0 = Mathf.Min(rw, rh); break;
                case CanvasScaler.ScreenMatchMode.Shrink: s0 = Mathf.Max(rw, rh); break;
                default:
                    s0 = Mathf.Pow(2f, Mathf.Lerp(Mathf.Log(rw, 2f), Mathf.Log(rh, 2f),
                                                  s.matchWidthOrHeight));
                    break;
            }
            return Mathf.Clamp(Mathf.Min(rw, rh) / s0, 0.25f, 1f);
        }

        // Largest factor up to the setting whose layout is no worse than the
        // menu's intended one (laid out at DesignFactor): no text wraps onto
        // more lines, spills out of its box or is auto-sized smaller, and
        // nothing newly sticks out of the screen. Never below DesignFactor; it
        // only goes below 1 where the game's own layout is already squeezed.
        // Binary search with a forced layout per step, all within this frame,
        // so nothing visibly resizes in steps.
        static readonly List<MonoBehaviour> Behaviours = new List<MonoBehaviour>();

        // Plugin.Disable: switch those game scripts off on this canvas (they
        // may be added later, so this runs on every fit).
        static void DisableScripts(Managed m)
        {
            if (Plugin.Disable.Count == 0) return;
            m.Canvas.GetComponentsInChildren(true, Behaviours);
            foreach (MonoBehaviour b in Behaviours)
                if (b != null && b.enabled && Plugin.Disable.Contains(b.GetType().Name))
                {
                    b.enabled = false;
                    Plugin.Log.LogInfo("switched off " + b.GetType().Name + " on '" + PathOf(b.transform) + "'");
                }
        }

        static void Fit(Managed m, string why)
        {
            DisableScripts(m);
            float before = m.Current;
            m.ResetGroups();
            m.GroupLearned.Remove(m.Sig);
            // Off screen is judged against the game's own layout (factor 1):
            // what it already crops in this tile (a logo too wide for a tall
            // tile) is its choice. Text squeeze is judged against the intended
            // layout (DesignFactor), so only squeezed text takes it below 1.
            float lo = BelowOne ? Mathf.Min(MinFactor, DesignFactor(m)) : MinFactor;
            Baseline b = new Baseline();
            m.ApplyNow(MinFactor);
            Measure(m, null, null, b);
            if (lo < MinFactor)
            {
                Baseline design = new Baseline();
                m.ApplyNow(lo);
                Measure(m, null, null, design);
                b.Text.Clear();
                foreach (KeyValuePair<Graphic, TextLayout> kv in design.Text)
                    b.Text[kv.Key] = kv.Value;
            }
            m.Baselines[m.Sig] = b;
            float hi = Plugin.Factor;
            m.ApplyNow(hi);
            if (!Measure(m, b, null, null))
            {
                for (int i = 0; i < SearchSteps; i++)
                {
                    float mid = (lo + hi) * 0.5f;
                    m.ApplyNow(mid);
                    if (Measure(m, b, null, null)) lo = mid; else hi = mid;
                }
                hi = lo;
                m.ApplyNow(hi);
            }
            m.Learned[m.Sig] = hi;
            m.OverflowStreak = 0;
            if (Mathf.Abs(hi - before) > 0.001f || why == "opened")
            {
                StringBuilder r = new StringBuilder();
                if (hi < Plugin.Factor - 0.001f)
                {
                    // What stopped it going bigger (one step up).
                    m.ApplyNow(Mathf.Min(Plugin.Factor, hi * 1.05f));
                    Measure(m, b, r, null);
                    m.ApplyNow(hi);
                }
                Plugin.Log.LogInfo("fit '" + m.Name + "' (" + why + "): factor " + Plugin.F(hi)
                                   + (r.Length > 0 ? "; bigger would break:" + Truncate(r, 6) : ""));
            }
            if (Plugin.Groups && hi < Plugin.Factor * 0.97f)
                FitGroups(m, hi, why);
        }

        // --- per-group scaling (experimental) ------------------------------

        // The element groups of a canvas: below the chain of single-child
        // containers (Canvas -> HelmetUIToggler -> Pivot -> Others), the
        // first level with several active children that draw something.
        static List<RectTransform> GroupsOf(Managed m, Baseline gb)
        {
            Transform t = m.Canvas.transform;
            for (int depth = 0; depth < 8; depth++)
            {
                List<RectTransform> kids = new List<RectTransform>();
                for (int i = 0; i < t.childCount; i++)
                {
                    RectTransform c = t.GetChild(i) as RectTransform;
                    if (c != null && c.gameObject.activeInHierarchy && GroupRect(m, c, gb).width > 1)
                        kids.Add(c);
                }
                if (kids.Count >= 2)
                {
                    // Positions under a layout group are driven by it.
                    if (t.GetComponent<LayoutGroup>() != null) return null;
                    return kids;
                }
                if (kids.Count == 0) return null;
                t = kids[0];
            }
            return null;
        }

        // Screen bounds of a group's visible graphics (those in baseline gb).
        static readonly List<Graphic> GroupGraphics = new List<Graphic>();

        static Rect GroupRect(Managed m, Transform t, Baseline gb)
        {
            float x0 = float.MaxValue, y0 = float.MaxValue, x1 = float.MinValue, y1 = float.MinValue;
            t.GetComponentsInChildren(false, GroupGraphics);
            foreach (Graphic g in GroupGraphics)
            {
                if (!gb.Visible.Contains(g)) continue;
                Rect r = ClipToMasks(m.Canvas, g.transform, GraphicRect(m.Canvas, g, Corners), Corners);
                if (r.width <= 1 || r.height <= 1) continue;
                x0 = Mathf.Min(x0, r.xMin); y0 = Mathf.Min(y0, r.yMin);
                x1 = Mathf.Max(x1, r.xMax); y1 = Mathf.Max(y1, r.yMax);
            }
            return x0 > x1 ? Rect.zero : Rect.MinMaxRect(x0, y0, x1, y1);
        }

        static float Overlap(Rect a, Rect b)
        {
            float w = Mathf.Min(a.xMax, b.xMax) - Mathf.Max(a.xMin, b.xMin);
            float h = Mathf.Min(a.yMax, b.yMax) - Mathf.Max(a.yMin, b.yMin);
            return w > 0 && h > 0 ? w * h : 0f;
        }

        // The canvas stays at its fitted factor *fc*; each group is scaled up
        // to Factor / fc about the point of its box nearest its screen edge
        // (a bottom-left group grows up and right), as far as it stays on
        // screen (no worse than at fc) and doesn't overlap the other groups
        // more than it did. Only transforms change, no re-layout needed.
        static void FitGroups(Managed m, float fc, string why)
        {
            Baseline gb = new Baseline();
            Measure(m, null, null, gb);          // the layout at fc, groups untouched
            List<RectTransform> top = GroupsOf(m, gb);
            if (top == null)
                return;
            float sw = Screen.width, sh = Screen.height, maxG = Plugin.Factor / fc;
            // A group spanning most of the screen is a container (all of a
            // HUD's blocks: Content Warning's 'Others') or a full-screen frame
            // ('Edge'): split it into its children instead, never scale it.
            List<RectTransform> groups = new List<RectTransform>();
            System.Action<RectTransform, int> add = null;
            add = (t, depth) =>
            {
                Rect r = GroupRect(m, t, gb);
                if (r.width <= 1)
                    return;
                bool spans = (r.width >= sw * 0.6f && r.height >= sh * 0.6f)
                             || r.width >= sw * 0.95f || r.height >= sh * 0.95f;
                if (!spans) { groups.Add(t); return; }
                if (depth >= 3 || t.GetComponent<LayoutGroup>() != null)
                    return;
                for (int i = 0; i < t.childCount; i++)
                {
                    RectTransform c = t.GetChild(i) as RectTransform;
                    if (c != null && c.gameObject.activeInHierarchy)
                        add(c, depth + 1);
                }
            };
            foreach (RectTransform t in top)
                add(t, 0);
            if (groups.Count == 0)
                return;
            Camera cam = m.Canvas.renderMode == RenderMode.ScreenSpaceOverlay ? null : m.Canvas.worldCamera;
            Dictionary<RectTransform, Rect> rects = new Dictionary<RectTransform, Rect>();
            foreach (RectTransform t in groups)
                rects[t] = GroupRect(m, t, gb);
            List<KeyValuePair<RectTransform, Vector3[]>> placed = new List<KeyValuePair<RectTransform, Vector3[]>>();
            StringBuilder log = new StringBuilder();
            foreach (RectTransform t in groups)
            {
                Rect r0 = rects[t];
                Vector2 anchor = new Vector2(
                    r0.center.x < sw / 3 ? r0.xMin : r0.center.x > sw * 2 / 3 ? r0.xMax : r0.center.x,
                    r0.center.y < sh / 3 ? r0.yMin : r0.center.y > sh * 2 / 3 ? r0.yMax : r0.center.y);
                // On screen: a block the game already lets stick out (HUDs laid
                // out for 16:9 in a tall tile) grows from the screen edge.
                anchor = new Vector2(Mathf.Clamp(anchor.x, 0, sw), Mathf.Clamp(anchor.y, 0, sh));
                Vector2 lp;
                RectTransform parent = t.parent as RectTransform;
                if (parent == null || !RectTransformUtility.ScreenPointToLocalPointInRectangle(
                        parent, anchor, cam, out lp))
                    continue;
                if (m.GamePositioned.Contains(t))
                {
                    log.Append("\n    '" + t.name + "' x1 (positioned by the game)");
                    continue;
                }
                if (!m.GroupOrig.ContainsKey(t))
                    m.GroupOrig[t] = new[] { t.localPosition, t.localScale };
                Vector3 pos = t.localPosition, scale = t.localScale;
                Dictionary<RectTransform, float> baseOverlap = new Dictionary<RectTransform, float>();
                foreach (KeyValuePair<RectTransform, Rect> o in rects)
                    if (o.Key != t) baseOverlap[o.Key] = Overlap(r0, o.Value);
                System.Func<float, bool> ok = g =>
                {
                    // Scale about the anchor: the pivot point moves away from it.
                    t.localScale = scale * g;
                    t.localPosition = new Vector3(lp.x + (pos.x - lp.x) * g, lp.y + (pos.y - lp.y) * g, pos.z);
                    Rect r = GroupRect(m, t, gb);
                    // Nudge it fully onto the screen (2 px margin) — also
                    // repairs what the game itself leaves cut off.
                    if (r.width > sw - 4 || r.height > sh - 4) return false;
                    float dx = r.xMin < 2 ? 2 - r.xMin : r.xMax > sw - 2 ? sw - 2 - r.xMax : 0;
                    float dy = r.yMin < 2 ? 2 - r.yMin : r.yMax > sh - 2 ? sh - 2 - r.yMax : 0;
                    Vector2 a, z;
                    if ((dx != 0 || dy != 0)
                            && RectTransformUtility.ScreenPointToLocalPointInRectangle(parent, r.center, cam, out a)
                            && RectTransformUtility.ScreenPointToLocalPointInRectangle(
                                   parent, r.center + new Vector2(dx, dy), cam, out z))
                    {
                        t.localPosition += new Vector3(z.x - a.x, z.y - a.y, 0);
                        r = GroupRect(m, t, gb);
                    }
                    if (!Measure(m, gb, null, null, within: t)) return false;
                    foreach (KeyValuePair<RectTransform, Rect> o in rects)
                        if (o.Key != t && Overlap(r, o.Value) > baseOverlap[o.Key] + 4f) return false;
                    return true;
                };
                float lo = 1f, hi = maxG;
                if (!ok(hi))
                {
                    for (int i = 0; i < SearchSteps; i++)
                    {
                        float mid = (lo + hi) * 0.5f;
                        if (ok(mid)) lo = mid; else hi = mid;
                    }
                    hi = lo;
                    ok(hi);
                }
                rects[t] = GroupRect(m, t, gb);
                placed.Add(new KeyValuePair<RectTransform, Vector3[]>(t, new[] { t.localPosition, t.localScale }));
                Rect fr = rects[t];
                log.Append("\n    '" + t.name + "' x" + Plugin.F(hi) + ""
                           + " at " + Mathf.RoundToInt(fr.xMin) + "," + Mathf.RoundToInt(fr.yMin) + " "
                           + Mathf.RoundToInt(fr.width) + "x" + Mathf.RoundToInt(fr.height)
                           + " (was " + Mathf.RoundToInt(r0.xMin) + "," + Mathf.RoundToInt(r0.yMin) + " "
                           + Mathf.RoundToInt(r0.width) + "x" + Mathf.RoundToInt(r0.height) + ")");
            }
            m.GroupLearned[m.Sig] = placed;
            if (why != "overflow")
                Plugin.Log.LogInfo("groups '" + m.Name + "' (" + why + ", canvas " + Plugin.F(fc) + "):" + log);
        }

        static string Truncate(StringBuilder r, int lines)
        {
            string[] parts = r.ToString().Split('\n');
            if (parts.Length <= lines + 1) return r.ToString();
            return string.Join("\n", parts, 0, lines + 1) + "\n    (+" + (parts.Length - lines - 1) + " more)";
        }

        // Safety net: overflow that appears later (content filled in, a
        // panel grew): logged, and re-fitted. Only overflow — squeezing comes
        // from the factor itself, which Fit already accounted for.
        static void Check(Managed m)
        {
            Baseline b;
            m.Baselines.TryGetValue(m.Sig, out b);
            bool reportNew = !m.Reported.Contains(m.Sig);
            StringBuilder report = reportNew ? new StringBuilder() : null;
            if (Measure(m, b, report, null, overflowOnly: true))
            {
                m.OverflowStreak = 0;
                return;
            }
            if (++m.OverflowStreak < Persist)
                return;                          // maybe an animation passing by
            if (reportNew)
            {
                m.Reported.Add(m.Sig);
                Plugin.Log.LogInfo("overflow on '" + m.Name + "' at factor " + Plugin.F(m.Current)
                                   + " (screen " + Screen.width + "x" + Screen.height + "):" + report);
            }
            if (m.Current > MinFactor + 0.001f)
                Fit(m, "overflow");
            m.OverflowStreak = 0;
        }

        // JANUS_UI_DUMP=1: every canvas and its visible graphics, once.
        static void Dump()
        {
            Vector3[] corners = new Vector3[4];
            foreach (Canvas c in Resources.FindObjectsOfTypeAll<Canvas>())
            {
                if (!c.gameObject.scene.IsValid() || !c.isActiveAndEnabled || !c.isRootCanvas) continue;
                CanvasScaler cs = c.GetComponent<CanvasScaler>();
                string cam = "";
                if (c.renderMode != RenderMode.ScreenSpaceOverlay && c.worldCamera != null)
                {
                    Camera wc = c.worldCamera;
                    cam = " camera '" + wc.name + "' " + wc.pixelWidth + "x" + wc.pixelHeight
                          + (wc.targetTexture != null ? " -> texture " + wc.targetTexture.width
                             + "x" + wc.targetTexture.height : " -> screen");
                }
                Plugin.Log.LogInfo("DUMP canvas '" + PathOf(c.transform) + "' " + c.renderMode
                                   + " sort " + c.sortingOrder + cam
                                   + (cs == null ? " NO SCALER" : " " + cs.uiScaleMode
                                      + " ref " + cs.referenceResolution.x + "x" + cs.referenceResolution.y
                                      + " match " + Plugin.F(cs.matchWidthOrHeight)));
                VisibleCache.Clear();
                c.GetComponentsInChildren(false, Graphics);
                foreach (Graphic g in Graphics)
                {
                    if (!g.enabled || g.color.a <= 0.01f || !Visible(g.transform, c.transform)) continue;
                    Rect r = GraphicRect(c, g, corners);
                    if (r.width < 200 && r.height < 200) continue;   // big ones only
                    Plugin.Log.LogInfo("DUMP   " + g.GetType().Name + " '" + PathOf(g.transform) + "' "
                                       + Mathf.RoundToInt(r.xMin) + "," + Mathf.RoundToInt(r.yMin) + " "
                                       + Mathf.RoundToInt(r.width) + "x" + Mathf.RoundToInt(r.height));
                }
            }
        }

        // Is the canvas's layout acceptable against baseline *b*? Violations:
        // visible content partly off screen that wasn't in the baseline, and
        // (unless *overflowOnly*) a text squeezed compared to the baseline:
        // more lines, newly spilling out of its box, or auto-sized smaller.
        // b == null: any overflow counts. *record*: fill it as the baseline
        // instead of checking. Violations are listed into *report* if given.
        static bool Measure(Managed m, Baseline b, StringBuilder report, Baseline record,
                            bool overflowOnly = false, Transform within = null)
        {
            float sw = Screen.width, sh = Screen.height;
            bool ok = true;
            VisibleCache.Clear();
            m.Canvas.GetComponentsInChildren(false, Graphics);
            foreach (Graphic g in Graphics)
            {
                if (!g.enabled || g.canvasRenderer.cull || g.color.a <= 0.01f
                        || !Visible(g.transform, m.Canvas.transform))
                    continue;
                if (within != null && !g.transform.IsChildOf(within))
                    continue;
                if (Ignored(g.transform, m.Canvas.transform))
                    continue;
                if (record != null)
                    record.Visible.Add(g);
                string why = null;
                TextLayout tl, bt;
                if ((record != null || (!overflowOnly && b != null)) && Layout(g, out tl))
                {
                    if (record != null)
                        record.Text[g] = tl;
                    else if (b.Text.TryGetValue(g, out bt))
                    {
                        if (tl.Lines > bt.Lines)
                            why = "text wraps onto " + tl.Lines + " lines (was " + bt.Lines + ")";
                        else if (tl.Spills && !bt.Spills)
                            why = "text spills out of its box";
                        else if (tl.Size < bt.Size * 0.95f)
                            why = "text auto-sized down";
                    }
                }
                Rect r = ClipToMasks(m.Canvas, g.transform, GraphicRect(m.Canvas, g, Corners), Corners);
                bool offScreen = r.xMax <= 0 || r.xMin >= sw || r.yMax <= 0 || r.yMin >= sh;
                if (record != null && r.width > 1 && r.height > 1 && !offScreen)
                    record.OnScreen.Add(g);
                // Fully off screen: parked there by the game (hidden panels)
                // unless it was on screen in the baseline — then the scaling
                // pushed it off (Content Warning's helmet HUD at the edges).
                if (why == null && offScreen && b != null && b.OnScreen.Contains(g))
                    why = "pushed off screen";
                else if (why == null && r.width > 1 && r.height > 1 && !offScreen)
                {
                    // An axis the element spans edge to edge (backgrounds,
                    // bars) overflows by design.
                    bool spanX = r.xMin <= 1 && r.xMax >= sw - 1;
                    bool spanY = r.yMin <= 1 && r.yMax >= sh - 1;
                    float overX = spanX ? 0f : Mathf.Max(-r.xMin, r.xMax - sw);
                    float overY = spanY ? 0f : Mathf.Max(-r.yMin, r.yMax - sh);
                    float over = Mathf.Max(overX / (sw * 0.5f), overY / (sh * 0.5f));
                    // > 3 px: corner pieces that hug the edge (Lethal
                    // Company's HUD sits ~1 px past the top) aren't overflow.
                    if (Mathf.Max(overX, overY) > 3f)
                    {
                        float was;
                        if (record != null)
                            record.Overflow[g] = over;
                        else if (b == null || !b.Overflow.TryGetValue(g, out was))
                            why = "off screen";
                        // Decorations designed to hang off screen (ropes,
                        // frames) may hang off a bit more; text may not.
                        else if (over > was + 0.01f && Layout(g, out tl))
                            why = "text further off screen than at the game's own size";
                    }
                }
                if (why == null)
                    continue;
                ok = false;
                if (report == null)
                    return false;
                Rect box = ScreenRect(m.Canvas, g.rectTransform, Corners);
                report.Append("\n    '" + PathOf(g.transform) + "' " + Mathf.RoundToInt(box.xMin)
                              + "," + Mathf.RoundToInt(box.yMin) + " " + Mathf.RoundToInt(box.width)
                              + "x" + Mathf.RoundToInt(box.height) + ": " + why);
            }
            return ok;
        }

        // Text layout of a uGUI Text or a TextMeshPro text (by reflection, so
        // the plugin loads without TextMeshPro). False for non-text graphics
        // and empty texts.
        static readonly Dictionary<Type, System.Reflection.PropertyInfo[]> TmpProps
            = new Dictionary<Type, System.Reflection.PropertyInfo[]>();

        static bool Layout(Graphic g, out TextLayout tl)
        {
            tl = new TextLayout();
            Text t = g as Text;
            if (t != null)
            {
                if (string.IsNullOrEmpty(t.text)) return false;
                TextGenerator gen = t.cachedTextGenerator;
                tl.Lines = gen.lineCount;
                tl.Size = t.resizeTextForBestFit ? gen.fontSizeUsedForBestFit : t.fontSize;
                Rect rr = t.rectTransform.rect;
                tl.Spills = t.preferredHeight > rr.height + 1 && t.verticalOverflow == VerticalWrapMode.Overflow;
                return true;
            }
            System.Reflection.PropertyInfo[] p;
            Type ty = g.GetType();
            if (!TmpProps.TryGetValue(ty, out p))
            {
                p = null;
                for (Type bt = ty; bt != null && bt != typeof(Graphic); bt = bt.BaseType)
                    if (bt.FullName == "TMPro.TMP_Text")
                        p = new[] { bt.GetProperty("textInfo"), bt.GetProperty("textBounds"),
                                    bt.GetProperty("fontSize"), bt.GetProperty("text") };
                TmpProps[ty] = p;
            }
            if (p == null || p[0] == null) return false;
            string text = p[3].GetValue(g, null) as string;
            if (string.IsNullOrEmpty(text)) return false;
            object info = p[0].GetValue(g, null);
            if (info == null) return false;
            System.Reflection.FieldInfo lc = info.GetType().GetField("lineCount");
            tl.Lines = lc != null ? (int)lc.GetValue(info) : 0;
            tl.Size = (float)p[2].GetValue(g, null);
            Bounds bb = (Bounds)p[1].GetValue(g, null);
            Rect box = g.rectTransform.rect;
            tl.Spills = bb.size.x > box.width + 2f || bb.size.y > box.height + 2f;
            return true;
        }

        // Hidden panels stay laid out on screen: games hide them by disabling
        // a nested Canvas or fading a CanvasGroup to 0. Cached per check.
        static readonly Dictionary<Transform, bool> VisibleCache = new Dictionary<Transform, bool>();
        static readonly List<CanvasGroup> Groups = new List<CanvasGroup>();

        static bool Visible(Transform t, Transform root)
        {
            bool v;
            if (VisibleCache.TryGetValue(t, out v))
                return v;
            v = true;
            Canvas c = t.GetComponent<Canvas>();
            if (c != null && !c.enabled)
                v = false;
            bool stop = false;
            if (v)
            {
                t.GetComponents(Groups);
                foreach (CanvasGroup g in Groups)
                {
                    if (!g.enabled) continue;
                    if (g.alpha <= 0.01f) v = false;
                    if (g.ignoreParentGroups) stop = true;
                }
            }
            if (v && t != root && t.parent != null)
                v = stop ? VisibleCanvasOnly(t.parent, root) : Visible(t.parent, root);
            VisibleCache[t] = v;
            return v;
        }

        // Above a CanvasGroup with ignoreParentGroups only canvases count.
        static bool VisibleCanvasOnly(Transform t, Transform root)
        {
            for (; t != null; t = t.parent)
            {
                Canvas c = t.GetComponent<Canvas>();
                if (c != null && !c.enabled) return false;
                if (t == root) break;
            }
            return true;
        }

        // TextMeshPro: the glyphs, not the layout box (a title's box can be
        // far wider than its text, or the text can spill out of it). Found by
        // reflection so the plugin loads in games without TextMeshPro.
        static readonly Dictionary<Type, System.Reflection.PropertyInfo> TextBounds
            = new Dictionary<Type, System.Reflection.PropertyInfo>();

        static Rect GraphicRect(Canvas c, Graphic g, Vector3[] corners)
        {
            Type t = g.GetType();
            System.Reflection.PropertyInfo p;
            if (!TextBounds.TryGetValue(t, out p))
            {
                p = null;
                for (Type b = t; b != null && b != typeof(Graphic); b = b.BaseType)
                    if (b.FullName == "TMPro.TMP_Text")
                        p = b.GetProperty("textBounds");
                TextBounds[t] = p;
            }
            if (p == null)
                return ScreenRect(c, g.rectTransform, corners);
            Bounds bb = (Bounds)p.GetValue(g, null);
            if (bb.size.x <= 0 || bb.size.y <= 0)
                return Rect.zero;                // empty text
            Transform tr = g.transform;
            Camera cam = c.renderMode == RenderMode.ScreenSpaceOverlay ? null : c.worldCamera;
            corners[0] = tr.TransformPoint(new Vector3(bb.min.x, bb.min.y, 0));
            corners[1] = tr.TransformPoint(new Vector3(bb.min.x, bb.max.y, 0));
            corners[2] = tr.TransformPoint(new Vector3(bb.max.x, bb.max.y, 0));
            corners[3] = tr.TransformPoint(new Vector3(bb.max.x, bb.min.y, 0));
            return ProjectCorners(cam, corners);
        }

        static Rect ScreenRect(Canvas c, RectTransform t, Vector3[] corners)
        {
            t.GetWorldCorners(corners);
            Camera cam = c.renderMode == RenderMode.ScreenSpaceOverlay ? null : c.worldCamera;
            return ProjectCorners(cam, corners);
        }

        // Screen bounds of four world-space corners. All four: a rotated or
        // perspective-tilted element (Content Warning's helmet HUD) reaches
        // further than its bottom-left/top-right pair.
        static Rect ProjectCorners(Camera cam, Vector3[] corners)
        {
            float x0 = float.MaxValue, y0 = float.MaxValue, x1 = float.MinValue, y1 = float.MinValue;
            for (int i = 0; i < 4; i++)
            {
                Vector2 p = RectTransformUtility.WorldToScreenPoint(cam, corners[i]);
                x0 = Mathf.Min(x0, p.x); y0 = Mathf.Min(y0, p.y);
                x1 = Mathf.Max(x1, p.x); y1 = Mathf.Max(y1, p.y);
            }
            return Rect.MinMaxRect(x0, y0, x1, y1);
        }

        // Masks clip their content, and a scroll view's content may extend
        // past the screen by design: only the part inside every mask / scroll
        // viewport can overflow the screen.
        static Rect ClipToMasks(Canvas c, Transform t, Rect r, Vector3[] corners)
        {
            for (Transform p = t.parent; p != null && p != c.transform; p = p.parent)
            {
                RectTransform clip = null;
                if (p.GetComponent<RectMask2D>() != null || p.GetComponent<Mask>() != null)
                    clip = (RectTransform)p;
                else
                {
                    ScrollRect sr = p.GetComponent<ScrollRect>();
                    if (sr != null)
                        clip = sr.viewport != null ? sr.viewport : (RectTransform)p;
                }
                if (clip == null)
                    continue;
                Rect m = ScreenRect(c, clip, corners);
                float x0 = Mathf.Max(r.xMin, m.xMin), y0 = Mathf.Max(r.yMin, m.yMin);
                float x1 = Mathf.Min(r.xMax, m.xMax), y1 = Mathf.Min(r.yMax, m.yMax);
                if (x1 <= x0 || y1 <= y0) return Rect.zero;
                r = Rect.MinMaxRect(x0, y0, x1, y1);
            }
            return r;
        }

        // Which panels are open: the active state of the canvas's top three
        // levels of children.
        static int Signature(Transform root)
        {
            int h = 17;
            Walk(root, 0, ref h);
            return h;
        }

        static void Walk(Transform t, int depth, ref int h)
        {
            if (depth == 3) return;
            for (int i = 0; i < t.childCount; i++)
            {
                Transform c = t.GetChild(i);
                if (!c.gameObject.activeSelf) continue;
                h = h * 31 + c.name.GetHashCode() + i;
                Walk(c, depth + 1, ref h);
            }
        }

        // t or one of its parents (below *root*) is named in Plugin.Ignore.
        static bool Ignored(Transform t, Transform root)
        {
            if (Plugin.Ignore.Count == 0) return false;
            for (; t != null && t != root; t = t.parent)
                if (Plugin.Ignore.Contains(t.name)) return true;
            return false;
        }

        static string PathOf(Transform t)
        {
            string p = t.name;
            while (t.parent != null) { t = t.parent; p = t.name + "/" + p; }
            return t.gameObject.scene.name + ":" + p;
        }
    }
}

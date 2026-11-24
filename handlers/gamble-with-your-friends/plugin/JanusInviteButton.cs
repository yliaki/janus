// A big "INVITE FRIENDS" button above the main menu's "Host Game".
//
// The game's own invite button is a small "+" icon at the top right. This adds
// a copy of the Host Game button (so it looks like the game's menu) above it,
// larger, whose only action is LobbyManager.InviteFriend() (the same call as
// the "+" icon and the pad's View button).
//
// Everything is found at runtime by name / label text, so a game update can
// only make the button not appear: every step that doesn't find what it
// expects gives up and logs one "[Janus] invite button:" line, and any
// exception disables the feature for the rest of the session. The game itself
// is never touched beyond the one added object.
//
// JANUS_UI_TREE=1 logs the menu's hierarchy (components, rects, texts, the
// UnityEvents' persistent calls) once the Host Game button is found.
using System;
using System.Collections.Generic;
using System.Reflection;
using System.Text;
using UnityEngine;
using UnityEngine.Events;
using UnityEngine.EventSystems;
using UnityEngine.UI;

static class JanusInviteButton
{
    const string ObjectName = "JanusInviteFriends";
    const string HostLabel = "host game";
    const float SizeFactor = 1.8f;   // font and fixed sizes, x Host Game's

    // What may stay on the copy: Unity's UI building blocks, TextMesh Pro and
    // the game's looks/sound components. Anything else (hosting, lobby-owner
    // checks, localization that would rename it back, text animation, click
    // feedback players) is removed.
    static readonly HashSet<string> KeepNamespaces = new HashSet<string> {
        "UnityEngine", "UnityEngine.UI", "TMPro",
    };
    static readonly HashSet<string> KeepGameTypes = new HashSet<string> {
        "UIButtonFeedback", "SFXGenericUIButtonComponent", "UIImportance",
        "MoreMountains.Tools.MMOnPointer", "MoreMountains.Tools.ShaderController",
    };
    // Kept UnityEvents may only call these (hover highlight); others are emptied.
    static readonly HashSet<string> SafeEventTargets = new HashSet<string> { "UIButtonFeedback" };

    static bool disabled;
    static float nextTry;
    static GameObject made;
    static bool dumped;
    static readonly bool dumpTree = Environment.GetEnvironmentVariable("JANUS_UI_TREE") == "1";

    static void Log(string msg) { Debug.Log("[Janus] invite button: " + msg); }

    public static void Tick(Action invite)
    {
        if (disabled || made != null) return;   // destroyed with its scene -> null
        if (Time.unscaledTime < nextTry) return;
        nextTry = Time.unscaledTime + 0.5f;
        try { made = TryAdd(invite); }
        catch (Exception e)
        {
            disabled = true;
            Log("disabled after an error (game update?): " + e);
        }
    }

    static GameObject TryAdd(Action invite)
    {
        // Only where the game itself offers invites (the main menu).
        var inviteType = Type.GetType("InviteFriendsButton, Assembly-CSharp");
        if (inviteType == null) { disabled = true; Log("no InviteFriendsButton type, not added"); return null; }
        if (UnityEngine.Object.FindAnyObjectByType(inviteType) == null) return null;

        Button host = null;
        foreach (var b in UnityEngine.Object.FindObjectsByType<Button>(FindObjectsSortMode.None))
        {
            if (!b.isActiveAndEnabled || b.name == ObjectName) continue;
            var label = FindLabel(b.gameObject, HostLabel);
            if (label == null) continue;
            if (host != null) { Log("two Host Game buttons, not added"); disabled = true; return null; }
            host = b;
        }
        if (host == null) return null;
        var parent = host.transform.parent as RectTransform;
        if (parent == null || parent.GetComponent<LayoutGroup>() == null)
        {
            disabled = true; Log("Host Game isn't in a menu layout (game update?), not added"); return null;
        }
        if (dumpTree && !dumped) { dumped = true; Dump(parent.parent != null ? parent.parent : parent); }

        var existing = parent.Find(ObjectName);
        if (existing != null) return existing.gameObject;

        // Copy under an inactive holder, so none of the copy's scripts run
        // (Awake/OnEnable/Start) before it's cleaned.
        var holder = new GameObject("JanusInviteHolder");
        holder.SetActive(false);
        GameObject copy = null;
        try
        {
            copy = UnityEngine.Object.Instantiate(host.gameObject, holder.transform, false);
            copy.name = ObjectName;
            ResetLooks(host.gameObject, copy);
            if (!Clean(copy)) { UnityEngine.Object.Destroy(copy); return Fail(); }
            var button = copy.GetComponent<Button>();
            var label = FindLabel(copy, HostLabel);
            if (button == null || label == null) { UnityEngine.Object.Destroy(copy); return Fail(); }

            button.onClick = new Button.ButtonClickedEvent();
            button.onClick.AddListener(() =>
            {
                try { invite(); }
                catch (Exception e) { Log("invite failed: " + e); }
            });
            string old = GetText(label);
            SetText(label, old == old.ToUpperInvariant() ? "INVITE FRIENDS" : "Invite friends");
            ScaleFont(label);
            RaiseImportance(copy, host);
            foreach (var le in copy.GetComponentsInChildren<LayoutElement>(true)) ScaleLayout(le);

            copy.transform.SetParent(parent, false);
            copy.transform.SetSiblingIndex(host.transform.GetSiblingIndex());
            LinkNavigation(button, host);
            Log("added above Host Game");
            return copy;
        }
        catch
        {
            if (copy != null) UnityEngine.Object.Destroy(copy);
            throw;
        }
        finally
        {
            UnityEngine.Object.Destroy(holder);
        }
    }

    static GameObject Fail()
    {
        disabled = true;
        Log("the Host Game button looks different (game update?), not added");
        return null;
    }

    // Removes every component the copy may not keep (see KeepNamespaces) and
    // empties the UnityEvents of what stays unless they only call
    // SafeEventTargets inside the copy. False if something couldn't be removed.
    static bool Clean(GameObject copy)
    {
        // Several passes, last component first: one that another requires
        // (RequireComponent) can only go after it.
        for (int pass = 0; pass < 4; pass++)
        {
            var all = copy.GetComponentsInChildren<Component>(true);
            bool removed = false;
            for (int i = all.Length - 1; i >= 0; i--)
                if (all[i] != null && !Allowed(all[i]))
                {
                    UnityEngine.Object.DestroyImmediate(all[i]);
                    removed = true;
                }
            if (!removed) break;
        }
        foreach (var c in copy.GetComponentsInChildren<Component>(true))
        {
            if (c == null) continue;
            if (!Allowed(c)) { Log("couldn't remove " + c.GetType().FullName + " from the copy"); return false; }
            if (!(c is Button) && !(c is Transform)) ClearEvents(c, copy.transform);
        }
        return true;
    }

    static bool Allowed(Component c)
    {
        if (c is Transform) return true;
        if (c is EventTrigger) return false;
        var t = c.GetType();
        return KeepGameTypes.Contains(t.FullName) || (t.Namespace != null && KeepNamespaces.Contains(t.Namespace));
    }

    static void ClearEvents(Component c, Transform copy)
    {
        for (var t = c.GetType(); t != null && t != typeof(MonoBehaviour); t = t.BaseType)
            foreach (var f in t.GetFields(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly))
            {
                if (!typeof(UnityEventBase).IsAssignableFrom(f.FieldType)) continue;
                var ev = f.GetValue(c) as UnityEventBase;
                if (ev == null || SafeEvent(ev, copy)) continue;
                var ctor = f.FieldType.GetConstructor(Type.EmptyTypes);
                if (ctor == null) throw new Exception("can't empty " + t.FullName + "." + f.Name);
                f.SetValue(c, ctor.Invoke(null));
            }
    }

    static bool SafeEvent(UnityEventBase ev, Transform copy)
    {
        for (int i = 0; i < ev.GetPersistentEventCount(); i++)
        {
            var target = ev.GetPersistentTarget(i) as Component;
            if (target == null || !SafeEventTargets.Contains(target.GetType().FullName)
                || !target.transform.IsChildOf(copy))
                return false;
        }
        return true;
    }

    // UIButtonFeedback caches its button's colours and scale in Awake as the
    // "not selected" look and lerps back to them on deselect. Host Game is
    // usually highlighted when it's copied (selected by the pad, or hovered),
    // so the copy would cache the highlight (green background) as its normal
    // look and never lose it. Give the copy the colours/scale Host Game's
    // feedback cached instead. Optional: unknown fields leave the copy as is.
    static void ResetLooks(GameObject host, GameObject copy)
    {
        var t = Type.GetType("UIButtonFeedback, Assembly-CSharp");
        if (t == null) return;
        const BindingFlags F = BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.Public;
        int n = 0;
        foreach (var fb in host.GetComponentsInChildren(t, true))
        {
            var target = Follow(host.transform, fb.transform, copy.transform);
            if (target == null) continue;
            var scale = t.GetField("_baseScale", F);
            if (scale != null && scale.FieldType == typeof(Vector3))
                target.localScale = (Vector3)scale.GetValue(fb);
            n += ResetColors(fb, t.GetField("_childImages", F), t.GetField("_baseImageColors", F), host.transform, copy.transform);
            n += ResetColors(fb, t.GetField("_childTmpTexts", F), t.GetField("_baseTmpTextColors", F), host.transform, copy.transform);
            n += ResetColors(fb, t.GetField("_childLegacyTexts", F), t.GetField("_baseLegacyTextColors", F), host.transform, copy.transform);
        }
        if (n == 0) Log("couldn't read Host Game's normal colours (game update?), the copy may look highlighted");
    }

    static int ResetColors(Component fb, FieldInfo graphicsField, FieldInfo colorsField, Transform host, Transform copy)
    {
        if (graphicsField == null || colorsField == null) return 0;
        var graphics = graphicsField.GetValue(fb) as Array;
        var colors = colorsField.GetValue(fb) as Color[];
        if (graphics == null || colors == null || graphics.Length != colors.Length) return 0;
        int n = 0;
        for (int i = 0; i < graphics.Length; i++)
        {
            var g = graphics.GetValue(i) as Graphic;
            if (g == null) continue;
            var t = Follow(host, g.transform, copy);
            if (t == null) continue;
            foreach (var cg in t.GetComponents(g.GetType()))
            {
                ((Graphic)cg).color = colors[i];
                n++;
                break;
            }
        }
        return n;
    }

    // The object in `copy` at the same place as `node` is in `root` (the copy
    // was instantiated from root and only lost components, not children).
    static Transform Follow(Transform root, Transform node, Transform copy)
    {
        var path = new List<int>();
        for (var t = node; t != root; t = t.parent)
        {
            if (t == null) return null;
            path.Add(t.GetSiblingIndex());
        }
        var r = copy;
        for (int i = path.Count - 1; i >= 0; i--)
        {
            if (path[i] >= r.childCount) return null;
            r = r.GetChild(path[i]);
        }
        return r;
    }

    // Fixed sizes (the label's LayoutElement) grow with the font; the menu's
    // layout group and the button's own fitter then make room for it.
    static void ScaleLayout(LayoutElement le)
    {
        if (le.minWidth > 0) le.minWidth *= SizeFactor;
        if (le.minHeight > 0) le.minHeight *= SizeFactor;
        if (le.preferredWidth > 0) le.preferredWidth *= SizeFactor;
        if (le.preferredHeight > 0) le.preferredHeight *= SizeFactor;
    }

    // The game's InputModeManager selects the most important UIImportance
    // whenever the input mode changes, i.e. on a pad's first press after the
    // mouse moved. With Host Game's importance the copy loses that tie, and
    // the press that moved up to it is undone; one more makes it the button
    // the pad starts on. Optional: without the field the copy just ties.
    static void RaiseImportance(GameObject copy, Button host)
    {
        var t = Type.GetType("UIImportance, Assembly-CSharp");
        if (t == null) return;
        var f = t.GetField("importance", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
        var mine = copy.GetComponent(t);
        var theirs = host.GetComponent(t);
        if (f == null || f.FieldType != typeof(int) || mine == null || theirs == null) return;
        f.SetValue(mine, (int)f.GetValue(theirs) + 1);
    }

    static void LinkNavigation(Button copy, Button host)
    {
        var hn = host.navigation;
        if (hn.mode != Navigation.Mode.Explicit) return;  // automatic finds it itself
        var above = hn.selectOnUp;
        var cn = hn;
        cn.selectOnDown = host;
        cn.selectOnUp = above;
        copy.navigation = cn;
        hn.selectOnUp = copy;
        host.navigation = hn;
        if (above != null)
        {
            var an = above.navigation;
            if (an.mode == Navigation.Mode.Explicit && an.selectOnDown == host)
            {
                an.selectOnDown = copy;
                above.navigation = an;
            }
        }
    }

    // A text component under go (TMP or legacy, read by reflection) whose text
    // is `want` (case and rich-text tags ignored).
    static Component FindLabel(GameObject go, string want)
    {
        foreach (var c in go.GetComponentsInChildren<Component>(true))
        {
            var s = GetText(c);
            if (s != null && Normalize(s) == want) return c;
        }
        return null;
    }

    static PropertyInfo TextProp(Component c)
    {
        if (c == null) return null;
        var p = c.GetType().GetProperty("text", BindingFlags.Instance | BindingFlags.Public, null, typeof(string), Type.EmptyTypes, null);
        return p != null && p.CanRead && p.CanWrite && c is Behaviour ? p : null;
    }

    static string GetText(Component c)
    {
        var p = TextProp(c);
        return p == null ? null : p.GetValue(c, null) as string;
    }

    static void SetText(Component c, string s) { TextProp(c).SetValue(c, s, null); }

    static void ScaleFont(Component c)
    {
        var auto = c.GetType().GetProperty("enableAutoSizing");
        if (auto != null && auto.PropertyType == typeof(bool) && (bool)auto.GetValue(c, null))
        {
            var max = c.GetType().GetProperty("fontSizeMax");
            if (max != null && max.PropertyType == typeof(float))
                max.SetValue(c, (float)max.GetValue(c, null) * SizeFactor, null);
        }
        var p = c.GetType().GetProperty("fontSize");
        if (p == null || !p.CanWrite) return;
        if (p.PropertyType == typeof(float)) p.SetValue(c, (float)p.GetValue(c, null) * SizeFactor, null);
        else if (p.PropertyType == typeof(int)) p.SetValue(c, (int)((int)p.GetValue(c, null) * SizeFactor), null);
    }

    static string Normalize(string s)
    {
        var sb = new StringBuilder();
        bool tag = false;
        foreach (var ch in s)
        {
            if (ch == '<') tag = true;
            else if (ch == '>') tag = false;
            else if (!tag) sb.Append(ch);
        }
        return string.Join(" ", sb.ToString().Trim().ToLowerInvariant().Split(new[] { ' ', '\n', '\t', '\r' }, StringSplitOptions.RemoveEmptyEntries));
    }

    static void Dump(Transform root)
    {
        var sb = new StringBuilder("[Janus] UI tree:\n");
        DumpNode(root, 0, sb);
        Debug.Log(sb.ToString());
    }

    static void DumpNode(Transform t, int depth, StringBuilder sb)
    {
        var pad = new string(' ', depth * 2);
        var r = t as RectTransform;
        sb.Append(pad).Append(t.name).Append(t.gameObject.activeSelf ? "" : " (inactive)");
        if (r != null) sb.Append(" rect=").Append(r.rect.size).Append(" pos=").Append(r.anchoredPosition)
                         .Append(" anchors=").Append(r.anchorMin).Append(r.anchorMax).Append(" scale=").Append(r.localScale);
        sb.Append('\n');
        foreach (var c in t.GetComponents<Component>())
        {
            if (c == null || c is Transform) continue;
            sb.Append(pad).Append("  - ").Append(c.GetType().FullName);
            var s = GetText(c);
            if (s != null) sb.Append(" text=\"").Append(s.Replace("\n", "\\n")).Append('"');
            if (c is Selectable sel) sb.Append(" nav=").Append(sel.navigation.mode);
            sb.Append('\n');
            for (var ty = c.GetType(); ty != null && ty != typeof(MonoBehaviour); ty = ty.BaseType)
                foreach (var f in ty.GetFields(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly))
                {
                    if (!typeof(UnityEventBase).IsAssignableFrom(f.FieldType)) continue;
                    var ev = f.GetValue(c) as UnityEventBase;
                    if (ev == null) continue;
                    for (int i = 0; i < ev.GetPersistentEventCount(); i++)
                    {
                        var target = ev.GetPersistentTarget(i);
                        sb.Append(pad).Append("      ").Append(f.Name).Append(" -> ")
                          .Append(target == null ? "null" : target.GetType().Name + "(" + target.name + ")")
                          .Append('.').Append(ev.GetPersistentMethodName(i)).Append('\n');
                    }
                }
        }
        foreach (Transform child in t) DumpNode(child, depth + 1, sb);
    }
}

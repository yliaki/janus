// BepInEx 5 plugin for Janus: keep reading gamepads while the window is not
// focused. Unity's Input System defaults to ResetAndDisableNonBackgroundDevices,
// which disables every gamepad when the application loses focus; in a
// split-screen session only one gamescope tile has focus, so every other
// player's pad went dead. IgnoreFocus keeps them all live (keyboard and mouse
// still only reach the focused window).
// It also skips the "FEELING LUCKY?" start screen (OfflineSplashCoinFlip):
// YES there can quit the game, NO only continues, so it continues for the
// player (the splash's own LoadNextScene, as NO does minus its achievement).
// And the pad's View/Back button invites: the game's "Invite friends" button
// (a small "+" icon) can only be clicked with the mouse, so View calls the
// same LobbyManager.InviteFriend() (ActivateGameOverlayInviteDialog, which
// Goldberg turns into invites to the other instances).
// The main menu also gets a big "INVITE FRIENDS" button above Host Game
// (JanusInviteButton.cs) doing the same.
using System.Reflection;
using BepInEx;
using UnityEngine;
using UnityEngine.InputSystem;

[BepInPlugin("janus.padfocus", "Janus pads without focus", "1.0.0")]
public class JanusPadFocus : BaseUnityPlugin
{
    void Awake()
    {
        Application.runInBackground = true;
        Apply();
    }

    bool splashSkipped;

    // The game may load its own InputSettings asset later, replacing ours.
    // Each part on its own: a game update that breaks one leaves the others.
    void Update()
    {
        Guard("focus", ref focusBroken, Apply);
        if (!splashSkipped) Guard("splash", ref splashSkipped, SkipSplash);
        Guard("View invite", ref viewBroken, () =>
        {
            foreach (var pad in Gamepad.all)
                if (pad.selectButton.wasPressedThisFrame) { Invite("View pressed"); break; }
        });
        Guard("invite button", ref buttonBroken, () => JanusInviteButton.Tick(() => Invite("INVITE FRIENDS button")));
    }

    bool focusBroken, viewBroken, buttonBroken;

    static void Guard(string what, ref bool broken, System.Action f)
    {
        if (broken) return;
        try { f(); }
        catch (System.Exception e)
        {
            broken = true;
            Debug.LogWarning("[Janus] " + what + " disabled after an error (game update?): " + e);
        }
    }

    static void Invite(string why)
    {
        var t = System.Type.GetType("LobbyManager, Assembly-CSharp");
        var lobby = t == null ? null : Object.FindAnyObjectByType(t);
        var m = t == null ? null : t.GetMethod("InviteFriend", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
        if (lobby == null || m == null) { Debug.Log("[Janus] " + why + ": no LobbyManager to invite from"); return; }
        Debug.Log("[Janus] " + why + ": inviting the other players");
        m.Invoke(lobby, null);
    }

    void SkipSplash()
    {
        var t = System.Type.GetType("OfflineSplashCoinFlip, Assembly-CSharp");
        if (t == null) { splashSkipped = true; return; }
        var splash = Object.FindAnyObjectByType(t);
        if (splash == null)
        {
            // It's the first scene; long gone if not found by now.
            if (Time.realtimeSinceStartup > 60f) splashSkipped = true;
            return;
        }
        var m = t.GetMethod("LoadNextScene", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
        if (m == null) { splashSkipped = true; return; }
        m.Invoke(splash, null);
        splashSkipped = true;
        Debug.Log("[Janus] skipped the FEELING LUCKY? start screen");
    }

    static void Apply()
    {
        var s = InputSystem.settings;
        if (s == null || s.backgroundBehavior == InputSettings.BackgroundBehavior.IgnoreFocus)
            return;
        s.backgroundBehavior = InputSettings.BackgroundBehavior.IgnoreFocus;
        // Pads disabled by an earlier focus loss stay disabled otherwise.
        foreach (var d in InputSystem.devices)
            if (d is Gamepad && !d.enabled) InputSystem.EnableDevice(d);
        Debug.Log("[Janus] Input System: backgroundBehavior = IgnoreFocus");
    }
}

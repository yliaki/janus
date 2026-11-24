// BepInEx 5 plugin for Janus (Mycopunk): allow modded joiners.
// Mycopunk refuses joiners that load any BepInEx plugin ("Host does not
// allow modded clients to join") unless the host's "Allow modded joiners"
// lobby option is on, and PlayerOptions.InitializeConfig() resets that
// option to off on every start. In a Janus session every instance loads
// BepInEx (UI scale), so this turns the option on right after the game's
// config is initialised. The host can still switch it off in the lobby.
using BepInEx;
using HarmonyLib;

[BepInPlugin("janus.myco.modjoin", "Janus allow modded joiners", "1.0.0")]
public class JanusMycoModJoin : BaseUnityPlugin
{
    void Awake()
    {
        new Harmony("janus.myco.modjoin").PatchAll(typeof(JanusMycoModJoin));
        Allow();   // in case the config was initialised before we loaded
    }

    [HarmonyPatch(typeof(PlayerOptions), nameof(PlayerOptions.InitializeConfig))]
    [HarmonyPostfix]
    static void Allow()
    {
        try
        {
            PlayerOptions.SetConfig<float>("AllowModJoin", 1f);
            UnityEngine.Debug.Log("[Janus] AllowModJoin = 1 (modded joiners allowed)");
        }
        catch (System.Exception e)
        {
            UnityEngine.Debug.Log("[Janus] AllowModJoin not set yet: " + e.Message);
        }
    }
}

`payload/JanusMycoModJoin.dll` = built from `plugin/JanusMycoModJoin.cs` (Janus's own
code, `plugin/build.sh "<install>/Mycopunk_Data/Managed"`, needs mono-mcs; references
the game's assemblies, BepInEx and HarmonyX by name only). A BepInEx 5 plugin deployed
via `Game.bepinexPlugins`: turns on the host's "Allow modded joiners" option, which the
game resets at every start, so instances running Janus's BepInEx plugins (UI scale) can
join each other. Why: NOTES.md "Mycopunk".

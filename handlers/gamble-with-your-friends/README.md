`payload/JanusPadFocus.dll` = built from `plugin/JanusPadFocus.cs` (Janus's own
code, `plugin/build.sh "<install>/Gamble With Your Friends_Data/Managed"`, needs
mono-mcs; references the game's Unity assemblies and BepInEx by name only). A
BepInEx 5 plugin deployed via `Game.bepinexPlugins`: sets the Unity Input System's
`backgroundBehavior` to `IgnoreFocus` (pads keep working in unfocused tiles) and
skips the "FEELING LUCKY?" start screen; adds a big "INVITE FRIENDS" button above
Host Game and binds the pad's View button to the same invite (`JanusInviteButton.cs`;
found by label at runtime, gives up with a log line if a game update changes the menu). Why: NOTES.md "Gamble With Your Friends".

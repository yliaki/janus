Your Janus game handlers
========================

Janus needs a handler script for each game it launches. Put your own here:

    <game-name>.js                 one file, e.g. my-game.js
    <game-name>/handler.js         a folder, with files to deploy in <game-name>/payload/

The game is chosen by a line inside the script, not by the file name:

    Game.appid = 105600;

Handlers come from the handler hub (https://janus-split-screen.org; in
Janus: "Get handlers…", or Y in Big Picture) and are installed here. You
can change one here or write your own. The
<appid>.json files beside them are Janus's saved settings per game.

The scripting reference is SCRIPTING.md in the Janus program folder.
This file is rewritten whenever Janus is installed.

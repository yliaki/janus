# Steam artwork for the Janus shortcut

`scripts/install_steam_shortcut.sh` copies these into each Steam user's
`userdata/<id>/config/grid/`, named after the shortcut's appid. All optional:
a missing file is skipped (and artwork set by hand in Steam is kept).
PNG or JPG.

| File        | Steam slot                                   | Size                       |
|-------------|----------------------------------------------|----------------------------|
| `cover`     | Library portrait capsule (`<appid>p`)        | 600×900                    |
| `capsule`   | Wide capsule, recent games (`<appid>`)       | 920×430                    |
| `hero`      | Banner on the game page (`<appid>_hero`)     | 3840×1240 (or 1920×620)    |
| `logo`      | Logo over the hero (`<appid>_logo`)          | transparent PNG, ~1280×720 max |
| `icon`      | Icon in lists / taskbar (`<appid>_icon`, also the shortcut's `icon`) | square, 256×256 |

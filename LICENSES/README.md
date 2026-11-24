# Third-party licences

Janus Split Screen itself is free software under the GNU General Public
License, version 3 or (at your option) any later version: see
[../LICENSE](../LICENSE). Its handler scripts (`handlers/`) are part of it.

Below: the licences of the third-party code that Janus redistributes. Each
keeps its own licence; bundling it with Janus doesn't change that.

| Component | Where in Janus | Licence | Licence text | Source |
|---|---|---|---|---|
| [gbe_fork](https://github.com/Detanup01/gbe_fork) (Goldberg Steam Emulator) | `vendor/goldberg/` | LGPL-3.0 | [LGPL-3.0.txt](LGPL-3.0.txt) + [GPL-3.0.txt](GPL-3.0.txt) | regular: [release-2026_01_19](https://github.com/Detanup01/gbe_fork/releases/tag/release-2026_01_19), debug: [release-2026_05_16](https://github.com/Detanup01/gbe_fork/releases/tag/release-2026_05_16) |
| gbe_fork ColdClientLoader + steamclient64 | `handlers/payday-2/payload/coldclient/` | LGPL-3.0 | same | [release-2026_05_30](https://github.com/Detanup01/gbe_fork/releases/tag/release-2026_05_30) (`steamclient_experimental/`) |
| Libraries inside gbe_fork (curl, mbedtls, protobuf, zlib, nlohmann json, ...) | inside the gbe_fork binaries | various | [gbe_fork-CREDITS.md](gbe_fork-CREDITS.md) (identical in all three releases) | links in that file |
| [Steamless](https://github.com/atom0s/Steamless) 3.1.0.5 | `vendor/steamless/` | CC BY-NC-ND 4.0 (non-commercial, no derivatives) | [Steamless-LICENSE.txt](Steamless-LICENSE.txt) | https://github.com/atom0s/Steamless |
| SharpDisasm (+ udis86) | `vendor/steamless/Plugins/SharpDisasm.dll` | BSD-2-Clause | [SharpDisasm-LICENSE.md](SharpDisasm-LICENSE.md) | https://github.com/spazzarama/SharpDisasm |
| BepInEx 5, HarmonyX, MonoMod, Mono.Cecil, UnityDoorstop | `vendor/bepinex/` | MIT; Doorstop LGPL-2.1 | `vendor/bepinex/licenses/` | `vendor/bepinex/README.md` |
| [RE-UE4SS](https://github.com/UE4SS-RE/RE-UE4SS) (Unreal mod loader) + its per-game configs | `vendor/ue4ss/`, `handlers/far-far-west/payload/ue4ss/` | MIT | `vendor/ue4ss/win64/ue4ss/LICENSE` | `vendor/ue4ss/README.md` |
| [quickjs-ng](https://github.com/quickjs-ng/quickjs) Python binding | `vendor/quickjs/` | MIT | `vendor/quickjs/*.dist-info/licenses/LICENSE` | https://github.com/quickjs-ng/quickjs |
| **Zip release only** (`pylib/`): | | | | |
| PySide6-Essentials + shiboken6 6.11.2 (Qt for Python, with the Qt 6.11.2 libraries) | `pylib/PySide6/`, `pylib/shiboken6/` | LGPL-3.0 (or GPL-2.0/3.0) | [LGPL-3.0.txt](LGPL-3.0.txt) + [GPL-3.0.txt](GPL-3.0.txt) | https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.11.2-src/ and https://download.qt.io/official_releases/qt/6.11/6.11.2/single/ |
| Third-party code inside Qt (harfbuzz, freetype, libpng, pcre2, ...) | inside the Qt libraries | various | [Qt-6.11-third-party.txt](Qt-6.11-third-party.txt) (component list; full texts on Qt's pages) | https://doc.qt.io/qt-6.11/licenses-used-in-qt.html |
| python-xlib 0.33 | `pylib/Xlib/` | LGPL-2.1 | `pylib/python_xlib-0.33.dist-info/LICENSE` | https://github.com/python-xlib/python-xlib |
| six 1.17.0 | `pylib/six.py` | MIT | `pylib/six-1.17.0.dist-info/LICENSE` | https://github.com/benjaminp/six |

All binaries are unmodified upstream builds. The LGPL libraries are loaded as
separate shared libraries / DLLs, so they can be replaced with your own builds
of the same projects.

**gbe_fork versions** (matched by sha256 against the release archives
`emu-{linux,win}-{release,debug}`): `vendor/goldberg/regular/` is
release-2026_01_19, `vendor/goldberg/debug/` is release-2026_05_16, the PAYDAY 2
payload is release-2026_05_30. The source of each is the repository at that tag.

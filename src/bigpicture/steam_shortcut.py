"""Read/write Steam's non-Steam-game shortcuts (userdata/<id>/config/shortcuts.vdf).

Used to warn when the Steam overlay is enabled for the Janus shortcut: the
overlay can't draw on a native Wayland window anyway (it only hooks GLX and
X11 Vulkan surfaces), and the environment can't tell — Steam sets the same
LD_PRELOAD/ENABLE_VK_LAYER vars either way and the overlay library asks the
client at runtime. The per-shortcut AllowOverlay flag is the only signal.

The writer (_dump_binary_vdf) is used by install_shortcut.py.
"""

import struct
from pathlib import Path

from steam_input import parse_text_vdf as _parse_text_vdf  # noqa: E402


def _parse_binary_vdf(data: bytes, pos: int = 0):
    """Binary VDF map starting at *pos* -> (dict, end position).
    Types: 0x00 nested map, 0x01 string, 0x02 int32, 0x08 end of map."""
    out = {}
    while pos < len(data):
        t = data[pos]
        pos += 1
        if t == 0x08:
            return out, pos
        end = data.index(b'\0', pos)
        key = data[pos:end].decode('utf-8', 'replace')
        pos = end + 1
        if t == 0x00:
            out[key], pos = _parse_binary_vdf(data, pos)
        elif t == 0x01:
            end = data.index(b'\0', pos)
            out[key] = data[pos:end].decode('utf-8', 'replace')
            pos = end + 1
        elif t == 0x02:
            out[key] = struct.unpack_from('<i', data, pos)[0]
            pos += 4
        else:
            raise ValueError(f'unsupported binary VDF type 0x{t:02x}')
    return out, pos


def _dump_binary_vdf(doc: dict) -> bytes:
    """Inverse of _parse_binary_vdf: dict -> binary VDF map (str, int32 and
    nested dicts), terminated by 0x08."""
    out = bytearray()
    for key, value in doc.items():
        name = str(key).encode('utf-8') + b'\0'
        if isinstance(value, dict):
            out += b'\x00' + name + _dump_binary_vdf(value)
        elif isinstance(value, int):
            out += b'\x02' + name + struct.pack('<i' if value < 1 << 31 else '<I', value)
        else:
            out += b'\x01' + name + str(value).encode('utf-8') + b'\0'
    return bytes(out + b'\x08')


def find_shortcut(steam_roots, appid: int):
    """(entry, userdata dir) for the shortcuts.vdf entry whose appid is
    *appid* (as in $SteamAppId, unsigned) — any Steam user on any root —
    or (None, None)."""
    signed = appid - (1 << 32) if appid >= 1 << 31 else appid
    for root in steam_roots:
        for f in Path(root, 'userdata').glob('*/config/shortcuts.vdf'):
            try:
                doc, _ = _parse_binary_vdf(f.read_bytes())
            except (OSError, ValueError):
                continue
            for entry in (doc.get('shortcuts') or {}).values():
                if isinstance(entry, dict) and entry.get('appid') in (appid, signed):
                    return entry, f.parent.parent
    return None, None


def overlay_enabled(steam_roots, appid: int) -> bool | None:
    """Is the Steam overlay on for the shortcut *appid*?

    True/False when the shortcut's properties say so explicitly ("Enable the
    Steam Overlay while in-game", stored as apps/<signed appid>/
    OverlayAppEnable in that user's localconfig.vdf; or the shortcut's own
    AllowOverlay=0). None when it follows Steam's global overlay setting,
    which isn't stored anywhere readable (default: on). Shortcut not
    found -> None too."""
    entry, userdir = find_shortcut(steam_roots, appid)
    if entry is None:
        return None
    if entry.get('AllowOverlay', entry.get('allowoverlay')) == 0:
        return False
    signed = appid - (1 << 32) if appid >= 1 << 31 else appid
    try:
        cfg = _parse_text_vdf((userdir / 'config' / 'localconfig.vdf')
                              .read_text(errors='replace'))
    except OSError:
        return None
    apps = (cfg.get('UserLocalConfigStore') or {}).get('apps') or {}
    for key in (str(signed), str(appid)):
        flag = (apps.get(key) or {}).get('OverlayAppEnable')
        if flag is not None:
            return str(flag) != '0'
    return None

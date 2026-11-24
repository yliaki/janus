#!/usr/bin/env python3
"""Unit tests for src/host/steam_input.py (the "Enable Steam Input" launch
check). Uses a fake localconfig.vdf, never the real Steam config.

    python3 tests/test_steam_input.py
"""

import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'src' / 'host'))

import steam_input  # noqa: E402

LOCALCONFIG = r'''"UserLocalConfigStore"
{
	"Software" { "Valve" { "Steam" { "apps" { "10" { "LastPlayed" "1" } } } } }
	"blob"		"{\"json\":\"with \\\"quotes\\\"\"}"
	"apps"
	{
		"10"	{ "UseSteamControllerConfig"		"2" }
		"20"	{ "UseSteamControllerConfig"		"1" }
		"30"	{ "UseSteamControllerConfig"		"0" }
		"40"	{ "OverlayAppEnable"		"0" }
	}
}
'''


class FakeScript:
    def __init__(self, **sheet):
        self.sheet = sheet

    def get(self, field, default=None):
        return self.sheet.get(field, default)


fails = []


def check(cond, msg):
    print(('ok   ' if cond else 'FAIL ') + msg)
    if not cond:
        fails.append(msg)


def blocks(appid, script=None):
    try:
        steam_input.check(appid, script, 'Game')
        return None
    except steam_input.SteamInputNotEnabled as e:
        return str(e)


with tempfile.TemporaryDirectory() as tmp:
    cfg = Path(tmp) / 'localconfig.vdf'
    cfg.write_text(LOCALCONFIG)
    steam_input._localconfig_files = lambda: [Path(tmp) / 'missing.vdf', cfg]

    check(steam_input.setting(10) == '2', 'enabled -> "2" (not the Software/.../apps block)')
    check(steam_input.setting(20) == '1', 'Use default settings -> "1"')
    check(steam_input.setting(30) == '0', 'disabled -> "0"')
    check(steam_input.setting(40) == '', 'app entry without the key -> ""')
    check(steam_input.setting(50) == '', 'no app entry -> ""')

    check(blocks(10) is None, 'enabled launches')
    check('"Use default settings"' in (blocks(20) or ''), 'default blocks, names the setting')
    check('"Disable Steam Input"' in (blocks(30) or ''), 'disabled blocks, names the setting')
    check(blocks(50) is not None, 'never touched blocks')
    check(blocks(30, FakeScript(requireSteamInput=False)) is None,
          'Game.requireSteamInput = false opts out')
    check(blocks(30, FakeScript(requireSteamInput=True)) is not None,
          'Game.requireSteamInput = true still checks')
    check(blocks(30, FakeScript(requireSteamInput='disabled')) is None,
          'requireSteamInput = "disabled" launches when disabled')
    check('"Disable Steam Input"' in (blocks(10, FakeScript(requireSteamInput='disabled')) or ''),
          'requireSteamInput = "disabled" blocks enabled, asks to disable')
    check(blocks(20, FakeScript(requireSteamInput='disabled')) is not None,
          'requireSteamInput = "disabled" blocks default settings')
    check(isinstance(steam_input.SteamInputNotEnabled('x'), ValueError),
          'SteamInputNotEnabled is a ValueError (GUI shows it as "Can\'t launch")')

    steam_input._localconfig_files = lambda: [Path(tmp) / 'missing.vdf']
    check(steam_input.setting(10) is None, 'no localconfig.vdf -> None')
    check(blocks(10) is None, 'unreadable setting does not block')

# --- Gamepad layout per controller (Game.requireGamepadLayout) ---------------
import os  # noqa: E402
from types import SimpleNamespace as Pad  # noqa: E402


def configset(d, name, apps, mtime):
    body = ''.join(f'\t"{a}"\n\t{{\n\t\t"{k}"\t\t"{v}"\n\t}}\n'
                   for a, (k, v) in apps.items())
    f = Path(d) / name
    f.write_text(f'"controller_config"\n{{\n{body}}}\n')
    os.utime(f, (mtime, mtime))


with tempfile.TemporaryDirectory() as d:
    configset(d, 'configset_46d-c21d-d48d305.vdf',
              {500: ('template', 'controller_xbox360_gamepad_joystick.vdf'),
               600: ('template', 'controller_xbox360_wasd.vdf')}, 200)
    configset(d, 'configset_46d-c21d-d48d305g.vdf', {700: ('template', 'x_gamepad.vdf')}, 100)
    configset(d, 'configset_57e-2009-d48d305g.vdf', {}, 100)
    configset(d, 'configset_NSP98b6e949e8bd.vdf',
              {500: ('template', 'controller_switch_pro_gamepad_joystick.vdf')}, 100)
    configset(d, 'configset_45e-28e-d48d305.vdf', {500: ('workshop', '12345')}, 100)
    gl = lambda appid, vid, pid, uniq='': steam_input.gamepad_layout(appid, vid, pid, uniq, [d])
    check(gl(500, 0x46d, 0xc21d) == 'gamepad', 'gamepad template -> gamepad')
    check(gl(600, 0x46d, 0xc21d) == 'controller_xbox360_wasd.vdf', 'other template named')
    check(gl(700, 0x46d, 0xc21d) == 'gamepad', 'entry in an older suffix variant found')
    check(gl(800, 0x46d, 0xc21d) == 'missing', 'no entry -> missing')
    check(gl(500, 0x57e, 0x2009, '98:B6:E9:49:E8:BD') == 'gamepad',
          'Nintendo pad matched by its MAC')
    check(gl(500, 0x57e, 0x2009) == 'missing', 'same pad without MAC: missing')
    configset(d, 'configset_57e-2009-d48d305g.vdf',
              {600: ('template', 'controller_switch_pro_gamepad_joystick.vdf')}, 50)
    check(gl(600, 0x57e, 0x2009, '98:b6:e9:49:e8:bd') == 'missing',
          'MAC-keyed pad: a USB-ID file entry does not count')
    check(gl(600, 0x57e, 0x2009) == 'gamepad', 'same model over USB uses the USB-ID file')
    check(gl(500, 0x45e, 0x28e) is None, 'workshop config: unknown, not blocked')
    check(gl(500, 0x1234, 0x5678) is None, 'no configset file: unknown')

    f310 = Pad(name='F310', vendor_id=0x46d, product_id=0xc21d, uniq='')
    other = Pad(name='Other', vendor_id=0x1234, product_id=0x5678, uniq='')

    def layout_blocks(appid, script, pads):
        try:
            steam_input.check_gamepad_layout(appid, script, pads, 'Game', [d])
        except steam_input.GamepadLayoutMissing as e:
            return str(e)
        return None
    need = FakeScript(requireGamepadLayout=True)
    check(layout_blocks(800, None, [f310]) is None, 'no handler: not checked')
    check(layout_blocks(800, FakeScript(), [f310]) is None, 'not required: not checked')
    check(layout_blocks(500, need, [f310, other]) is None, 'all gamepad/unknown: launches')
    msg = layout_blocks(600, need, [f310]) or ''
    check('F310 (controller_xbox360_wasd.vdf)' in msg and 'Gamepad' in msg,
          'non-gamepad template blocks, names pad + template')
    check('F310' in (layout_blocks(800, need, [f310]) or ''), 'missing layout blocks')
    check(isinstance(steam_input.GamepadLayoutMissing('x'), steam_input.SteamInputNotEnabled),
          'GamepadLayoutMissing is caught where SteamInputNotEnabled is')

print(f'\n{"FAILED: " + str(len(fails)) if fails else "all passed"}')
sys.exit(1 if fails else 0)

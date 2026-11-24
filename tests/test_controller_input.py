#!/usr/bin/env python3
"""Unit tests for Janus input mode: controller_input.py (modes, warnings),
steam_input.check per mode, pad_mappings.py (stores, MappingRecorder),
padd.py (pad identity, Nintendo simple reports), input_ns.py.

Uses a temp $HOME and a fake localconfig.vdf; no pads or games needed (SDL
only to check that recorded mappings parse, skipped without it).

    python3 tests/test_controller_input.py
"""

import ctypes
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'src' / 'host'))

_tmp_home = tempfile.TemporaryDirectory()
os.environ['HOME'] = _tmp_home.name

import controller_input as ci  # noqa: E402
import pad_mappings  # noqa: E402
import padd  # noqa: E402
import steam_input  # noqa: E402
import input_ns  # noqa: E402

pad_mappings.PADS_FILE = Path(_tmp_home.name) / '.config' / 'janus' / 'pads.json'

fails = []


def check(cond, msg):
    print(('ok   ' if cond else 'FAIL ') + msg)
    if not cond:
        fails.append(msg)


class Sheet(dict):
    """A handler sheet with HandlerScript's get()."""


class Pad:
    def __init__(self, name, vid, pid, uniq='', index=0):
        self.name, self.vendor_id, self.product_id = name, vid, pid
        self.uniq, self.index = uniq, index


# --- controller_input: modes ---------------------------------------------------------

check(ci.supported(None) == ['janus', 'steam'], 'no handler: both, Janus first')
check(ci.supported(Sheet()) == ['janus', 'steam'], 'defaults: both')
check(ci.supported(Sheet(janusInput=False)) == ['steam'], 'janusInput false: Steam only')
check(ci.supported(Sheet(steamInput=False)) == ['janus'], 'steamInput false: Janus only')
check(ci.supported(Sheet(janusInput=False, steamInput=False)) == ['steam'],
      'both false: falls back to Steam Input')
check(ci.resolve(Sheet(), None) == 'janus', 'default mode = Janus input')
check(ci.resolve(Sheet(), 'steam') == 'steam', 'explicit Steam Input honoured')
check(ci.resolve(Sheet(janusInput=False), 'janus') == 'steam',
      'unsupported request -> the handler\'s mode')
check(ci.resolve(Sheet(), 'bogus') == 'janus', 'unknown request -> default')

# --- steam_input.check per mode ------------------------------------------------------

LOCALCONFIG = r'''"UserLocalConfigStore"
{
	"apps"
	{
		"10"	{ "UseSteamControllerConfig"		"2" }
		"20"	{ "UseSteamControllerConfig"		"1" }
		"30"	{ "UseSteamControllerConfig"		"0" }
	}
}
'''
cfg = Path(_tmp_home.name) / 'localconfig.vdf'
cfg.write_text(LOCALCONFIG)
steam_input._localconfig_files = lambda: [cfg]


def outcome(appid, mode, sheet=None):
    try:
        steam_input.check(appid, sheet if sheet is not None else Sheet(), 'Game', mode)
        return 'ok'
    except steam_input.SteamInputNotEnabled as e:
        return ('refused', e.alternative, str(e))


check(outcome(30, 'janus') == 'ok', 'Janus input + "Disable Steam Input": launches')
r = outcome(10, 'janus')
check(r[0] == 'refused' and r[1] == 'steam' and 'Disable Steam Input' in r[2],
      'Janus input + "Enable": refused, Steam Input offered instead')
r = outcome(20, 'janus')
check(r[0] == 'refused' and r[1] is None, 'Janus input + "Use default": refused, no alternative')
r = outcome(10, 'janus', Sheet(steamInput=False))
check(r[0] == 'refused' and r[1] is None,
      'Janus-only game + "Enable": no Steam Input alternative')
check(outcome(10, 'steam') == 'ok', 'Steam Input + "Enable": launches')
r = outcome(30, 'steam')
check(r[0] == 'refused' and r[1] == 'janus', 'Steam Input + "Disable": Janus input offered')
check(outcome(10, 'janus', Sheet(requireSteamInput=False)) == 'ok',
      'requireSteamInput false: Janus input launches whatever the setting')
r = outcome(10, 'steam', Sheet(requireSteamInput='disabled'))
check(r[0] == 'refused' and 'shared mouse' in r[2],
      'requireSteamInput "disabled" keeps its Steam Input mode message')
check(outcome(10, 'janus', Sheet(requireSteamInput='disabled'))[0] == 'refused',
      'requireSteamInput "disabled" + Janus input needs Disable too')
check(outcome('minecraft', 'janus') == 'ok', 'non-Steam game: no Steam setting check')

# --- pad warnings ---------------------------------------------------------------------

twins = [Pad('Twin', 0x0810, 0x0001, index=1), Pad('Twin', 0x0810, 0x0001, index=2)]
w = ci.pad_warnings('steam', twins)
check(len(w) == 1 and 'identical' in w[0], 'Steam Input: identical serial-less pads warn')
check(ci.pad_warnings('steam', [Pad('DS4', 0x054c, 0x09cc, 'aa:bb'),
                                 Pad('DS4', 0x054c, 0x09cc, 'cc:dd')]) == [],
      'Steam Input: identical pads WITH serials are fine')
w = ci.pad_warnings('janus', [Pad('Steam Deck', 0x28de, 0x1205)], probed=[])
check(len(w) == 1 and 'Steam Input' in w[0], 'Janus input: Valve pad -> Steam Input advice')
import controllers  # noqa: E402
controllers_event_node = controllers.event_node
controllers.event_node = lambda i: f'/dev/input/event{50 + i}'
probed = [{'path': '/dev/input/event51', 'gamecontroller': False},
          {'path': '/dev/input/event52', 'gamecontroller': True}]
w = ci.pad_warnings('janus', twins, probed=probed)
check(len(w) == 1 and 'isn\'t a known gamepad' in w[0],
      'Janus input: the pad SDL can\'t map warns, the mapped one doesn\'t')
check(ci.pad_warnings('janus', twins, probed=[]) == [],
      'Janus input: pads the probe didn\'t see aren\'t flagged')
controllers.event_node = controllers_event_node

# --- pad_mappings: stores ---------------------------------------------------------------

check(pad_mappings.janus_mappings() == {}, 'no pads.json: no mappings')
m = '03000000100800000100000010010000,My Pad,a:b2,b:b1,platform:Linux,'
guid = pad_mappings.save_mapping(m)
check(guid == '03000000100800000100000010010000' and
      pad_mappings.janus_mappings()[guid] == m, 'mapping saved and read back')
try:
    pad_mappings.save_mapping('not a mapping')
    check(False, 'garbage mapping refused')
except ValueError:
    check(True, 'garbage mapping refused')
check(pad_mappings.remove_mapping(guid) and pad_mappings.janus_mappings() == {},
      'mapping removed')
check(not pad_mappings.positional(0x057e, 0x2009), 'Nintendo pads: by label by default')
pad_mappings.set_positional(0x057e, 0x2009, True)
check(pad_mappings.positional(0x057e, 0x2009), 'positional toggled on (per model)')
check(not pad_mappings.positional(0x057e, 0x2006), '... other models unaffected')
pad_mappings.set_positional(0x057e, 0x2009, False)
check(not pad_mappings.positional(0x057e, 0x2009), 'positional toggled off')

# --- MappingRecorder -------------------------------------------------------------------

REST = ([0] * 12, [0, 0, -32768, 0, 0, 0], [0])   # axis 2 = a full-range trigger


def st(buttons=(), axes=None, hat=0):
    b = [0] * 12
    for i in buttons:
        b[i] = 1
    a = list(REST[1])
    for i, v in (axes or {}).items():
        a[i] = v
    return (b, a, [hat])


steps = [('a', '', 'button'), ('dpup', '', 'dpad'), ('dpdown', '', 'dpad'),
         ('lefttrigger', '', 'trigger'), ('righttrigger', '', 'trigger'),
         ('leftx', '', 'stick'), ('lefty', '', 'stick'), ('b', '', 'button')]
rec = pad_mappings.MappingRecorder('0300aaaa', 'Odd, Pad', steps)
rec.feed(REST)
rec.feed(st([2]))                         # a = b2
rec.feed(st([2]))                         # still held: ignored
rec.feed(REST)                            # released
rec.feed(st(hat=1))                       # dpup = hat up
rec.feed(REST)
rec.feed(st(axes={1: 32767}))             # dpdown = half axis +a1
rec.feed(REST)
rec.feed(st(axes={2: 32767}))             # lefttrigger: full axis resting at -32768
rec.feed(REST)
rec.feed(st([6]))                         # righttrigger: a digital button
rec.feed(REST)
rec.feed(st(axes={0: -30000}))            # leftx pushed the 'wrong' way: inverted
rec.feed(REST)
rec.feed(st([2]))                         # lefty: b2 is taken by 'a' -> ignored
check(rec.step == 6, 'an input bound already isn\'t taken twice')
rec.feed(st(axes={3: 32000}))             # lefty = a3
rec.feed(REST)
rec.skip()                                # b skipped
check(rec.finished, 'recorder finished after the last step')
b = rec.bindings
check(b == {'a': 'b2', 'dpup': 'h0.1', 'dpdown': '+a1', 'lefttrigger': 'a2',
            'righttrigger': 'b6', 'leftx': 'a0~', 'lefty': 'a3'},
      f'bindings recorded: {b}')
mp = rec.mapping()
check(mp.startswith('0300aaaa,Odd  Pad,') and mp.endswith('platform:Linux,'),
      'mapping string: GUID, name without commas, platform')

rec = pad_mappings.MappingRecorder('0300bbbb', 'Pad', steps[:3])
rec.feed(REST)
rec.feed(st([0]))
rec.feed(REST)
rec.back()
check(rec.step == 0 and 'a' not in rec.bindings, 'back undoes the previous step')
rec.feed(st([0]))                          # still waiting for release after back
check(rec.step == 0, '... and waits for everything to be let go first')
rec.feed(REST)
rec.feed(st([1]))
check(rec.bindings.get('a') == 'b1', '... then records anew')

# The A that picked the pad is still held in the first snapshot: once let go it
# must be recordable as A (it used to count as rest and was never taken).
rec = pad_mappings.MappingRecorder('0300cccc', 'Pad', steps[:2])
rec.feed(st([0]))                          # rest with b0 held
rec.feed(st([0]))
check(rec.step == 0, 'a button held at the start is not taken')
rec.feed(REST)
rec.feed(st([0]))
check(rec.bindings.get('a') == 'b0', 'the button held at the start is recordable once let go')

# Two inputs at once, the first already set: the other one is taken.
rec = pad_mappings.MappingRecorder('0300dddd', 'Pad', steps[:3])
rec.feed(REST)
rec.feed(st([3]))
rec.feed(REST)
rec.feed(st([3], hat=1))
check(rec.bindings.get('dpup') == 'h0.1' and rec.taken is None,
      'a set input pressed with a free one: the free one is recorded')
rec.feed(REST)
rec.feed(st([3]))
check(rec.taken == 'a' and rec.step == 2, 'a press of a set input is reported as taken')

# Live values through the recorded bindings.
rec = pad_mappings.MappingRecorder('0300eeee', 'Pad', steps)
rec.rest = REST
rec.bindings = {'a': 'b2', 'dpup': 'h0.1', 'dpdown': '+a1', 'lefttrigger': 'a2', 'leftx': 'a0~'}
v = rec.values(st([2], axes={1: 32767, 2: 32767, 0: -32768}, hat=1))
check(v == {'a': 1.0, 'dpup': 1.0, 'dpdown': 1.0, 'lefttrigger': 1.0, 'leftx': 1.0},
      f'values(): pressed state read through the bindings ({v})')
v = rec.values(REST)
check(v == {'a': 0.0, 'dpup': 0.0, 'dpdown': 0.0, 'lefttrigger': 0.0, 'leftx': -0.0},
      f'values(): rest state ({v})')

# SDL accepts what the recorder writes.
try:
    sdl = padd.load_sdl()
    import ctypes
    sdl.SDL_GameControllerAddMapping.restype = ctypes.c_int
    real = pad_mappings.MappingRecorder('03000000100800000100000010010000', 'Rec', steps)
    real.bindings = dict(b)
    check(sdl.SDL_GameControllerAddMapping(real.mapping().encode()) >= 0,
          'SDL parses a recorded mapping')
except OSError:
    print('skip SDL parse check (no libSDL2)')

# --- Steam's stored mappings are not used ------------------------------------------
# Steam's config.vdf SDL_GamepadBind holds one per pad it saw, in its own view
# ("Xbox 360 Controller" with the D-pad on buttons killed xpad's hat D-pad):
# only SDL's database and Janus's own mappings count.

root = Path(_tmp_home.name) / 'steamroot'
(root / 'config').mkdir(parents=True)
(root / 'config' / 'config.vdf').write_text(
    '"InstallConfigStore"\n{\n\t"SDL_GamepadBind"\t\t"'
    '030000005e0400008e02000010010000,Xbox 360 Controller,a:b0,b:b1,dpup:b11,platform:Linux,"\n}\n')
import steam  # noqa: E402
steam.find_all_steam_roots = lambda: [root]
pad_mappings.save_mapping(m)


class _FakeSDL:
    def __init__(self):
        self.added = []
        self.SDL_GameControllerAddMapping = lambda s: self.added.append(s.decode()) or 1


fake = _FakeSDL()
check(pad_mappings.apply_to_sdl(fake) == 1 and fake.added == [m],
      'only Janus\'s mappings are added to SDL (not Steam\'s config.vdf ones)')

# --- padd: Nintendo simple HID reports ---------------------------------------------

rep = bytes([0x3F, 0b00000001 | 0b01000000, 0b00000010, 2,
             0x00, 0xC0, 0x00, 0x80, 0x00, 0x80, 0x00, 0x40])
st3 = padd.parse_simple_report(rep)
check(st3['buttons'][0] and not st3['buttons'][1], '0x3f: A (label) -> SDL a')
check(st3['buttons'][6], '0x3f: + -> SDL start')
check(st3['axes'][4] == 32767 and st3['axes'][5] == 0, '0x3f: ZL -> left trigger')
check(st3['hat'] == (1, 0), '0x3f: hat 2 -> right')
check(st3['axes'][0] == 0x4000 and st3['axes'][1] == 0 and st3['axes'][3] == -0x4000,
      '0x3f: sticks centred on 0x8000')
check(padd.parse_simple_report(bytes([0x30] + [0] * 20)) is None,
      '0x30 reports are left to evdev')

# --- padd: which player a (re)appearing pad belongs to ----------------------------

def players():
    return [{'player': 1, 'node': '/dev/input/event10', 'vid': 1, 'pid': 2, 'uniq': '',
             'phys': 'usb-1/input0', 'attached': None, 'seen': False},
            {'player': 2, 'node': '/dev/input/event11', 'vid': 1, 'pid': 2, 'uniq': '',
             'phys': 'usb-2/input0', 'attached': None, 'seen': False}]


def ident(node, phys='', uniq='', vid=1, pid=2):
    return {'node': node, 'vid': vid, 'pid': pid, 'uniq': uniq, 'phys': phys, 'name': 'x'}


ps = players()
check(padd.pick_player(ps, ident('/dev/input/event11', 'usb-2/input0'))['player'] == 2,
      'start: a pad takes the player it was given (event node)')
check(padd.pick_player(ps, ident('/dev/input/event12', 'usb-9/input0')) is None,
      'start: a stranger\'s identical pad takes no seat')
check(padd.pick_player(ps, ident('/dev/input/event12', vid=9)) is None,
      'other models never match')
ps[0].update(seen=True)
check(padd.pick_player(ps, ident('/dev/input/event20', 'usb-1/input0'))['player'] == 1,
      'reconnect on the same port: same player')
check(padd.pick_player(ps, ident('/dev/input/event21', 'usb-7/input0'))['player'] == 1,
      'reconnect on another port (serial-less): the orphaned player')
ps = players()
ps[0].update(uniq='aa:bb', seen=True)
ps[1].update(uniq='cc:dd', seen=True)
check(padd.pick_player(ps, ident('/dev/input/event30', uniq='cc:dd'))['player'] == 2,
      'reconnect with a serial/MAC: matched by it')
check(padd.pick_player(ps, ident('/dev/input/event31', uniq='ee:ff')) is None,
      'an unknown serial takes no seat')
ps = players()
check(padd.pick_player(ps, ident('/dev/input/event12', 'usb-1/input0', vid=9)) is None,
      'start: another model on a player\'s port takes no seat (not seen yet)')
ps[0].update(seen=True)
check(padd.pick_player(ps, ident('/dev/input/event40', 'usb-1/input0', vid=0x45e, pid=0x28e))['player'] == 1,
      'reconnect on the same USB port as another model (pad switched mode): same player')
check(padd.pick_player(ps, ident('/dev/input/event41', 'usb-7/input0', vid=0x45e, pid=0x28e)) is None,
      'another model on another port takes no seat')
ps = players()
for p_ in ps:
    p_.update(seen=True, phys='5c:e9:1e:00:00:01')
check(padd.pick_player(ps, ident('/dev/input/event42', '5c:e9:1e:00:00:01', vid=9)) is None,
      'Bluetooth (adapter phys): another model never matches by phys')

# --- Big Picture: is a (re)appearing pad the one a player joined with? ----------

sys.path.insert(0, str(REPO / 'src'))
try:
    from bigpicture.backend import Backend as _BP  # noqa: E402
except Exception as e:  # noqa: BLE001 — PySide6 missing
    _BP = None
    print(f'skip Big Picture checks ({e})')
if _BP is not None:
    def bp(vid=1, pid=2, phys='0000:00:14.0-3.1', usb=True, uniq='', name='x'):
        return {'vid': vid, 'pid': pid, 'phys': phys, 'usb': usb, 'uniq': uniq, 'name': name}
    same = _BP._same_pad
    check(same(bp(), bp()), 'Big Picture: same model, same port')
    check(same(bp(), bp(vid=0x45e, pid=0x28e)),
          'Big Picture: another model on the same USB port (pad changed mode)')
    check(not same(bp(), bp(vid=0x45e, pid=0x28e, phys='0000:00:14.0-3.2')),
          'Big Picture: another model on another port is another pad')
    check(not same(bp(phys='5c:e9:1e:00:00:01', usb=False),
                   bp(vid=0x45e, pid=0x28e, phys='5c:e9:1e:00:00:01', usb=False)),
          'Big Picture: Bluetooth (adapter phys): another model is another pad')

# --- input_ns ------------------------------------------------------------------------

if input_ns.available():
    args = input_ns.wrap(['true'], ['/dev/null'])
    check(args[-2:] == ['--', 'true'] and '--die-with-parent' in args,
          'input_ns.wrap: bwrap command ending in the game command')
    check(not any(a.startswith('/dev/hidraw') or a == '/dev/uinput' for a in args),
          'input_ns.wrap: no hidraw / uinput in the private /dev')
    import subprocess
    r = subprocess.run(input_ns.wrap(['ls', '/dev/input'], ['/dev/null']),
                       capture_output=True, text=True)
    check(r.returncode == 0 and r.stdout.split() == [],
          'input_ns: /dev/input inside is empty except what is allowed')
else:
    print('skip input_ns checks (no unprivileged user namespaces)')

print(f'\n{len(fails)} failure(s)' if fails else '\nall passed')
sys.exit(1 if fails else 0)

#!/usr/bin/env python3
"""Unit tests for src/host/profiles.py (player profiles) and the profile-aware
isolation in steam.build_environment(). Runs against a throwaway $HOME.

    python3 tests/test_profiles.py
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Everything below runs in a child process with HOME pointed at a temp dir:
# profiles.py resolves its paths from Path.home() at import time.
CHILD = r'''
import os, sys, json
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, sys.argv[1])
H = Path.home()
JANUS = H / '.local/share/janus'

import profiles

fails = []
def check(cond, msg):
    print(('  ok: ' if cond else '  FAIL: ') + msg)
    if not cond:
        fails.append(msg)

ps = profiles.load()
check(len(ps) == 1 and ps[0].id == 'owner' and ps[0].owner, 'fresh store: only the owner')
check(not profiles.CONFIG_FILE.exists(), 'loading writes nothing')

# --- defaults
check(profiles.default_for_instance(1).owner, 'instance 1 defaults to the owner')
check(profiles.peek_default(2) is None, 'peek_default never creates')
p2 = profiles.default_for_instance(2)
check(p2.name == 'Player 2' and not p2.owner, 'instance 2 creates "Player 2"')
check(profiles.default_for_instance(2).id == p2.id, '... and reuses it')
check(profiles.peek_default(2).id == p2.id, 'peek_default finds it')

# --- create / rename / delete
alice = profiles.create('  Alice ')
check(alice.name == 'Alice', 'names are trimmed')
check(76561198100000000 <= int(alice.steam_id) < 76561198900000000 and alice.steam_id != p2.steam_id,
      'new profiles get distinct random Steam IDs')
try:
    profiles.create('alice'); check(False, 'duplicate name refused')
except ValueError:
    check(True, 'duplicate name refused')
check(profiles.find('ALICE').id == alice.id, 'find by name, case-insensitive')
check(profiles.find(alice.id).id == alice.id, 'find by id')
profiles.rename(alice.id, 'Alicia')
check(profiles.get(alice.id).name == 'Alicia' and profiles.get(alice.id).steam_id == alice.steam_id,
      'rename keeps id and Steam ID')
try:
    profiles.rename('owner', 'x'); check(False, 'owner rename refused')
except ValueError:
    check(True, 'owner rename refused')
(alice.data_dir(550) / 'home').mkdir(parents=True)
profiles.delete(alice.id)
check(profiles.get(alice.id) is None and (alice.data_dir(550) / 'home').is_dir(),
      'delete keeps the saves by default')

# --- sessions
s = profiles.resolve_session([(None, 1), ('Player 2', 2)])
check([p.id for p in s] == ['owner', p2.id], 'resolve_session by default + name')
try:
    profiles.resolve_session([('owner', 1), (None, 2), ('owner', 3)])
    check(False, 'one profile twice refused')
except ValueError as e:
    check('both player 1 and player 3' in str(e), 'one profile twice refused')
try:
    profiles.resolve('Nobody', 1); check(False, 'unknown profile refused')
except ValueError:
    check(True, 'unknown profile refused')

# --- pad memory
profiles.remember_pads({'045e:028e:aa': p2.id})
check(profiles.remembered_for_pad('045e:028e:aa') == p2.id, 'pad -> profile remembered')

# --- isolation follows the owner, not the instance number
import steam
game = SimpleNamespace(appid=550, is_proton=False, is_native=True, is_32bit=False,
                       steam_root=H / 'steam', install_dir=H / 'game',
                       compat_data_path=None)
def env_for(inst, prof, iso='secondary'):
    return steam.build_environment(game, 0, inst, iso, False, profile=prof)
owner = profiles.steam_owner()
e = env_for(2, owner)
check('PRESSURE_VESSEL_HOME' not in e, 'owner as P2 plays on the real home (secondary)')
e = env_for(1, p2)
check(e.get('PRESSURE_VESSEL_HOME') == str(JANUS / f'profiles/{p2.id}/550/home'),
      "a virtual profile as P1 is isolated in its own profile dir")
e = env_for(1, owner, 'full')
check(e.get('PRESSURE_VESSEL_HOME') == str(JANUS / 'profiles/owner/550/home'),
      'full isolation: the owner gets an isolated profile dir too')
e = steam.build_environment(game, 0, 1, 'secondary', False, goldberg_enabled=True, profile=p2)
ini = (JANUS / f'profiles/{p2.id}/550/home/.local/share/GSE Saves/settings/configs.user.ini').read_text()
check('account_name=Player 2' in ini and f'account_steamid={p2.steam_id}' in ini,
      "Goldberg identity is the profile's")

# --- save info (profile menus) + delete with data
import save_info, time as _t
bob = profiles.create('Bob')
check(save_info.dir_stats(bob.data_dir(550)) is None, 'no storage yet: no stats')
check(save_info.default_text('own', None) == 'No saves yet', 'empty profile: "No saves yet"')
d = bob.data_dir(550)
(d / 'home/.local/share/game').mkdir(parents=True)
(d / 'home/.local/share/game/save.dat').write_bytes(b'x' * 3000)
(d / 'tmp').mkdir()
(d / 'tmp/junk').write_bytes(b'x' * 99999)
(d / 'link').symlink_to('/usr')
st = save_info.dir_stats(d)
check(st[0] == 3000, f'size counts files, not tmp/ or symlinks ({st[0]})')
check(save_info.default_text('own', st).startswith('2.9 KB · played just now'),
      save_info.default_text('own', st))
check(save_info.default_text('real', st) == 'Real Steam saves', 'owner on real saves')
check(save_info.saves_tag(profiles.steam_owner(), 'secondary', False, False) == 'real', 'tag real')
check(save_info.saves_tag(bob, 'none', False, False) == 'shared', 'tag shared')
check(save_info.saves_tag(bob, 'none', True, False) == 'own', 'tag own with profileSaves')
handler = H / 'h.js'
handler.write_text("""Game.appid = 550;
Game.profileInfo = function (ctx) {
  if (ctx.profile.name !== 'Bob') return null;
  Janus.writeFile(ctx.saveRoot + '/nope', 'x');
  return 'never';
};
""")
texts = save_info.profile_texts(550, handler, {}, profiles.load(),
                                lambda p: save_info.saves_tag(p, 'secondary', False, False))
check(texts[bob.id].startswith('2.9 KB') and not (d / 'nope').exists(),
      'profileInfo is read-only (a write throws, the default line stays)')
handler.write_text("""Game.appid = 550;
Game.profileInfo = function (ctx) {
  const n = Janus.glob(ctx.saveRoot + '/home/.local/share/game/*.dat').length;
  return ctx.profile.owner ? null : n + ' worlds · ' + ctx.defaultText;
};
""")
texts = save_info.profile_texts(550, handler, {}, profiles.load(),
                                lambda p: save_info.saves_tag(p, 'secondary', False, False))
check(texts[bob.id].startswith('1 worlds · 2.9 KB'), texts[bob.id])
check(texts['owner'] == 'Real Steam saves', 'null keeps the default line')
os.chmod(d / 'home/.local/share/game', 0o500)
profiles.remember_pads({'padkey': bob.id})
profiles.delete(bob.id, delete_data=True)
check(not (profiles.DATA_DIR / bob.id).exists(), 'delete_data removes read-only saves too')
check(Path('/usr').exists(), '... without following symlinks')
check('padkey' not in (profiles._read().get('pads') or {}), 'pad memory forgets it')

print('ALL PASS' if not fails else f'{len(fails)} FAILED')
sys.exit(1 if fails else 0)
'''


def main():
    home = Path(tempfile.mkdtemp(prefix='janus-profiles-test-'))
    try:
        env = dict(os.environ, HOME=str(home))
        r = subprocess.run([sys.executable, '-c', CHILD, str(REPO / 'src' / 'host')],
                           env=env, cwd=home)
        sys.exit(r.returncode)
    finally:
        shutil.rmtree(home, ignore_errors=True)


if __name__ == '__main__':
    main()

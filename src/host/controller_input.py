"""How a session's controllers reach the games: Janus input or Steam Input.

janus  Janus's own input (default): Steam Input off for the game, the input
       daemon (padd.py) turns each player's pad into a virtual Xbox 360 pad,
       and each instance sees only its own one (private /dev, input_ns.py).
       Independent of Steam's routing, survives pads reconnecting.
steam  Steam Input: Steam remaps the pads into its virtual pads and the hook
       filters them per instance. For pads only Steam understands (Steam
       Controller/Deck trackpads, gyro, keyboard/mouse layouts).

A handler says which ones the game works with: ``Game.janusInput`` and
``Game.steamInput`` (both default true). The session setting
(``SessionConfig.input_mode``, CLI ``--input``) picks one; "" = the default
(Janus input when supported).
"""

from typing import Iterable, List, Optional

JANUS = 'janus'
STEAM = 'steam'
LABELS = {JANUS: 'Janus input', STEAM: 'Steam Input'}
VALVE_VID = 0x28de


def supported(script) -> List[str]:
    """The modes the handler allows, preferred first."""
    if script is None:
        return [JANUS, STEAM]
    modes = [m for m, key in ((JANUS, 'janusInput'), (STEAM, 'steamInput'))
             if script.get(key, True) is not False]
    return modes or [STEAM]


def resolve(script, requested: Optional[str] = None) -> str:
    """The mode a launch uses: *requested* if the handler allows it, else the
    handler's preferred one."""
    modes = supported(script)
    return requested if requested in modes else modes[0]


def other(mode: str) -> str:
    return STEAM if mode == JANUS else JANUS


def pad_warnings(mode: str, controllers: Iterable, probed=None) -> List[str]:
    """Per-pad advice for a session in *mode* (ControllerInfo-like pads):
    what won't work well, and when the other mode would be better."""
    controllers = list(controllers)
    out: List[str] = []
    if mode == JANUS:
        valve = [c for c in controllers if c.vendor_id == VALVE_VID]
        for c in valve:
            out.append(f'{c.name} works best with Steam Input: Janus input makes '
                       f'it a plain Xbox controller (no trackpads, gyro or Steam '
                       f'layouts).')
        try:
            import pad_mappings
            unknown = pad_mappings.unrecognised(
                [c for c in controllers if c not in valve], probed)
        except Exception:
            unknown = []
        for c in unknown:
            out.append(f'{c.name} ({c.vendor_id:04x}:{c.product_id:04x}) isn\'t a '
                       f'known gamepad, so Janus input can\'t turn it into an Xbox '
                       f'controller: it would send nothing. Set it up first (Big '
                       f'Picture: Y → Set up a controller, or Steam\'s controller '
                       f'settings), or play this game with Steam Input.')
    else:
        groups = {}
        for c in controllers:
            if not c.uniq:
                groups.setdefault((c.vendor_id, c.product_id), []).append(c)
        for (vid, pid), same in groups.items():
            if len(same) > 1:
                out.append(f'{len(same)} identical {same[0].name} controllers without '
                           f'serial numbers: Steam Input can mix them up between the '
                           f'games. Janus input tells them apart reliably.')
    return out


def start_daemon(game_key, assignments, verbose: bool = False):
    """Start the session's input daemon (padd.py) for *assignments*
    [(instance number, controller index)]; returns the running PadDaemon
    (its .nodes: {instance: [/dev/input nodes]}). Raises padd.PadDaemonError."""
    from controllers import list_controllers
    from padd import PadDaemon, PadDaemonError
    by_index = {c.index: c for c in list_controllers()}
    players = []
    for instance, index in assignments:
        c = by_index.get(index)
        if c is None:
            raise PadDaemonError(f'No controller at index {index} (unplugged?)')
        players.append((instance, c))
    daemon = PadDaemon(game_key, players, verbose)
    daemon.start()
    return daemon

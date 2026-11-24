"""Plain data model shared by the terminal and GUI frontends."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class InputDevice:
    """One /dev/input pad node (physical USB or Steam's virtual uinput)."""
    event: str                      # "event22"
    js: str                         # "js0"
    name: str
    vid: str                        # "045e"
    pid: str                        # "028e"
    origin: str                     # "physical" | "virtual"
    phys: str = ""                  # USB port path, e.g. "usb-0000:2a:00.1-2.1/input0"
    uniq: str = ""                  # serial/MAC when the device exposes one (often empty)
    pressed: set[str] = field(default_factory=set)   # currently-held button labels
    last_label: str = ""            # most recent event, e.g. "A press" / "DPadY -1"
    last_ns: int = 0                # monotonic-ish ordering (frame counter, not wall clock)

    @property
    def is_virtual(self) -> bool:
        return self.origin == "virtual"

    @property
    def port(self) -> str:
        """Short stable port id from phys (the '-2.1' bit) — the identical-pad tie-breaker."""
        if not self.phys:
            return ""
        tail = self.phys.split("-", 1)[-1]      # "0000:2a:00.1-2.1/input0" -> after first '-'
        return tail.split("/", 1)[0]            # -> "0000:2a:00.1-2.1" ... keep port suffix


@dataclass
class MappingRow:
    """One expected controller and where it actually maps in the Steam virtual layer."""
    phys_event: str
    phys_js: str
    phys_id: str                    # "36e8:3001"
    phys_port: str                  # stable USB port id
    expected_virtual: str | None    # by order (what Janus assumes): js index i -> Steam slot i
    detected_virtual: str | None    # by live button-coincidence (ground truth)
    votes: int                      # correlation confidence
    gamepad_index: int | None       # of the mapped virtual, links to ISteamInput
    handle: int | None              # ISteamInput handle for that gamepad_index
    status: str                     # "ok" | "mismatch" | "unverified"


@dataclass
class SteamController:
    """One controller as ISteamInput reports it over IPC."""
    handle: int
    input_type: str                 # "XBox360", "PS5", ...
    gamepad_index: int


@dataclass
class Activity:
    """Session state used to warn the user (esp. about Steam Input focus gating)."""
    steam_running: bool
    game_appid: str | None
    game_focused: bool | None       # is the X-focused window a game? None = couldn't tell
    steam_input_active: bool        # virtual pads exist OR IPC reports controllers
    ipc_available: bool             # libsteam_api.so loaded + inited
    level: str = "ok"               # "ok" | "warn" | "error"
    message: str = ""               # human warning ("" = nothing to say)


@dataclass
class ProbeSnapshot:
    devices: list[InputDevice]
    steam_controllers: list[SteamController]
    activity: Activity
    mapping: list[MappingRow] = field(default_factory=list)

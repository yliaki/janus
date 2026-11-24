"""Empirically link physical <-> virtual pads by button coincidence.

Metadata can't tell identical MAC-less pads apart, so the ground truth for
"physical pad X drives virtual pad Y" is behavioral: when a physical node and a virtual
node emit the SAME button within a short window, that's a vote for the pair. See NOTES.md.
"""
from __future__ import annotations

from collections import defaultdict, deque


class Correlator:
    def __init__(self, window: float = 0.08) -> None:
        self.window = window
        self._recent: deque = deque(maxlen=400)          # (t, event, origin, label)
        self.votes: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    def feed(self, t: float, presses: list[tuple[str, str, str]]) -> None:
        """presses: list of (event_node, origin, label) button-press events at time t.

        Vote for opposite-origin presses within the window. Prefer same-label matches
        (pads Steam passes through 1:1); fall back to any coincident press when none match
        (pads Steam REMAPS to different button codes, e.g. joystick->Xbox adapters).
        """
        for event, origin, label in presses:
            cands = []  # (event_node, same_label)
            for t0, e0, o0, l0 in reversed(self._recent):
                if t - t0 > self.window:
                    break
                if o0 != origin:
                    cands.append((e0, l0 == label))
            if not cands:
                self._recent.append((t, event, origin, label))
                continue
            same = [e for e, s in cands if s]
            targets = same if same else [e for e, _ in cands]
            for other in targets:
                phys, virt = (event, other) if origin == "physical" else (other, event)
                self.votes[phys][virt] += 1
            self._recent.append((t, event, origin, label))

    def best(self, phys_event: str) -> tuple[str | None, int]:
        m = self.votes.get(phys_event)
        if not m:
            return None, 0
        virt, votes = max(m.items(), key=lambda kv: kv[1])
        return virt, votes

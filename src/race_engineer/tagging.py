"""Keyword tagging of rider feedback.

This is a cheap first pass so feedback is searchable even offline. When a
phrase is ambiguous (mentions both ends, or several corner phases) the tag is
left empty rather than guessed; the engineer agent refines tags later.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

ENDS = ("front", "rear")
PHASES = ("braking", "entry", "mid", "exit", "straight")

_END_WORDS = {
    "front": ("front", "fork", "bars", "steering", "headshake"),
    "rear": ("rear", "shock", "back end", "back tire", "back tyre"),
}
_PHASE_WORDS = {
    "braking": ("braking", "brakes", "trail brak", "stoppie"),
    "entry": ("entry", "turn in", "turn-in", "turning in", "tip in", "tip-in"),
    "mid": ("mid corner", "mid-corner", "midcorner", "apex", "middle of the corner", "max lean", "full lean"),
    "exit": ("exit", "on the gas", "throttle", "drive out", "pick up", "picking up"),
    "straight": ("straight", "top speed"),
}
_CORNER = re.compile(r"\b(?:t|turn\s*)(\d{1,2})\b", re.IGNORECASE)


@dataclass
class Tags:
    end: str | None = None
    phase: str | None = None
    corners: list[str] = field(default_factory=list)


def tag(text: str) -> Tags:
    lowered = text.lower()
    corners: list[str] = []
    for number in _CORNER.findall(text):
        corner = f"T{int(number)}"
        if corner not in corners:
            corners.append(corner)
    return Tags(
        end=_single_match(lowered, _END_WORDS),
        phase=_single_match(lowered, _PHASE_WORDS),
        corners=corners,
    )


def _single_match(text: str, table: dict[str, tuple[str, ...]]) -> str | None:
    hits = [name for name, words in table.items() if any(re.search(r"\b" + re.escape(w), text) for w in words)]
    return hits[0] if len(hits) == 1 else None

"""Lap time parsing and formatting. Times are stored as integer milliseconds."""

from __future__ import annotations

import re
from decimal import Decimal

_TIME = re.compile(r"^(?:(\d+):)?(\d+(?:\.\d{1,3})?)$")


def parse_time(text: str) -> int:
    """Parse "1:42.31", "1:42.310" or "102.31" into milliseconds."""
    match = _TIME.match(text.strip())
    if not match:
        raise ValueError(f"Can't read lap time '{text}'. Use m:ss.xxx or ss.xxx.")
    minutes, seconds = match.group(1), Decimal(match.group(2))
    if minutes is not None and seconds >= 60:
        raise ValueError(f"Can't read lap time '{text}': seconds must be under 60.")
    total = seconds + 60 * int(minutes or 0)
    return int(total * 1000)


def format_time(ms: int) -> str:
    minutes, rest = divmod(ms, 60_000)
    seconds, millis = divmod(rest, 1000)
    if minutes:
        return f"{minutes}:{seconds:02d}.{millis:03d}"
    return f"{seconds}.{millis:03d}"


def format_gap(ms: int) -> str:
    sign = "-" if ms < 0 else "+"
    return sign + format_time(abs(ms))

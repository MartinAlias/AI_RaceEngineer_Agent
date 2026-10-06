"""Setup parameters, change parsing and diffs.

A setup is a flat dict keyed by dotted parameter names, e.g.
{"fork.rebound": 12, "tire.front_cold": 2.1}. Clickers are counted in clicks
out from fully closed (fully stiff), the usual paddock convention.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Any


class SetupError(ValueError):
    pass


@dataclass(frozen=True)
class Param:
    label: str
    unit: str
    integer: bool = False


PARAMETERS: dict[str, Param] = {
    "fork.preload": Param("Fork preload", "mm"),
    "fork.compression": Param("Fork compression", "clicks out", integer=True),
    "fork.rebound": Param("Fork rebound", "clicks out", integer=True),
    "fork.spring_rate": Param("Fork spring rate", "N/mm"),
    "fork.oil_level": Param("Fork oil level", "mm"),
    "fork.height": Param("Fork tubes above top clamp", "mm"),
    "shock.preload": Param("Shock preload", "mm"),
    "shock.hs_compression": Param("Shock high-speed compression", "turns out"),
    "shock.ls_compression": Param("Shock low-speed compression", "clicks out", integer=True),
    "shock.rebound": Param("Shock rebound", "clicks out", integer=True),
    "shock.spring_rate": Param("Shock spring rate", "N/mm"),
    "shock.ride_height": Param("Shock length / ride height", "mm"),
    "sag.front_static": Param("Front static sag", "mm"),
    "sag.front_rider": Param("Front rider sag", "mm"),
    "sag.rear_static": Param("Rear static sag", "mm"),
    "sag.rear_rider": Param("Rear rider sag", "mm"),
    "gearing.front_sprocket": Param("Front sprocket", "teeth", integer=True),
    "gearing.rear_sprocket": Param("Rear sprocket", "teeth", integer=True),
    "gearing.chain_links": Param("Chain length", "links", integer=True),
    "tire.front_cold": Param("Front pressure (cold)", "bar"),
    "tire.rear_cold": Param("Rear pressure (cold)", "bar"),
    "tire.front_hot": Param("Front pressure (hot)", "bar"),
    "tire.rear_hot": Param("Rear pressure (hot)", "bar"),
}

# Anything under these prefixes is free-form (e.g. electronics.tc=4, electronics.map=B).
FREE_PREFIXES = ("electronics.",)
GROUPS = ("fork", "shock", "sag", "gearing", "tire", "electronics")

_ASSIGNMENT = re.compile(r"^\s*([A-Za-z0-9_.]+)\s*=\s*(.+?)\s*$")
_KEY_ORDER = {key: i for i, key in enumerate(PARAMETERS)}


@dataclass(frozen=True)
class Assignment:
    key: str
    value: Any
    relative: bool  # True: value is added to the current value


def normalize_key(key: str) -> str:
    key = key.strip().lower()
    if key in PARAMETERS:
        return key
    for prefix in FREE_PREFIXES:
        if key.startswith(prefix) and len(key) > len(prefix):
            return key
    suggestions = difflib.get_close_matches(key, PARAMETERS, n=3)
    hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else " Run `re setup params` for the list."
    raise SetupError(f"Unknown setup parameter '{key}'.{hint}")


def parse_assignment(text: str) -> Assignment:
    """Parse "fork.rebound=12" (absolute) or "fork.rebound=+2" / "=-1" (relative)."""
    match = _ASSIGNMENT.match(text)
    if not match:
        raise SetupError(f"Expected key=value, got '{text}'.")
    key = normalize_key(match.group(1))
    raw = match.group(2)
    relative = raw[0] in "+-"
    try:
        number = float(raw)
    except ValueError:
        if key in PARAMETERS or relative:
            raise SetupError(f"{key} needs a number, got '{raw}'.") from None
        return Assignment(key, raw, relative=False)
    param = PARAMETERS.get(key)
    if param and param.integer and not number.is_integer():
        raise SetupError(f"{key} must be a whole number ({param.unit}), got '{raw}'.")
    return Assignment(key, _tidy(number), relative)


def apply_assignments(params: dict[str, Any], assignments: list[Assignment]) -> tuple[dict[str, Any], list[dict]]:
    """Return the new params and the resulting deltas. The input dict is not modified."""
    new = dict(params)
    for a in assignments:
        if a.relative:
            current = new.get(a.key)
            if not isinstance(current, (int, float)):
                raise SetupError(
                    f"Can't change {a.key} by {format_number(a.value, signed=True)}: it has no current value. "
                    f"Set it first with {a.key}=<value>."
                )
            new[a.key] = _tidy(current + a.value)
        else:
            new[a.key] = a.value
    return new, diff(params, new)


def diff(before: dict[str, Any], after: dict[str, Any]) -> list[dict]:
    keys = sorted(set(before) | set(after), key=sort_key)
    return [
        {"param": k, "from": before.get(k), "to": after.get(k)}
        for k in keys
        if before.get(k) != after.get(k)
    ]


def sort_key(key: str) -> tuple[int, int, str]:
    group = key.split(".", 1)[0]
    group_index = GROUPS.index(group) if group in GROUPS else len(GROUPS)
    return group_index, _KEY_ORDER.get(key, len(_KEY_ORDER)), key


def label(key: str) -> str:
    param = PARAMETERS.get(key)
    return param.label if param else key


def unit(key: str) -> str:
    param = PARAMETERS.get(key)
    return param.unit if param else ""


def format_number(value: Any, signed: bool = False) -> str:
    if isinstance(value, (int, float)):
        return f"{value:+g}" if signed else f"{value:g}"
    return "-" if value is None else str(value)


def format_delta(delta: dict) -> str:
    """e.g. "fork.rebound 10 -> 12 (+2)"."""
    before, after = delta["from"], delta["to"]
    text = f"{delta['param']} {format_number(before)} -> {format_number(after)}"
    if isinstance(before, (int, float)) and isinstance(after, (int, float)):
        text += f" ({format_number(_tidy(after - before), signed=True)})"
    return text


def _tidy(value: float) -> int | float:
    value = round(value, 4)
    return int(value) if float(value).is_integer() else value

"""Setup parameters, change parsing and diffs.

A setup is a flat dict keyed by dotted parameter names, e.g.
{"fork.rebound": 12, "tire.front_cold": 2.1}. Clickers are counted in clicks
out from fully closed (fully stiff), the usual paddock convention.

Pressures are stored in bar but can be entered in psi ("30psi"); a bare number
of 10 or more is read as psi, since no tire runs at 10 bar.
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
    hint: str = ""  # what a higher value means


_CLICKS = "more = softer (less damping)"

PARAMETERS: dict[str, Param] = {
    "fork.preload": Param("Fork preload", "mm", hint="more = more preload, front sits higher"),
    "fork.compression": Param("Fork compression", "clicks out", integer=True, hint=_CLICKS),
    "fork.rebound": Param("Fork rebound", "clicks out", integer=True, hint=_CLICKS),
    "fork.spring_rate": Param("Fork spring rate", "N/mm"),
    "fork.oil_level": Param("Fork oil level", "mm", hint="more = more air-spring ramp-up near full travel"),
    "fork.height": Param("Fork tubes above top clamp", "mm", hint="more = front lower, quicker steering"),
    "shock.preload": Param("Shock preload", "mm", hint="more = more preload, rear sits higher"),
    "shock.hs_compression": Param("Shock high-speed compression", "turns out", hint=_CLICKS),
    "shock.ls_compression": Param("Shock low-speed compression", "clicks out", integer=True, hint=_CLICKS),
    "shock.rebound": Param("Shock rebound", "clicks out", integer=True, hint=_CLICKS),
    "shock.spring_rate": Param("Shock spring rate", "N/mm"),
    "shock.ride_height": Param("Rear ride height adjuster", "mm", hint="more = rear higher"),
    "sag.front_static": Param("Front static sag", "mm"),
    "sag.front_rider": Param("Front rider sag", "mm"),
    "sag.rear_static": Param("Rear static sag", "mm"),
    "sag.rear_rider": Param("Rear rider sag", "mm"),
    "gearing.front_sprocket": Param("Front sprocket", "teeth", integer=True, hint="more = taller gearing"),
    "gearing.rear_sprocket": Param("Rear sprocket", "teeth", integer=True, hint="more = shorter gearing"),
    "gearing.chain_links": Param("Chain length", "links", integer=True, hint="more = longer wheelbase"),
    "tire.front_cold": Param("Front pressure (cold)", "bar"),
    "tire.rear_cold": Param("Rear pressure (cold)", "bar"),
}

# Hot pressures used to be setup parameters; they are now measurements on the session.
MOVED = {
    "tire.front_hot": "Hot pressures are measurements, not settings: record them with `re pressure --front <value>`.",
    "tire.rear_hot": "Hot pressures are measurements, not settings: record them with `re pressure --rear <value>`.",
}

PSI_PER_BAR = 14.5038
PSI_GUESS_FROM = 10  # a bare pressure this high must be psi

# Anything under these prefixes is free-form (e.g. electronics.tc=4, electronics.map=B).
FREE_PREFIXES = ("electronics.",)
GROUPS = ("fork", "shock", "sag", "gearing", "tire", "electronics")

_ASSIGNMENT = re.compile(r"^\s*([A-Za-z0-9_.]+)\s*=\s*(.+?)\s*$")
_PRESSURE = re.compile(r"^([+-]?\d+(?:\.\d+)?)\s*(psi|bar)?$", re.IGNORECASE)
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
    if key in MOVED:
        raise SetupError(MOVED[key])
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
    if unit(key) == "bar":
        value, relative = parse_pressure(raw, allow_relative=True)
        return Assignment(key, value, relative)
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
    return Assignment(key, tidy(number), relative)


def parse_pressure(text: str, *, allow_relative: bool = False) -> tuple[float, bool]:
    """Read a pressure in bar or psi: "2.1", "2.1bar", "30psi", "30" (psi), "-0.1", "-1psi".

    Returns (bar rounded to 0.01, relative)."""
    match = _PRESSURE.match(text.strip())
    if not match:
        raise SetupError(f"Can't read pressure '{text}'. Use bar (2.1) or psi (30psi).")
    number, given_unit = float(match.group(1)), (match.group(2) or "").lower()
    relative = match.group(1)[0] in "+-"
    if relative and not allow_relative:
        raise SetupError(f"Give the pressure itself, not a change: '{text}'.")
    if given_unit == "psi" or (not given_unit and not relative and number >= PSI_GUESS_FROM):
        number /= PSI_PER_BAR
    return round(number, 2), relative


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
            new[a.key] = tidy(current + a.value)
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
        text += f" ({format_number(tidy(after - before), signed=True)})"
    return text


def tidy(value: float) -> int | float:
    value = round(value, 4)
    return int(value) if float(value).is_integer() else value

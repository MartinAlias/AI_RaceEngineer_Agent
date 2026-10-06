"""Allowed value ranges for setup parameters.

Defaults come from knowledge/ranges.yaml; a bike's own ranges (stored in the
param_range table, see repo.set_param_range) replace them per parameter.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import yaml

from . import setup_diff
from .guide import data_file

Range = tuple[float, float]


@lru_cache
def default_ranges() -> dict[str, Range]:
    raw = yaml.safe_load(data_file("ranges.yaml")) or {}
    ranges = {}
    for key, (low, high) in raw.items():
        ranges[setup_diff.normalize_key(key)] = (float(low), float(high))
    return ranges


def violations(ranges: dict[str, Range], params: dict[str, Any], keys: list[str] | None = None) -> list[str]:
    """Messages for values outside their range. Only `keys` are checked when given."""
    problems = []
    for key in keys if keys is not None else params:
        value = params.get(key)
        if not isinstance(value, (int, float)) or key not in ranges:
            continue
        low, high = ranges[key]
        if not low <= value <= high:
            problems.append(
                f"{key} = {setup_diff.format_number(value)} is outside the allowed range "
                f"{setup_diff.format_number(low)} to {setup_diff.format_number(high)} {setup_diff.unit(key)}".rstrip()
            )
    return problems

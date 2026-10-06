"""Symptom guide: generic causes and adjustments for common rider complaints.

The data lives in knowledge/symptoms.yaml so it can be read and extended without
touching code. It is validated on load: every adjustment must name a real setup
parameter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Any

import yaml

from . import setup_diff, tagging

LIKELIHOODS = ("high", "medium", "low")


class GuideError(ValueError):
    pass


@dataclass(frozen=True)
class Adjustment:
    param: str
    step: float
    effect: str


@dataclass(frozen=True)
class Cause:
    description: str
    likelihood: str
    checks: tuple[str, ...]
    adjustments: tuple[Adjustment, ...]


@dataclass(frozen=True)
class Symptom:
    id: str
    name: str
    end: str | None
    phases: tuple[str, ...]
    keywords: tuple[str, ...]
    follow_up: tuple[str, ...]
    causes: tuple[Cause, ...]


@dataclass(frozen=True)
class Match:
    symptom: Symptom
    score: float
    keywords: tuple[str, ...]  # keywords found in the text


def data_file(name: str) -> str:
    return (resources.files("race_engineer") / "knowledge" / name).read_text(encoding="utf-8")


@lru_cache
def load_symptoms() -> dict[str, Symptom]:
    return parse_symptoms(yaml.safe_load(data_file("symptoms.yaml")))


def parse_symptoms(raw: list[dict[str, Any]]) -> dict[str, Symptom]:
    symptoms: dict[str, Symptom] = {}
    for entry in raw:
        symptom_id = entry["id"]
        try:
            symptom = _parse_symptom(entry)
        except (KeyError, TypeError, ValueError) as error:
            raise GuideError(f"Symptom '{symptom_id}': {error}") from None
        if symptom_id in symptoms:
            raise GuideError(f"Duplicate symptom id '{symptom_id}'.")
        symptoms[symptom_id] = symptom
    return symptoms


def _parse_symptom(entry: dict[str, Any]) -> Symptom:
    end = entry.get("end")
    if end not in (None, *tagging.ENDS):
        raise GuideError(f"end must be one of {tagging.ENDS} or null, got '{end}'")
    phases = tuple(entry.get("phases") or ())
    if unknown := set(phases) - set(tagging.PHASES):
        raise GuideError(f"unknown phases {sorted(unknown)}")
    causes = []
    for cause in entry.get("causes") or ():
        if cause.get("likelihood") not in LIKELIHOODS:
            raise GuideError(f"likelihood must be one of {LIKELIHOODS}")
        adjustments = []
        for a in cause.get("adjustments") or ():
            param = setup_diff.normalize_key(a["param"])
            spec = setup_diff.PARAMETERS.get(param)
            if spec and spec.integer and not float(a["step"]).is_integer():
                raise GuideError(f"{param} step must be a whole number")
            adjustments.append(Adjustment(param, a["step"], a.get("effect", "")))
        causes.append(Cause(cause["description"], cause["likelihood"], tuple(cause.get("checks") or ()),
                            tuple(adjustments)))
    return Symptom(
        id=entry["id"],
        name=entry["name"],
        end=end,
        phases=phases,
        keywords=tuple(k.lower() for k in entry.get("keywords") or ()),
        follow_up=tuple(entry.get("follow_up") or ()),
        causes=tuple(causes),
    )


def get_symptom(symptom_id: str) -> Symptom:
    try:
        return load_symptoms()[symptom_id]
    except KeyError:
        raise GuideError(f"Unknown symptom '{symptom_id}'. See `re guide`.") from None


def match(text: str, *, end: str | None = None, phase: str | None = None, limit: int = 3) -> list[Match]:
    """Rank guide symptoms against the rider's words.

    End and phase default to what the text says; when the text mentions several
    phases ("on the brakes at turn in") symptoms covering more of them score higher.
    """
    lowered = text.lower()
    end = end or tagging.tag(text).end
    phases = [phase] if phase else tagging.phases(text)
    results = []
    for symptom in load_symptoms().values():
        hits = tuple(k for k in symptom.keywords if re.search(r"\b" + re.escape(k), lowered))
        if not hits or (end and symptom.end and symptom.end != end):
            continue
        score = 2.0 * len(hits)
        if end and symptom.end == end:
            score += 1.5
        if phases:
            overlap = len(set(phases) & set(symptom.phases))
            score += 2.0 * overlap if overlap else -1.0
        results.append(Match(symptom, score, hits))
    results.sort(key=lambda m: -m.score)  # stable: ties keep file order
    return results[:limit]


def best_symptom(text: str, *, end: str | None = None, phase: str | None = None) -> str | None:
    """The single best matching symptom id, or None when nothing (or more than one) fits clearly."""
    matches = match(text, end=end, phase=phase, limit=2)
    if not matches or (len(matches) == 2 and matches[0].score == matches[1].score):
        return None
    return matches[0].symptom.id

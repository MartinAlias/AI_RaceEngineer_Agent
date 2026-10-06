"""Offline advice for a piece of rider feedback.

Combines the symptom guide, the bike's current setup and allowed ranges, the
bike's own track record for each adjustment, and similar past notes. Needs no
network: it is what the rider gets when the AI engineer can't be reached, and the
engineer agent uses the same building blocks as tools.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from . import guide, history, ranges, repo, setup_diff
from .models import Feedback, Session


@dataclass
class Suggestion:
    cause: guide.Cause
    adjustment: guide.Adjustment
    current: Any
    proposed: Any
    problem: str | None  # why it can't be applied as-is (not set, out of range)
    track_record: history.Effect | None  # what happened when this was tried before on this bike


@dataclass
class SymptomAdvice:
    match: guide.Match
    suggestions: list[Suggestion]
    effects: list[history.Effect]


@dataclass
class Advice:
    feedback: Feedback
    session: Session
    symptoms: list[SymptomAdvice]  # worked through in full
    alternatives: list[guide.Match]  # other plausible symptoms, by name only
    similar: list[history.HistoryHit]


def advise(conn: sqlite3.Connection, feedback_id: int, *, max_symptoms: int = 2) -> Advice:
    """Advice for one piece of feedback. A tagged symptom is worked through alone;
    untagged feedback gets up to max_symptoms candidates."""
    feedback = repo.get_feedback(conn, feedback_id)
    session = repo.get_session(conn, feedback.session_id)
    bike = repo.get_bike(conn, session.bike_id)
    params = repo.get_setup(conn, bike.current_setup_id).params
    limits = repo.get_param_ranges(conn, bike.id)

    matches = guide.match(feedback.text, end=feedback.end, phase=feedback.phase, limit=4)
    if feedback.symptom in guide.load_symptoms():
        tagged = guide.get_symptom(feedback.symptom)
        others = [m for m in matches if m.symptom.id != tagged.id]
        matches, alternatives = [guide.Match(tagged, float("inf"), ())], others
    else:
        matches, alternatives = matches[:max_symptoms], matches[max_symptoms:]

    symptoms = []
    for m in matches:
        effects = history.learned_effects(conn, bike.id, symptom=m.symptom.id)
        suggestions = [
            _suggest(cause, adjustment, params, limits, effects)
            for cause in m.symptom.causes
            for adjustment in cause.adjustments
        ]
        symptoms.append(SymptomAdvice(m, suggestions, effects))

    similar = history.search(conn, bike.id, f"{feedback.text} {feedback.symptom or ''}",
                             exclude_session_id=session.id, limit=5)
    return Advice(feedback, session, symptoms, alternatives, similar)


def _suggest(
    cause: guide.Cause,
    adjustment: guide.Adjustment,
    params: dict[str, Any],
    limits: dict[str, ranges.Range],
    effects: list[history.Effect],
) -> Suggestion:
    current = params.get(adjustment.param)
    record = history.effect_for(effects, adjustment.param, adjustment.step)
    if not isinstance(current, (int, float)):
        return Suggestion(cause, adjustment, current, None, "not in your current setup", record)
    proposed = setup_diff.tidy(current + adjustment.step)
    problems = ranges.violations(limits, {adjustment.param: proposed})
    return Suggestion(cause, adjustment, current, proposed, problems[0] if problems else None, record)

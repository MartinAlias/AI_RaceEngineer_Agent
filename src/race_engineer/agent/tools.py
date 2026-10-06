"""Tools the engineer can call.

Every tool reads or writes the local database through repo / history / guide, so
whatever the engineer learns or decides ends up as ordinary records. Tools that
change data (propose_changes, record_outcome) ask the rider first.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field, ValidationError

from .. import guide, history, ranges, repo, setup_diff, timing


class RiderIO(Protocol):
    """How tools and the engineer talk to the rider (the terminal, or a script in tests)."""

    def say(self, text: str) -> None: ...
    def notice(self, text: str) -> None: ...
    def ask(self, prompt: str) -> str | None: ...  # None: the rider wants to stop
    def confirm(self, prompt: str) -> bool: ...
    def show_proposal(self, title: str, lines: list[str]) -> None: ...


class ToolError(Exception):
    """A problem the model should see and correct (bad input, out of range...)."""


# --- schemas -------------------------------------------------------------

_SYMPTOM_HELP = "Symptom id from the guide, e.g. front_push_mid (see lookup_symptom_guide)."

TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_session_context",
        "description": (
            "Everything about the current situation: rider profile, bike, current setup with units, "
            "direction hints and allowed ranges, the track and corners, conditions, tires, lap times, "
            "feedback so far this session, earlier sessions today at this track, and changes still "
            "waiting for a result. Call this first."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_setup_history",
        "description": "Recent setup changes on this bike, newest first: what changed, why, and how it worked out.",
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}},
            "additionalProperties": False,
        },
    },
    {
        "name": "search_history",
        "description": (
            "Full-text search over this bike's past rider feedback, change reasoning and outcome notes "
            "(excluding the current session). Optionally restrict to the same track, a track temperature "
            "(within 8 °C) or a tire."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Words to look for, e.g. 'front slide mid corner'."},
                "same_track": {"type": "boolean", "default": False},
                "track_temp": {"type": "number", "description": "Track temperature in °C."},
                "tire": {"type": "string", "description": "Tire name or compound substring."},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_learned_effects",
        "description": (
            "What setup changes have actually done on this bike: recorded results grouped by symptom, "
            "parameter and direction (tries, better/same/worse, lap time gain)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"symptom": {"type": "string", "description": _SYMPTOM_HELP + " Omit for all."}},
            "additionalProperties": False,
        },
    },
    {
        "name": "lookup_symptom_guide",
        "description": (
            "The generic symptom guide: likely causes, checks, follow-up questions and typical "
            "adjustments. Give a symptom id, or a description to find the best matching symptoms. "
            "Give neither to list all symptom ids."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symptom_id": {"type": "string"},
                "description": {"type": "string", "description": "Rider's words, e.g. 'front tucks at turn in'."},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "update_feedback_tags",
        "description": (
            "Correct or complete the tags on a piece of rider feedback once the follow-up questions "
            "have made the complaint clear. Fields left out are kept."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "feedback_id": {"type": "integer"},
                "symptom": {"type": "string", "description": _SYMPTOM_HELP},
                "end": {"type": "string", "enum": ["front", "rear"]},
                "phase": {"type": "string", "enum": ["braking", "entry", "mid", "exit", "straight"]},
                "corners": {"type": "array", "items": {"type": "string"}, "description": "e.g. ['T3', 'T7']"},
            },
            "required": ["feedback_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "propose_changes",
        "description": (
            "Propose one or two setup changes to the rider. Each change sets an absolute value or a "
            "relative step for one parameter. The proposal is checked against the bike's allowed "
            "ranges, shown to the rider, and applied only if they accept. Returns whether it was "
            "applied, with the change id."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "changes": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 2,
                    "items": {
                        "type": "object",
                        "properties": {
                            "param": {"type": "string", "description": "e.g. fork.rebound"},
                            "step": {"type": "number", "description": "Relative change, e.g. -2."},
                            "value": {
                                "type": ["number", "string"],
                                "description": "Absolute value. Use instead of step.",
                            },
                        },
                        "required": ["param"],
                        "additionalProperties": False,
                    },
                },
                "symptom": {"type": "string", "description": _SYMPTOM_HELP},
                "hypothesis": {"type": "string", "description": "One line: the cause this targets."},
                "reasoning": {"type": "string", "description": "Why this cause and this change, briefly."},
                "watch_for": {"type": "string", "description": "What the rider should feel / side effects."},
            },
            "required": ["changes", "hypothesis", "reasoning", "watch_for"],
            "additionalProperties": False,
        },
    },
    {
        "name": "record_outcome",
        "description": (
            "Record how a setup change worked out, after asking the rider. The rider confirms before "
            "it is saved. The lap time comparison is computed automatically."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "change_id": {"type": "integer"},
                "verdict": {"type": "string", "enum": ["better", "same", "worse"]},
                "symptom_resolved": {"type": "boolean"},
                "notes": {"type": "string", "description": "What the rider felt, in their words."},
            },
            "required": ["change_id", "verdict"],
            "additionalProperties": False,
        },
    },
]


# --- input models (tool inputs are validated before anything runs) --------

class _HistoryLimit(BaseModel):
    limit: int = Field(10, ge=1, le=50)


class _Search(BaseModel):
    query: str
    same_track: bool = False
    track_temp: float | None = None
    tire: str | None = None


class _Effects(BaseModel):
    symptom: str | None = None


class _Guide(BaseModel):
    symptom_id: str | None = None
    description: str | None = None


class _Tags(BaseModel):
    feedback_id: int
    symptom: str | None = None
    end: Literal["front", "rear"] | None = None
    phase: Literal["braking", "entry", "mid", "exit", "straight"] | None = None
    corners: list[str] | None = None


class _ChangeSpec(BaseModel):
    param: str
    step: float | None = None
    value: float | str | None = None


class _Proposal(BaseModel):
    changes: list[_ChangeSpec] = Field(min_length=1, max_length=2)
    symptom: str | None = None
    hypothesis: str
    reasoning: str
    watch_for: str


class _Outcome(BaseModel):
    change_id: int
    verdict: Literal["better", "same", "worse"]
    symptom_resolved: bool | None = None
    notes: str | None = None


# --- implementation ------------------------------------------------------

class Toolbox:
    def __init__(self, conn: sqlite3.Connection, io: RiderIO, *, bike_id: int, session_id: int | None):
        self.conn = conn
        self.io = io
        self.bike_id = bike_id
        self.session_id = session_id
        self._handlers = {
            "get_session_context": (None, self.get_session_context),
            "get_setup_history": (_HistoryLimit, self.get_setup_history),
            "search_history": (_Search, self.search_history),
            "get_learned_effects": (_Effects, self.get_learned_effects),
            "lookup_symptom_guide": (_Guide, self.lookup_symptom_guide),
            "update_feedback_tags": (_Tags, self.update_feedback_tags),
            "propose_changes": (_Proposal, self.propose_changes),
            "record_outcome": (_Outcome, self.record_outcome),
        }

    def run(self, name: str, raw_input: Any) -> tuple[str, bool]:
        """Run a tool. Returns (JSON result text, is_error)."""
        if name not in self._handlers:
            return f"Unknown tool '{name}'.", True
        model, handler = self._handlers[name]
        try:
            result = handler() if model is None else handler(model.model_validate(raw_input or {}))
        except ValidationError as error:
            return f"Invalid input for {name}: {error}", True
        except (ToolError, repo.RepoError, ValueError) as error:
            return str(error), True
        return json.dumps(result, ensure_ascii=False, default=str), False

    # read tools

    def get_session_context(self) -> dict[str, Any]:
        bike = repo.get_bike(self.conn, self.bike_id)
        setup = repo.get_setup(self.conn, bike.current_setup_id)
        limits = repo.get_param_ranges(self.conn, bike.id)
        context: dict[str, Any] = {
            "rider_profile": repo.get_state(self.conn, "rider_profile"),
            "bike": {"name": bike.name, "description": bike.description, "notes": bike.notes},
            "current_setup": {
                "setup_id": setup.id,
                "values": [_param_info(key, setup.params[key], limits)
                           for key in sorted(setup.params, key=setup_diff.sort_key)],
                "not_recorded": [k for k in setup_diff.PARAMETERS if k not in setup.params],
            },
            "changes_without_result": [self._change_info(c) for c in repo.changes_without_outcome(self.conn, bike.id)],
        }
        if self.session_id is None:
            context["session"] = None
            return context

        session = repo.get_session(self.conn, self.session_id)
        track = repo.get_track(self.conn, session.track_id)
        laps = repo.list_laps(self.conn, session.id)
        best = repo.best_lap(self.conn, session.id)
        context["session"] = {
            "session_id": session.id,
            "date": session.date,
            "type": session.type,
            "track": track.name,
            "track_notes": track.notes,
            "corners": [{"corner": f"T{c.number}", "name": c.name, "type": c.type}
                        for c in repo.list_corners(self.conn, track.id)],
            "air_temp_c": session.air_temp,
            "track_temp_c": session.track_temp,
            "weather": session.weather,
            "tires": {"front": session.tire_front, "rear": session.tire_rear,
                      "front_laps_before": session.tire_front_laps, "rear_laps_before": session.tire_rear_laps},
            "ran_setup_id": session.setup_id,
            "laps": [{"lap": lap.lap_number, "time": timing.format_time(lap.time_ms), "valid": lap.valid,
                      "sectors": [timing.format_time(s) for s in lap.sectors_ms]} for lap in laps],
            "best_lap": timing.format_time(best.time_ms) if best else None,
            "feedback": [_feedback_info(f) for f in repo.list_feedback(self.conn, session.id)],
            "notes": session.notes,
        }
        earlier = [s for s in repo.list_sessions(self.conn, bike_id=bike.id, track_id=track.id, limit=10)
                   if s.date == session.date and s.id != session.id]
        context["earlier_sessions_today"] = [
            {"session_id": s.id, "type": s.type, "track_temp_c": s.track_temp, "ran_setup_id": s.setup_id,
             "best_lap": _best(self.conn, s.id),
             "feedback": [f.text for f in repo.list_feedback(self.conn, s.id)]}
            for s in earlier
        ]
        return context

    def get_setup_history(self, args: _HistoryLimit) -> list[dict[str, Any]]:
        changes = repo.list_changes(self.conn, bike_id=self.bike_id)
        return [self._change_info(c, with_outcomes=True) for c in reversed(changes[-args.limit:])]

    def search_history(self, args: _Search) -> list[dict[str, Any]]:
        track_id = None
        if args.same_track and self.session_id is not None:
            track_id = repo.get_session(self.conn, self.session_id).track_id
        hits = history.search(self.conn, self.bike_id, args.query, track_id=track_id, tire=args.tire,
                              track_temp=args.track_temp, exclude_session_id=self.session_id, limit=10)
        return [{"kind": h.kind, "date": h.date, "track": h.track, "track_temp_c": h.track_temp,
                 "tires": h.tires, "text": h.snippet.replace("«", "").replace("»", ""), "change": h.detail}
                for h in hits]

    def get_learned_effects(self, args: _Effects) -> list[dict[str, Any]]:
        effects = history.learned_effects(self.conn, self.bike_id, symptom=args.symptom)
        return [{"symptom": e.symptom, "param": e.param, "typical_step": e.typical_step, "tries": e.tries,
                 "better": e.better, "same": e.same, "worse": e.worse, "resolved": e.resolved,
                 "combined_with_other_changes": e.combined,
                 "avg_best_lap_change": timing.format_gap(e.avg_lap_delta_ms) if e.avg_lap_delta_ms is not None else None,
                 "examples": e.examples[-5:]}
                for e in effects]

    def lookup_symptom_guide(self, args: _Guide) -> Any:
        if args.symptom_id:
            return _symptom_info(guide.get_symptom(args.symptom_id))
        if args.description:
            return [_symptom_info(m.symptom) for m in guide.match(args.description, limit=3)]
        return [{"id": s.id, "name": s.name} for s in guide.load_symptoms().values()]

    # write tools

    def update_feedback_tags(self, args: _Tags) -> dict[str, Any]:
        if args.symptom:
            guide.get_symptom(args.symptom)
        feedback = repo.update_feedback(self.conn, args.feedback_id, end=args.end, phase=args.phase,
                                        corners=args.corners, symptom=args.symptom)
        return _feedback_info(feedback)

    def propose_changes(self, args: _Proposal) -> dict[str, Any]:
        if args.symptom:
            guide.get_symptom(args.symptom)
        assignments = [_assignment(spec) for spec in args.changes]
        if len({a.key for a in assignments}) != len(assignments):
            raise ToolError("Each parameter can appear only once in a proposal.")

        # Check everything before bothering the rider.
        bike = repo.get_bike(self.conn, self.bike_id)
        before = repo.get_setup(self.conn, bike.current_setup_id)
        params, deltas = setup_diff.apply_assignments(before.params, assignments)
        if not deltas:
            raise ToolError("That proposal doesn't change anything: the values match the current setup.")
        problems = ranges.violations(repo.get_param_ranges(self.conn, bike.id), params, [d["param"] for d in deltas])
        if problems:
            raise ToolError("Not allowed: " + "; ".join(problems) + ". Propose something inside the range.")

        lines = [setup_diff.format_delta(d) for d in deltas]
        lines += ["", f"Targets: {args.hypothesis}", f"Feel for: {args.watch_for}"]
        self.io.show_proposal("Proposed change", lines)
        if not self.io.confirm("Apply this change?"):
            comment = self.io.ask("Why not? (optional, Enter to skip)") or ""
            return {"status": "declined", "rider_comment": comment.strip() or None}

        change = repo.change_setup(self.conn, bike.id, assignments, proposed_by="agent", hypothesis=args.hypothesis,
                                   reasoning=args.reasoning, watch_for=args.watch_for, symptom=args.symptom,
                                   session_id=self.session_id)
        self.io.notice(f"Applied: setup #{change.after_setup_id} (change #{change.id}).")
        return {"status": "applied", "change_id": change.id, "new_setup_id": change.after_setup_id,
                "deltas": change.deltas}

    def record_outcome(self, args: _Outcome) -> dict[str, Any]:
        change = repo.get_change(self.conn, args.change_id)
        if change.bike_id != self.bike_id:
            raise ToolError(f"Change #{change.id} belongs to a different bike.")
        what = "; ".join(setup_diff.format_delta(d) for d in change.deltas)
        resolved = {True: ", complaint resolved", False: ", complaint not resolved", None: ""}[args.symptom_resolved]
        if not self.io.confirm(f"Record change #{change.id} ({what}) as {args.verdict}{resolved}?"):
            return {"status": "not recorded", "reason": "rider did not confirm"}
        test_session = self.session_id if self.session_id != change.session_id else None
        delta = repo.lap_delta(self.conn, change, test_session)
        outcome = repo.add_outcome(self.conn, change.id, args.verdict, session_id=test_session, lap_delta_ms=delta,
                                   symptom_resolved=args.symptom_resolved, notes=args.notes)
        return {"status": "recorded", "outcome_id": outcome.id,
                "best_lap_change": timing.format_gap(delta) if delta is not None else None}

    def _change_info(self, change, with_outcomes: bool = False) -> dict[str, Any]:
        info: dict[str, Any] = {
            "change_id": change.id,
            "made_after_session_id": change.session_id,
            "created_at": change.created_at,
            "changes": [setup_diff.format_delta(d) for d in change.deltas],
            "symptom": change.symptom,
            "hypothesis": change.hypothesis,
            "proposed_by": change.proposed_by,
        }
        if with_outcomes:
            info["results"] = [
                {"verdict": o.verdict, "symptom_resolved": o.symptom_resolved, "notes": o.notes,
                 "best_lap_change": timing.format_gap(o.lap_delta_ms) if o.lap_delta_ms is not None else None}
                for o in repo.list_outcomes(self.conn, change.id)
            ]
        return info


def _assignment(spec: _ChangeSpec) -> setup_diff.Assignment:
    if (spec.step is None) == (spec.value is None):
        raise ToolError(f"{spec.param}: give either step or value, not both or neither.")
    if spec.step is not None:
        if spec.step == 0:
            raise ToolError(f"{spec.param}: a step of 0 changes nothing.")
        return setup_diff.parse_assignment(f"{spec.param}={spec.step:+g}")
    if isinstance(spec.value, str):
        return setup_diff.parse_assignment(f"{spec.param}={spec.value}")
    if spec.value < 0:  # "=-2" would read as relative; absolute negatives are set directly
        key = setup_diff.normalize_key(spec.param)
        return setup_diff.Assignment(key, setup_diff.tidy(spec.value), relative=False)
    return setup_diff.parse_assignment(f"{spec.param}={spec.value:g}")


def _param_info(key: str, value: Any, limits: dict[str, ranges.Range]) -> dict[str, Any]:
    info: dict[str, Any] = {"param": key, "label": setup_diff.label(key), "value": value, "unit": setup_diff.unit(key)}
    spec = setup_diff.PARAMETERS.get(key)
    if spec and spec.hint:
        info["higher_means"] = spec.hint
    if key in limits:
        info["allowed_range"] = list(limits[key])
    return info


def _feedback_info(f) -> dict[str, Any]:
    return {"feedback_id": f.id, "text": f.text, "end": f.end, "phase": f.phase, "corners": f.corners,
            "symptom": f.symptom, "severity": f.severity}


def _symptom_info(s: guide.Symptom) -> dict[str, Any]:
    return {
        "id": s.id,
        "name": s.name,
        "end": s.end,
        "phases": list(s.phases),
        "follow_up_questions": list(s.follow_up),
        "causes": [
            {"cause": c.description, "likelihood": c.likelihood, "checks": list(c.checks),
             "adjustments": [{"param": a.param, "step": a.step, "effect": a.effect} for a in c.adjustments]}
            for c in s.causes
        ],
    }


def _best(conn: sqlite3.Connection, session_id: int) -> str | None:
    best = repo.best_lap(conn, session_id)
    return timing.format_time(best.time_ms) if best else None

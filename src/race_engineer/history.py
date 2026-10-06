"""What this bike's history says: similar past notes, and which changes actually worked.

- search(): full-text search over feedback, change reasoning and outcome notes,
  filtered by track, tire and track temperature.
- learned_effects(): every recorded outcome, grouped by (symptom, parameter,
  direction), so "fork rebound more damping for front_push_mid: 3 of 4 better" is
  available to the rider and the engineer.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field

from . import setup_diff

_STOPWORDS = {
    "a", "an", "and", "are", "at", "be", "bit", "but", "felt", "feel", "feels", "for", "from", "i", "in", "is",
    "it", "its", "like", "little", "me", "my", "of", "on", "or", "really", "so", "the", "then", "to", "too",
    "very", "was", "when", "with",
}


@dataclass
class HistoryHit:
    kind: str  # feedback | change | outcome
    ref_id: int
    session_id: int | None
    date: str | None
    track: str | None
    track_temp: float | None
    tires: str
    snippet: str
    detail: str  # what was changed / what came of it


@dataclass
class Effect:
    symptom: str | None
    param: str
    direction: int  # +1 or -1: numeric direction of the change
    better: int = 0
    same: int = 0
    worse: int = 0
    resolved: int = 0
    combined: int = 0  # tries where other parameters changed at the same time
    steps: list[float] = field(default_factory=list)
    lap_deltas_ms: list[int] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)

    @property
    def tries(self) -> int:
        return self.better + self.same + self.worse

    @property
    def score(self) -> float:
        """-1 (always worse) to +1 (always better)."""
        return (self.better - self.worse) / self.tries if self.tries else 0.0

    @property
    def typical_step(self) -> float:
        return sorted(self.steps)[len(self.steps) // 2] if self.steps else 0.0

    @property
    def avg_lap_delta_ms(self) -> int | None:
        return round(sum(self.lap_deltas_ms) / len(self.lap_deltas_ms)) if self.lap_deltas_ms else None

    def summary(self) -> str:
        step = setup_diff.format_number(self.typical_step, signed=True)
        text = f"{self.param} {step}: tried {self.tries}x, {self.better} better, {self.same} same, {self.worse} worse"
        if self.combined:
            text += f" ({self.combined} combined with other changes)"
        return text


def fts_query(text: str) -> str | None:
    """Turn free text into a forgiving FTS5 query: any significant word, matched by prefix."""
    terms: list[str] = []
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        if word in _STOPWORDS or len(word) < 2:
            continue
        stem = word if len(word) <= 4 else word[: max(4, len(word) - 3)]
        term = f'"{stem}"*'
        if term not in terms:
            terms.append(term)
    return " OR ".join(terms) or None


def search(
    conn: sqlite3.Connection,
    bike_id: int,
    query: str,
    *,
    track_id: int | None = None,
    tire: str | None = None,
    track_temp: float | None = None,
    temp_window: float = 8,
    exclude_session_id: int | None = None,
    limit: int = 10,
) -> list[HistoryHit]:
    match = fts_query(query)
    if match is None:
        return []
    rows = conn.execute(
        """SELECT n.kind, n.ref_id, n.session_id,
                  snippet(notes_fts, 0, '«', '»', '…', 16) AS snippet,
                  s.date, s.track_temp, s.tire_front, s.tire_rear, t.name AS track
           FROM notes_fts n
           LEFT JOIN session s ON s.id = n.session_id
           LEFT JOIN track t ON t.id = s.track_id
           WHERE notes_fts MATCH :match AND n.bike_id = :bike
             AND (:exclude IS NULL OR n.session_id IS NULL OR n.session_id != :exclude)
             AND (:track IS NULL OR s.track_id = :track)
             AND (:tire IS NULL OR s.tire_front LIKE :tire OR s.tire_rear LIKE :tire)
             AND (:temp IS NULL OR abs(s.track_temp - :temp) <= :window)
           ORDER BY bm25(notes_fts)
           LIMIT :limit""",
        {"match": match, "bike": bike_id, "exclude": exclude_session_id, "track": track_id,
         "tire": f"%{tire}%" if tire else None, "temp": track_temp, "window": temp_window, "limit": limit},
    ).fetchall()
    return [
        HistoryHit(
            kind=r["kind"], ref_id=r["ref_id"], session_id=r["session_id"], date=r["date"], track=r["track"],
            track_temp=r["track_temp"], tires=" / ".join(t for t in (r["tire_front"], r["tire_rear"]) if t),
            snippet=r["snippet"], detail=_detail(conn, r["kind"], r["ref_id"]),
        )
        for r in rows
    ]


def _detail(conn: sqlite3.Connection, kind: str, ref_id: int) -> str:
    if kind == "change":
        change_id = ref_id
    elif kind == "outcome":
        change_id = conn.execute("SELECT change_id FROM outcome WHERE id = ?", (ref_id,)).fetchone()[0]
    else:
        return ""
    deltas = json.loads(conn.execute("SELECT deltas FROM change WHERE id = ?", (change_id,)).fetchone()[0])
    verdicts = [r[0] for r in conn.execute("SELECT verdict FROM outcome WHERE change_id = ?", (change_id,))]
    text = f"change #{change_id}: " + "; ".join(setup_diff.format_delta(d) for d in deltas)
    return text + (f" -> {', '.join(verdicts)}" if verdicts else " -> no result recorded")


def learned_effects(
    conn: sqlite3.Connection,
    bike_id: int,
    *,
    symptom: str | None = None,
    track_id: int | None = None,
    track_temp: float | None = None,
    temp_window: float = 8,
) -> list[Effect]:
    """Aggregate recorded outcomes per (symptom, parameter, direction), most-tried first."""
    rows = conn.execute(
        """SELECT c.id AS change_id, c.symptom, c.deltas, o.verdict, o.symptom_resolved, o.lap_delta_ms,
                  s.date, s.track_temp, t.name AS track
           FROM outcome o
           JOIN change c ON c.id = o.change_id
           LEFT JOIN session s ON s.id = coalesce(o.session_id, c.session_id)
           LEFT JOIN track t ON t.id = s.track_id
           WHERE c.bike_id = :bike
             AND (:symptom IS NULL OR c.symptom = :symptom)
             AND (:track IS NULL OR s.track_id = :track)
             AND (:temp IS NULL OR abs(s.track_temp - :temp) <= :window)
           ORDER BY o.id""",
        {"bike": bike_id, "symptom": symptom, "track": track_id, "temp": track_temp, "window": temp_window},
    ).fetchall()

    effects: dict[tuple[str | None, str, int], Effect] = {}
    for r in rows:
        numeric = [d for d in json.loads(r["deltas"])
                   if isinstance(d["from"], (int, float)) and isinstance(d["to"], (int, float))]
        for d in numeric:
            step = setup_diff.tidy(d["to"] - d["from"])
            direction = 1 if step > 0 else -1
            effect = effects.setdefault((r["symptom"], d["param"], direction),
                                        Effect(r["symptom"], d["param"], direction))
            setattr(effect, r["verdict"], getattr(effect, r["verdict"]) + 1)
            effect.resolved += bool(r["symptom_resolved"])
            effect.combined += len(numeric) > 1
            effect.steps.append(step)
            if r["lap_delta_ms"] is not None:
                effect.lap_deltas_ms.append(r["lap_delta_ms"])
            where = " ".join(str(x) for x in (r["date"], r["track"]) if x)
            effect.examples.append(f"{where}: {setup_diff.format_delta(d)} -> {r['verdict']}")
    return sorted(effects.values(), key=lambda e: (-e.tries, -e.score))


def effect_for(effects: list[Effect], param: str, step: float) -> Effect | None:
    """The recorded effect of moving `param` in the direction of `step`, if any."""
    direction = 1 if step > 0 else -1
    return next((e for e in effects if e.param == param and e.direction == direction), None)

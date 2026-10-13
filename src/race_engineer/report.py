"""Weekend report: sessions, laps, feedback, setup changes and their results.

build() gathers everything through repo; to_markdown() and to_csv() render it.
A "weekend" defaults to the bike's latest session plus the sessions at the same
track in the WEEKEND_DAYS days before it.
"""

from __future__ import annotations

import csv
import io
import sqlite3
from dataclasses import dataclass, field
from datetime import date as Date, timedelta

from . import repo, setup_diff, timing
from .models import Bike, Change, Feedback, Lap, Outcome, Session, Setup, TireTarget, Track

WEEKEND_DAYS = 3


@dataclass
class ChangeReport:
    change: Change
    outcomes: list[Outcome]
    after_session: Session | None


@dataclass
class SessionReport:
    session: Session
    track: Track
    setup: Setup
    laps: list[Lap]
    best: Lap | None
    feedback: list[Feedback]
    targets: dict[str, TireTarget | None]
    changes_after: list[ChangeReport] = field(default_factory=list)


@dataclass
class WeekendReport:
    bike: Bike
    rider_profile: str | None
    date_from: str
    date_to: str
    sessions: list[SessionReport]

    @property
    def tracks(self) -> list[str]:
        return list(dict.fromkeys(s.track.name for s in self.sessions))

    @property
    def changes(self) -> list[ChangeReport]:
        return [c for s in self.sessions for c in s.changes_after]


def build(
    conn: sqlite3.Connection,
    bike_id: int,
    *,
    track_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> WeekendReport:
    """Sessions from date_from to date_to. Without both dates, the weekend is the latest session
    (up to date_to, if given) and the sessions at its track in the WEEKEND_DAYS days before it."""
    bike = repo.get_bike(conn, bike_id)
    for value in (date_from, date_to):
        if value is not None:
            _check_date(value)
    if date_from is None or date_to is None:
        earlier = repo.sessions_between(conn, bike.id, "0000-01-01", date_to or "9999-12-31", track_id=track_id)
        if not earlier:
            raise repo.NotFound(f"No sessions logged for {bike.name}" + (f" up to {date_to}." if date_to else " yet."))
        latest = earlier[-1]
        date_to, track_id = latest.date, track_id or latest.track_id
        date_from = date_from or (Date.fromisoformat(date_to) - timedelta(days=WEEKEND_DAYS)).isoformat()
    if date_from > date_to:
        raise repo.RepoError(f"--from {date_from} is after --to {date_to}.")

    sessions = []
    for s in repo.sessions_between(conn, bike.id, date_from, date_to, track_id=track_id):
        changes = [ChangeReport(c, repo.list_outcomes(conn, c.id), s) for c in repo.list_changes(conn, session_id=s.id)]
        sessions.append(SessionReport(
            session=s, track=repo.get_track(conn, s.track_id), setup=repo.get_setup(conn, s.setup_id),
            laps=repo.list_laps(conn, s.id), best=repo.best_lap(conn, s.id), feedback=repo.list_feedback(conn, s.id),
            targets=repo.session_tire_targets(conn, s), changes_after=changes,
        ))
    if not sessions:
        raise repo.NotFound(f"No sessions for {bike.name} between {date_from} and {date_to}.")
    return WeekendReport(bike, repo.get_state(conn, "rider_profile"), date_from, date_to, sessions)


def _check_date(value: str) -> None:
    try:
        Date.fromisoformat(value)
    except ValueError:
        raise repo.RepoError(f"Dates look like 2026-10-13, got '{value}'.") from None


# --- markdown ------------------------------------------------------------

def to_markdown(report: WeekendReport) -> str:
    out: list[str] = []
    first, last = report.sessions[0].session.date, report.sessions[-1].session.date
    when = first if first == last else f"{first} to {last}"
    out.append(f"# {report.bike.name} at {', '.join(report.tracks)}, {when}")
    out.append("")
    if report.bike.description:
        out.append(f"Bike: {report.bike.description}  ")
    if report.rider_profile:
        out.append(f"Rider: {report.rider_profile}")
    out.append("")

    out += ["## Summary", "", "| Session | Date | Track °C | Setup | Laps | Best lap | Gap to best |",
            "|---|---|---|---|---|---|---|"]
    overall = min((s.best.time_ms for s in report.sessions if s.best), default=None)
    for s in report.sessions:
        best = timing.format_time(s.best.time_ms) if s.best else "-"
        gap = timing.format_gap(s.best.time_ms - overall) if s.best and overall is not None else "-"
        out.append(f"| {_cell(s.session.type)} | {s.session.date} | {_num(s.session.track_temp)} | "
                   f"#{s.setup.id} | {len(s.laps)} | {best} | {gap} |")
    out.append("")

    out += ["## Setup at the start", ""] + _setup_table(report.sessions[0].setup) + [""]

    out += ["## Setup changes", ""]
    if not report.changes:
        out += ["No setup changes this weekend.", ""]
    for c in report.changes:
        out += _change_lines(c) + [""]

    out += ["## Sessions", ""]
    for s in report.sessions:
        out += _session_lines(s)
    return "\n".join(out).rstrip() + "\n"


def _session_lines(s: SessionReport) -> list[str]:
    session = s.session
    out = [f"### {session.type}, {session.date} ({s.track.name}, setup #{s.setup.id})", ""]
    conditions = [f"air {_num(session.air_temp)} °C", f"track {_num(session.track_temp)} °C"]
    if session.weather:
        conditions.append(session.weather)
    out.append("- Conditions: " + ", ".join(conditions))
    out.append(f"- Tires: front {_tire(session.tire_front, session.tire_front_laps)}, "
               f"rear {_tire(session.tire_rear, session.tire_rear_laps)}")
    pressures = _pressures(s)
    if pressures:
        out.append(f"- Pressures: {pressures}")
    if session.notes:
        out.append(f"- Notes: {session.notes}")
    out.append("")

    if s.laps:
        sectors = max(len(lap.sectors_ms) for lap in s.laps)
        header = ["Lap", "Time", "Gap"] + [f"S{i}" for i in range(1, sectors + 1)]
        out += ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
        for lap in s.laps:
            gap = timing.format_gap(lap.time_ms - s.best.time_ms) if lap.valid and s.best else "invalid"
            cells = [str(lap.lap_number), timing.format_time(lap.time_ms), gap]
            cells += [timing.format_time(ms) for ms in lap.sectors_ms] + [""] * (sectors - len(lap.sectors_ms))
            out.append("| " + " | ".join(cells) + " |")
        out.append("")

    if s.feedback:
        out.append("Rider feedback:")
        out.append("")
        for f in s.feedback:
            tags = ", ".join(t for t in (f.end, f.phase, " ".join(f.corners), f.symptom,
                                         f"severity {f.severity}" if f.severity else None) if t)
            out.append(f"- \"{f.text}\"" + (f" ({tags})" if tags else ""))
        out.append("")

    for c in s.changes_after:
        out.append(f"Changed after this session: change #{c.change.id}, "
                   + "; ".join(setup_diff.format_delta(d) for d in c.change.deltas))
        out.append("")
    return out


def _change_lines(c: ChangeReport) -> list[str]:
    change = c.change
    who = "engineer" if change.proposed_by == "agent" else "rider"
    after = f" after {c.after_session.type} ({c.after_session.date})" if c.after_session else ""
    out = [f"**Change #{change.id}**{after}, by the {who}: "
           + "; ".join(setup_diff.format_delta(d) for d in change.deltas)]
    for label, value in (("Targets", change.symptom), ("Why", change.hypothesis), ("Reasoning", change.reasoning),
                         ("Feel for", change.watch_for)):
        if value:
            out.append(f"- {label}: {value}")
    if not c.outcomes:
        out.append("- Result: not recorded yet")
    for o in c.outcomes:
        result = o.verdict
        if o.lap_delta_ms is not None:
            result += f", best lap {timing.format_gap(o.lap_delta_ms)}"
        if o.symptom_resolved is not None:
            result += ", complaint resolved" if o.symptom_resolved else ", complaint not resolved"
        if o.notes:
            result += f": \"{o.notes}\""
        out.append(f"- Result: {result}")
    return out


def _setup_table(setup: Setup) -> list[str]:
    if not setup.params:
        return ["No setup values recorded."]
    rows = ["| Setting | Value | Unit |", "|---|---|---|"]
    for key in sorted(setup.params, key=setup_diff.sort_key):
        rows.append(f"| {setup_diff.label(key)} | {_cell(setup_diff.format_number(setup.params[key]))} | "
                    f"{setup_diff.unit(key)} |")
    return rows


def _pressures(s: SessionReport) -> str:
    parts = []
    for position, hot in (("front", s.session.front_hot), ("rear", s.session.rear_hot)):
        cold = s.setup.params.get(f"tire.{position}_cold")
        if cold is None and hot is None:
            continue
        text = f"{position} {setup_diff.format_number(cold)} cold"
        if hot is not None:
            text += f" / {hot:g} hot"
        text += " bar"
        target = s.targets[position]
        if hot is not None and target:
            text += f" (hot target {target.hot_min:g}-{target.hot_max:g})"
        parts.append(text)
    return "; ".join(parts)


def _tire(name: str | None, laps: int | None) -> str:
    return (name or "-") + (f" ({laps} laps before)" if laps is not None else "")


def _num(value: float | None) -> str:
    return "-" if value is None else f"{value:g}"


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


# --- csv -------------------------------------------------------------------

def to_csv(report: WeekendReport) -> str:
    """One row per lap, with the session's conditions and setup, for spreadsheets."""
    sectors = max((len(lap.sectors_ms) for s in report.sessions for lap in s.laps), default=0)
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["date", "session_id", "session", "track", "air_temp_c", "track_temp_c", "tire_front",
                     "tire_rear", "setup_id", "lap", "time", "time_s", "valid", "gap_to_session_best_s"]
                    + [f"s{i}_s" for i in range(1, sectors + 1)])
    for s in report.sessions:
        for lap in s.laps:
            gap = _seconds(lap.time_ms - s.best.time_ms) if lap.valid and s.best else ""
            writer.writerow([s.session.date, s.session.id, s.session.type, s.track.name, _blank(s.session.air_temp),
                             _blank(s.session.track_temp), s.session.tire_front or "", s.session.tire_rear or "",
                             s.setup.id, lap.lap_number, timing.format_time(lap.time_ms), _seconds(lap.time_ms),
                             int(lap.valid), gap]
                            + [_seconds(ms) for ms in lap.sectors_ms] + [""] * (sectors - len(lap.sectors_ms)))
    return buffer.getvalue()


def _seconds(ms: int) -> float:
    return round(ms / 1000, 3)


def _blank(value: float | None) -> str | float:
    return "" if value is None else value

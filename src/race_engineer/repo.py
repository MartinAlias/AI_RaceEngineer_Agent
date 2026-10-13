"""Data access. Every function takes an open connection; writes commit before returning."""

from __future__ import annotations

import json
import sqlite3
from datetime import date as Date
from typing import Any

from . import guide, ranges, setup_diff, tagging
from .models import Bike, Change, Corner, Feedback, Lap, Outcome, Session, Setup, TireTarget, Track, from_row

VERDICTS = ("better", "same", "worse")
POSITIONS = ("front", "rear")
HOT_PLAUSIBLE = (1.0, 4.0)  # bar: anything outside is a misread gauge or the wrong unit


class RepoError(Exception):
    pass


class NotFound(RepoError):
    pass


class RangeError(RepoError):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__(
            "; ".join(problems) + ". Use --force to apply anyway, or set this bike's range with `re bike range`."
        )


# --- state (current bike / session / rider profile) -------------------------

def get_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_state(conn: sqlite3.Connection, key: str, value: Any) -> None:
    with conn:
        _write_state(conn, key, value)


def _write_state(conn: sqlite3.Connection, key: str, value: Any) -> None:
    if value is None:
        conn.execute("DELETE FROM state WHERE key = ?", (key,))
    else:
        conn.execute(
            "INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )


# --- bikes ---------------------------------------------------------------

def add_bike(
    conn: sqlite3.Connection,
    name: str,
    *,
    make: str | None = None,
    model: str | None = None,
    year: int | None = None,
    notes: str | None = None,
    baseline: dict[str, Any] | None = None,
    force: bool = False,
) -> Bike:
    if not force and (problems := ranges.violations(ranges.default_ranges(), baseline or {})):
        raise RangeError(problems)
    try:
        with conn:
            bike_id = conn.execute(
                "INSERT INTO bike (name, make, model, year, notes) VALUES (?, ?, ?, ?, ?)",
                (name, make, model, year, notes),
            ).lastrowid
            setup_id = _insert_setup(conn, bike_id, None, baseline or {}, "Baseline")
            conn.execute(
                "UPDATE bike SET baseline_setup_id = ?, current_setup_id = ? WHERE id = ?",
                (setup_id, setup_id, bike_id),
            )
            if get_state(conn, "current_bike_id") is None:
                _write_state(conn, "current_bike_id", bike_id)
    except sqlite3.IntegrityError:
        raise RepoError(f"A bike called '{name}' already exists.") from None
    return get_bike(conn, bike_id)


def get_bike(conn: sqlite3.Connection, ref: int | str) -> Bike:
    row = _lookup(conn, "bike", ref)
    if not row:
        raise NotFound(f"No bike '{ref}'. See `re bike list`.")
    return from_row(Bike, row)


def list_bikes(conn: sqlite3.Connection) -> list[Bike]:
    return [from_row(Bike, r) for r in conn.execute("SELECT * FROM bike ORDER BY id")]


def current_bike(conn: sqlite3.Connection) -> Bike:
    bike_id = get_state(conn, "current_bike_id")
    if bike_id is None:
        raise NotFound("No bike selected. Add one with `re bike add`.")
    return get_bike(conn, int(bike_id))


def use_bike(conn: sqlite3.Connection, ref: int | str) -> Bike:
    bike = get_bike(conn, ref)
    with conn:
        _write_state(conn, "current_bike_id", bike.id)
        session_id = get_state(conn, "current_session_id")
        if session_id and get_session(conn, int(session_id)).bike_id != bike.id:
            _write_state(conn, "current_session_id", None)
    return bike


def set_baseline(conn: sqlite3.Connection, bike_id: int, setup_id: int | None = None) -> Bike:
    bike = get_bike(conn, bike_id)
    setup = get_setup(conn, setup_id or bike.current_setup_id)
    if setup.bike_id != bike.id:
        raise RepoError(f"Setup #{setup.id} belongs to a different bike.")
    with conn:
        conn.execute("UPDATE bike SET baseline_setup_id = ? WHERE id = ?", (setup.id, bike.id))
    return get_bike(conn, bike.id)


def get_param_ranges(conn: sqlite3.Connection, bike_id: int) -> dict[str, ranges.Range]:
    """Effective ranges for a bike: defaults, replaced by the bike's own ranges."""
    merged = dict(ranges.default_ranges())
    merged.update(bike_param_ranges(conn, bike_id))
    return merged


def bike_param_ranges(conn: sqlite3.Connection, bike_id: int) -> dict[str, ranges.Range]:
    rows = conn.execute("SELECT param, min, max FROM param_range WHERE bike_id = ?", (bike_id,))
    return {r["param"]: (r["min"], r["max"]) for r in rows}


def set_param_range(conn: sqlite3.Connection, bike_id: int, param: str, low: float, high: float) -> None:
    param = setup_diff.normalize_key(param)
    if low > high:
        raise RepoError(f"Minimum {low:g} is above maximum {high:g}.")
    get_bike(conn, bike_id)
    with conn:
        conn.execute(
            """INSERT INTO param_range (bike_id, param, min, max) VALUES (?, ?, ?, ?)
               ON CONFLICT (bike_id, param) DO UPDATE SET min = excluded.min, max = excluded.max""",
            (bike_id, param, low, high),
        )


def clear_param_range(conn: sqlite3.Connection, bike_id: int, param: str) -> None:
    with conn:
        conn.execute("DELETE FROM param_range WHERE bike_id = ? AND param = ?",
                     (bike_id, setup_diff.normalize_key(param)))


# --- tracks --------------------------------------------------------------

def add_track(
    conn: sqlite3.Connection,
    name: str,
    *,
    length_m: float | None = None,
    direction: str | None = None,
    notes: str | None = None,
    corners: list[tuple[int, str | None, str | None]] | None = None,
) -> Track:
    if direction not in (None, "cw", "ccw"):
        raise RepoError("Direction must be 'cw' or 'ccw'.")
    try:
        with conn:
            track_id = conn.execute(
                "INSERT INTO track (name, length_m, direction, notes) VALUES (?, ?, ?, ?)",
                (name, length_m, direction, notes),
            ).lastrowid
            for number, corner_name, corner_type in corners or []:
                _upsert_corner(conn, track_id, number, corner_name, corner_type)
    except sqlite3.IntegrityError:
        raise RepoError(f"A track called '{name}' already exists.") from None
    return get_track(conn, track_id)


def get_track(conn: sqlite3.Connection, ref: int | str) -> Track:
    row = _lookup(conn, "track", ref)
    if not row:
        raise NotFound(f"No track '{ref}'. See `re track list`.")
    return from_row(Track, row)


def list_tracks(conn: sqlite3.Connection) -> list[Track]:
    return [from_row(Track, r) for r in conn.execute("SELECT * FROM track ORDER BY name")]


def set_corner(
    conn: sqlite3.Connection, track_id: int, number: int, name: str | None = None, type: str | None = None
) -> Corner:
    with conn:
        _upsert_corner(conn, track_id, number, name, type)
    row = conn.execute("SELECT * FROM corner WHERE track_id = ? AND number = ?", (track_id, number)).fetchone()
    return from_row(Corner, row)


def list_corners(conn: sqlite3.Connection, track_id: int) -> list[Corner]:
    rows = conn.execute("SELECT * FROM corner WHERE track_id = ? ORDER BY number", (track_id,))
    return [from_row(Corner, r) for r in rows]


def _upsert_corner(conn: sqlite3.Connection, track_id: int, number: int, name: str | None, type: str | None) -> None:
    conn.execute(
        """INSERT INTO corner (track_id, number, name, type) VALUES (?, ?, ?, ?)
           ON CONFLICT (track_id, number) DO UPDATE SET
               name = COALESCE(excluded.name, name),
               type = COALESCE(excluded.type, type)""",
        (track_id, number, name, type),
    )


# --- sessions ------------------------------------------------------------

def start_session(
    conn: sqlite3.Connection,
    bike_id: int,
    track_id: int,
    *,
    type: str,
    date: str | None = None,
    air_temp: float | None = None,
    track_temp: float | None = None,
    weather: str | None = None,
    tire_front: str | None = None,
    tire_rear: str | None = None,
    tire_front_laps: int | None = None,
    tire_rear_laps: int | None = None,
    notes: str | None = None,
) -> Session:
    bike = get_bike(conn, bike_id)
    get_track(conn, track_id)
    with conn:
        session_id = conn.execute(
            """INSERT INTO session (bike_id, track_id, setup_id, date, type, air_temp, track_temp, weather,
                                    tire_front, tire_rear, tire_front_laps, tire_rear_laps, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (bike.id, track_id, bike.current_setup_id, date or Date.today().isoformat(), type, air_temp,
             track_temp, weather, tire_front, tire_rear, tire_front_laps, tire_rear_laps, notes),
        ).lastrowid
        _write_state(conn, "current_bike_id", bike.id)
        _write_state(conn, "current_session_id", session_id)
    return get_session(conn, session_id)


def get_session(conn: sqlite3.Connection, session_id: int) -> Session:
    row = conn.execute("SELECT * FROM session WHERE id = ?", (session_id,)).fetchone()
    if not row:
        raise NotFound(f"No session #{session_id}. See `re session list`.")
    return from_row(Session, row)


def current_session(conn: sqlite3.Connection) -> Session:
    session_id = get_state(conn, "current_session_id")
    if session_id is None:
        raise NotFound("No active session. Start one with `re session start`.")
    return get_session(conn, int(session_id))


def use_session(conn: sqlite3.Connection, session_id: int) -> Session:
    session = get_session(conn, session_id)
    with conn:
        _write_state(conn, "current_bike_id", session.bike_id)
        _write_state(conn, "current_session_id", session.id)
    return session


def list_sessions(
    conn: sqlite3.Connection, *, bike_id: int | None = None, track_id: int | None = None, limit: int = 20
) -> list[Session]:
    rows = conn.execute(
        """SELECT * FROM session
           WHERE (:bike IS NULL OR bike_id = :bike) AND (:track IS NULL OR track_id = :track)
           ORDER BY date DESC, id DESC LIMIT :limit""",
        {"bike": bike_id, "track": track_id, "limit": limit},
    )
    return [from_row(Session, r) for r in rows]


def sessions_between(
    conn: sqlite3.Connection, bike_id: int, date_from: str, date_to: str, *, track_id: int | None = None
) -> list[Session]:
    """A bike's sessions from date_from to date_to (inclusive, YYYY-MM-DD), oldest first."""
    rows = conn.execute(
        """SELECT * FROM session WHERE bike_id = :bike AND date BETWEEN :start AND :end
             AND (:track IS NULL OR track_id = :track)
           ORDER BY date, id""",
        {"bike": bike_id, "start": date_from, "end": date_to, "track": track_id},
    )
    return [from_row(Session, r) for r in rows]


def previous_session(conn: sqlite3.Connection, session: Session) -> Session | None:
    """The bike's session before this one (by date, then id)."""
    row = conn.execute(
        """SELECT * FROM session WHERE bike_id = ? AND (date < ? OR (date = ? AND id < ?))
           ORDER BY date DESC, id DESC LIMIT 1""",
        (session.bike_id, session.date, session.date, session.id),
    ).fetchone()
    return from_row(Session, row) if row else None


def record_hot_pressures(
    conn: sqlite3.Connection,
    session_id: int,
    *,
    front: float | None = None,
    rear: float | None = None,
    force: bool = False,
) -> Session:
    """Store hot pressures (bar) measured after a session.

    A reading that is implausible, or not above the cold pressure the session ran,
    is refused unless force is set: it is almost always a misread gauge or psi typed as bar.
    """
    if front is None and rear is None:
        raise RepoError("Give a front and/or rear hot pressure.")
    session = get_session(conn, session_id)
    cold = get_setup(conn, session.setup_id).params
    problems = []
    for position, hot in (("front", front), ("rear", rear)):
        if hot is None:
            continue
        low, high = HOT_PLAUSIBLE
        if not low <= hot <= high:
            problems.append(f"{position} hot {hot:g} bar is not a believable tire pressure")
            continue
        cold_value = cold.get(f"tire.{position}_cold")
        if isinstance(cold_value, (int, float)) and hot <= cold_value:
            problems.append(f"{position} hot {hot:g} bar is not above the {cold_value:g} bar cold pressure "
                            f"this session ran (setup #{session.setup_id})")
    if problems and not force:
        raise RepoError("; ".join(problems) + ". Check the reading and the unit (psi vs bar), "
                        "or use --force if it is right.")
    with conn:
        conn.execute("UPDATE session SET front_hot = COALESCE(?, front_hot), rear_hot = COALESCE(?, rear_hot) "
                     "WHERE id = ?", (front, rear, session.id))
    return get_session(conn, session.id)


# --- tire hot-pressure targets -------------------------------------------

def set_tire_target(conn: sqlite3.Connection, name: str, position: str, low: float, high: float) -> TireTarget:
    if position not in POSITIONS:
        raise RepoError("Position must be front or rear.")
    if low > high:
        raise RepoError(f"Minimum {low:g} is above maximum {high:g}.")
    with conn:
        conn.execute(
            """INSERT INTO tire_target (name, position, hot_min, hot_max) VALUES (?, ?, ?, ?)
               ON CONFLICT (name, position) DO UPDATE SET hot_min = excluded.hot_min, hot_max = excluded.hot_max""",
            (name.strip(), position, low, high),
        )
    return get_tire_target(conn, name.strip(), position)


def get_tire_target(conn: sqlite3.Connection, name: str | None, position: str) -> TireTarget | None:
    if not name:
        return None
    row = conn.execute("SELECT * FROM tire_target WHERE name = ? AND position = ?", (name.strip(), position)).fetchone()
    return from_row(TireTarget, row) if row else None


def list_tire_targets(conn: sqlite3.Connection) -> list[TireTarget]:
    return [from_row(TireTarget, r) for r in conn.execute("SELECT * FROM tire_target ORDER BY name, position")]


def session_tire_targets(conn: sqlite3.Connection, session: Session) -> dict[str, TireTarget | None]:
    return {"front": get_tire_target(conn, session.tire_front, "front"),
            "rear": get_tire_target(conn, session.tire_rear, "rear")}


# --- setups and changes --------------------------------------------------

def get_setup(conn: sqlite3.Connection, setup_id: int) -> Setup:
    row = conn.execute("SELECT * FROM setup WHERE id = ?", (setup_id,)).fetchone()
    if not row:
        raise NotFound(f"No setup #{setup_id}.")
    return from_row(Setup, row, ("params",))


def setup_history(conn: sqlite3.Connection, bike_id: int) -> list[Setup]:
    rows = conn.execute("SELECT * FROM setup WHERE bike_id = ? ORDER BY id", (bike_id,))
    return [from_row(Setup, r, ("params",)) for r in rows]


def change_setup(
    conn: sqlite3.Connection,
    bike_id: int,
    assignments: list[setup_diff.Assignment],
    *,
    note: str | None = None,
    proposed_by: str = "rider",
    hypothesis: str | None = None,
    watch_for: str | None = None,
    reasoning: str | None = None,
    session_id: int | None = None,
    symptom: str | None = None,
    force: bool = False,
    ran_in_session_id: int | None = None,
) -> Change:
    """Apply assignments to the bike's current setup, creating a new setup and a change record.

    Changed values must be inside the bike's allowed ranges unless force is set.

    ran_in_session_id is for a change already on the bike when that session ran (made in
    the pit lane after the session was started): the session is moved to the new setup and
    the change is linked to the session before it, so results compare against that one.
    """
    bike = get_bike(conn, bike_id)
    before = get_setup(conn, bike.current_setup_id)
    if ran_in_session_id is not None:
        ran = get_session(conn, ran_in_session_id)
        if ran.bike_id != bike.id:
            raise RepoError(f"Session #{ran.id} is for a different bike.")
        if ran.setup_id != before.id:
            raise RepoError(f"Session #{ran.id} ran setup #{ran.setup_id}, but the bike is now on setup #{before.id}; "
                            "a change can only be added to the latest session on the current setup.")
        previous = previous_session(conn, ran)
        session_id = previous.id if previous else None
    params, deltas = setup_diff.apply_assignments(before.params, assignments)
    if not deltas:
        raise RepoError("Nothing changed: those values match the current setup.")
    changed = [d["param"] for d in deltas]
    if not force and (problems := ranges.violations(get_param_ranges(conn, bike.id), params, changed)):
        raise RangeError(problems)
    return _record_change(conn, bike, before, params, deltas, note, proposed_by, hypothesis, watch_for,
                          reasoning, session_id, symptom, ran_in_session_id)


def revert_setup(
    conn: sqlite3.Connection, bike_id: int, setup_id: int, *, session_id: int | None = None
) -> Change:
    """Go back to an earlier setup. History is kept: this adds a new setup copied from the old one."""
    bike = get_bike(conn, bike_id)
    target = get_setup(conn, setup_id)
    if target.bike_id != bike.id:
        raise RepoError(f"Setup #{setup_id} belongs to a different bike.")
    before = get_setup(conn, bike.current_setup_id)
    deltas = setup_diff.diff(before.params, target.params)
    if not deltas:
        raise RepoError(f"The current setup already matches setup #{setup_id}.")
    return _record_change(conn, bike, before, dict(target.params), deltas, f"Revert to setup #{setup_id}",
                          "rider", None, None, None, session_id)


def get_change(conn: sqlite3.Connection, change_id: int) -> Change:
    row = conn.execute("SELECT * FROM change WHERE id = ?", (change_id,)).fetchone()
    if not row:
        raise NotFound(f"No change #{change_id}.")
    return from_row(Change, row, ("deltas",))


def list_changes(
    conn: sqlite3.Connection, *, bike_id: int | None = None, session_id: int | None = None
) -> list[Change]:
    rows = conn.execute(
        """SELECT * FROM change
           WHERE (:bike IS NULL OR bike_id = :bike) AND (:session IS NULL OR session_id = :session)
           ORDER BY id""",
        {"bike": bike_id, "session": session_id},
    )
    return [from_row(Change, r, ("deltas",)) for r in rows]


def _record_change(
    conn: sqlite3.Connection,
    bike: Bike,
    before: Setup,
    params: dict[str, Any],
    deltas: list[dict],
    note: str | None,
    proposed_by: str,
    hypothesis: str | None,
    watch_for: str | None,
    reasoning: str | None,
    session_id: int | None,
    symptom: str | None = None,
    ran_in_session_id: int | None = None,
) -> Change:
    with conn:
        after_id = _insert_setup(conn, bike.id, before.id, params, note)
        if ran_in_session_id is not None:
            conn.execute("UPDATE session SET setup_id = ? WHERE id = ?", (after_id, ran_in_session_id))
        change_id = conn.execute(
            """INSERT INTO change (bike_id, session_id, before_setup_id, after_setup_id, deltas,
                                   hypothesis, watch_for, reasoning, proposed_by, symptom)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (bike.id, session_id, before.id, after_id, json.dumps(deltas), hypothesis, watch_for,
             reasoning, proposed_by, symptom),
        ).lastrowid
        conn.execute("UPDATE bike SET current_setup_id = ? WHERE id = ?", (after_id, bike.id))
    return get_change(conn, change_id)


def _insert_setup(
    conn: sqlite3.Connection, bike_id: int, parent_id: int | None, params: dict[str, Any], notes: str | None
) -> int:
    return conn.execute(
        "INSERT INTO setup (bike_id, parent_id, params, notes) VALUES (?, ?, ?, ?)",
        (bike_id, parent_id, json.dumps(params, sort_keys=True), notes),
    ).lastrowid


# --- laps ----------------------------------------------------------------

def add_laps(
    conn: sqlite3.Connection,
    session_id: int,
    times_ms: list[int],
    *,
    sectors_ms: list[int] | None = None,
    valid: bool = True,
) -> list[Lap]:
    get_session(conn, session_id)
    if sectors_ms and len(times_ms) != 1:
        raise RepoError("Sector times can only be given when adding a single lap.")
    last = conn.execute("SELECT MAX(lap_number) FROM lap WHERE session_id = ?", (session_id,)).fetchone()[0] or 0
    with conn:
        for offset, time_ms in enumerate(times_ms, start=1):
            conn.execute(
                "INSERT INTO lap (session_id, lap_number, time_ms, sectors_ms, valid) VALUES (?, ?, ?, ?, ?)",
                (session_id, last + offset, time_ms, json.dumps(sectors_ms or []), int(valid)),
            )
    return [lap for lap in list_laps(conn, session_id) if lap.lap_number > last]


def import_laps(conn: sqlite3.Connection, session_id: int, laps: list[tuple[int, list[int], bool]]) -> list[Lap]:
    """Add laps as (time_ms, sectors_ms, valid), numbered after the session's existing laps, in one transaction."""
    get_session(conn, session_id)
    last = conn.execute("SELECT MAX(lap_number) FROM lap WHERE session_id = ?", (session_id,)).fetchone()[0] or 0
    with conn:
        for offset, (time_ms, sectors_ms, valid) in enumerate(laps, start=1):
            conn.execute(
                "INSERT INTO lap (session_id, lap_number, time_ms, sectors_ms, valid) VALUES (?, ?, ?, ?, ?)",
                (session_id, last + offset, time_ms, json.dumps(sectors_ms), int(valid)),
            )
    return [lap for lap in list_laps(conn, session_id) if lap.lap_number > last]


def list_laps(conn: sqlite3.Connection, session_id: int) -> list[Lap]:
    rows = conn.execute("SELECT * FROM lap WHERE session_id = ? ORDER BY lap_number", (session_id,))
    return [from_row(Lap, r, ("sectors_ms",)) for r in rows]


def best_lap(conn: sqlite3.Connection, session_id: int) -> Lap | None:
    valid = [lap for lap in list_laps(conn, session_id) if lap.valid]
    return min(valid, key=lambda lap: lap.time_ms, default=None)


# --- rider feedback ------------------------------------------------------

def add_feedback(
    conn: sqlite3.Connection,
    session_id: int,
    text: str,
    *,
    end: str | None = None,
    phase: str | None = None,
    corners: list[str] | None = None,
    symptom: str | None = None,
    severity: int | None = None,
) -> Feedback:
    """Store feedback. Tags not given explicitly are filled from the text: end, phase and
    corners by keyword, symptom from the symptom guide when one clearly fits."""
    get_session(conn, session_id)
    if end not in (None, *tagging.ENDS):
        raise RepoError(f"End must be one of: {', '.join(tagging.ENDS)}.")
    if phase not in (None, *tagging.PHASES):
        raise RepoError(f"Phase must be one of: {', '.join(tagging.PHASES)}.")
    if severity is not None and not 1 <= severity <= 5:
        raise RepoError("Severity must be between 1 and 5.")
    auto = tagging.tag(text)
    end, phase = end or auto.end, phase or auto.phase
    corners = [c.upper() for c in corners] if corners else auto.corners
    symptom = symptom or guide.best_symptom(text, end=end, phase=phase)
    with conn:
        feedback_id = conn.execute(
            """INSERT INTO feedback (session_id, text, "end", phase, corners, symptom, severity)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (session_id, text, end, phase, json.dumps(corners), symptom, severity),
        ).lastrowid
    return get_feedback(conn, feedback_id)


def get_feedback(conn: sqlite3.Connection, feedback_id: int) -> Feedback:
    row = conn.execute("SELECT * FROM feedback WHERE id = ?", (feedback_id,)).fetchone()
    if not row:
        raise NotFound(f"No feedback #{feedback_id}.")
    return from_row(Feedback, row, ("corners",))


def list_feedback(conn: sqlite3.Connection, session_id: int) -> list[Feedback]:
    rows = conn.execute("SELECT * FROM feedback WHERE session_id = ? ORDER BY id", (session_id,))
    return [from_row(Feedback, r, ("corners",)) for r in rows]


def latest_symptom(conn: sqlite3.Connection, session_id: int) -> str | None:
    """Symptom of the most recent tagged feedback in a session (what a change usually targets)."""
    row = conn.execute(
        "SELECT symptom FROM feedback WHERE session_id = ? AND symptom IS NOT NULL ORDER BY id DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    return row["symptom"] if row else None


# --- outcomes ------------------------------------------------------------

def add_outcome(
    conn: sqlite3.Connection,
    change_id: int,
    verdict: str,
    *,
    session_id: int | None = None,
    lap_delta_ms: int | None = None,
    symptom_resolved: bool | None = None,
    notes: str | None = None,
) -> Outcome:
    """Record how a change worked out, tested in session_id."""
    get_change(conn, change_id)
    if verdict not in VERDICTS:
        raise RepoError(f"Verdict must be one of: {', '.join(VERDICTS)}.")
    if session_id is not None:
        get_session(conn, session_id)
    resolved = None if symptom_resolved is None else int(symptom_resolved)
    with conn:
        outcome_id = conn.execute(
            """INSERT INTO outcome (change_id, session_id, verdict, lap_delta_ms, symptom_resolved, notes)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (change_id, session_id, verdict, lap_delta_ms, resolved, notes),
        ).lastrowid
    return from_row(Outcome, conn.execute("SELECT * FROM outcome WHERE id = ?", (outcome_id,)).fetchone())


def list_outcomes(conn: sqlite3.Connection, change_id: int) -> list[Outcome]:
    rows = conn.execute("SELECT * FROM outcome WHERE change_id = ? ORDER BY id", (change_id,))
    return [from_row(Outcome, r) for r in rows]


def changes_without_outcome(conn: sqlite3.Connection, bike_id: int) -> list[Change]:
    """Changes whose result hasn't been recorded yet, newest first."""
    rows = conn.execute(
        """SELECT * FROM change c WHERE c.bike_id = ?
           AND NOT EXISTS (SELECT 1 FROM outcome o WHERE o.change_id = c.id)
           ORDER BY c.id DESC""",
        (bike_id,),
    )
    return [from_row(Change, r, ("deltas",)) for r in rows]


def lap_delta(conn: sqlite3.Connection, change: Change, test_session_id: int | None) -> int | None:
    """Best lap in the test session minus best lap in the session the change was made after.

    Negative = faster. None when either session is unknown or has no valid laps.
    """
    if test_session_id is None or change.session_id is None or test_session_id == change.session_id:
        return None
    before, after = best_lap(conn, change.session_id), best_lap(conn, test_session_id)
    if before is None or after is None:
        return None
    return after.time_ms - before.time_ms


def update_feedback(
    conn: sqlite3.Connection,
    feedback_id: int,
    *,
    end: str | None = None,
    phase: str | None = None,
    corners: list[str] | None = None,
    symptom: str | None = None,
) -> Feedback:
    """Refine a feedback's tags (e.g. after follow-up questions). Fields left None are kept."""
    current = get_feedback(conn, feedback_id)
    if end not in (None, *tagging.ENDS):
        raise RepoError(f"End must be one of: {', '.join(tagging.ENDS)}.")
    if phase not in (None, *tagging.PHASES):
        raise RepoError(f"Phase must be one of: {', '.join(tagging.PHASES)}.")
    with conn:
        conn.execute(
            """UPDATE feedback SET "end" = ?, phase = ?, corners = ?, symptom = ? WHERE id = ?""",
            (end or current.end, phase or current.phase,
             json.dumps([c.upper() for c in corners] if corners is not None else current.corners),
             symptom or current.symptom, feedback_id),
        )
    return get_feedback(conn, feedback_id)


# --- engineer: offline queue and transcripts -----------------------------

def queue_analysis(conn: sqlite3.Connection, feedback_id: int) -> None:
    get_feedback(conn, feedback_id)
    with conn:
        conn.execute("INSERT OR IGNORE INTO pending_analysis (feedback_id) VALUES (?)", (feedback_id,))


def pending_analysis(conn: sqlite3.Connection) -> list[Feedback]:
    rows = conn.execute(
        "SELECT f.* FROM pending_analysis p JOIN feedback f ON f.id = p.feedback_id ORDER BY p.created_at, f.id"
    )
    return [from_row(Feedback, r, ("corners",)) for r in rows]


def latest_undiscussed_feedback(conn: sqlite3.Connection, session_id: int) -> Feedback | None:
    """Most recent feedback in a session that no engineer conversation has covered yet."""
    row = conn.execute(
        """SELECT f.* FROM feedback f WHERE f.session_id = ?
           AND NOT EXISTS (SELECT 1 FROM engineer_conversation e WHERE e.feedback_id = f.id)
           ORDER BY f.id DESC LIMIT 1""",
        (session_id,),
    ).fetchone()
    return from_row(Feedback, row, ("corners",)) if row else None


def clear_pending(conn: sqlite3.Connection, feedback_id: int) -> None:
    with conn:
        conn.execute("DELETE FROM pending_analysis WHERE feedback_id = ?", (feedback_id,))


def save_conversation(
    conn: sqlite3.Connection,
    conversation_id: int | None,
    *,
    bike_id: int,
    session_id: int | None,
    feedback_id: int | None,
    model: str,
    messages: list[Any],
) -> int:
    """Insert or update an engineer transcript; returns its id."""
    payload = json.dumps(messages)
    with conn:
        if conversation_id is None:
            return conn.execute(
                """INSERT INTO engineer_conversation (bike_id, session_id, feedback_id, model, messages)
                   VALUES (?, ?, ?, ?, ?)""",
                (bike_id, session_id, feedback_id, model, payload),
            ).lastrowid
        conn.execute("UPDATE engineer_conversation SET messages = ? WHERE id = ?", (payload, conversation_id))
        return conversation_id


# --- helpers -------------------------------------------------------------

def _lookup(conn: sqlite3.Connection, table: str, ref: int | str) -> sqlite3.Row | None:
    """Find a bike or track by id or (case-insensitive) name."""
    if isinstance(ref, int) or str(ref).isdigit():
        row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (int(ref),)).fetchone()
        if row:
            return row
    return conn.execute(f"SELECT * FROM {table} WHERE name = ?", (str(ref),)).fetchone()

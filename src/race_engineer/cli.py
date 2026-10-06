"""Command-line interface. Run `re --help` to see the commands."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import List, Optional

import typer
from rich.console import Console
from rich.table import Table

from . import db, repo, setup_diff, timing
from .models import Setup

app = typer.Typer(help="Virtual race engineer: log sessions, setups, laps and rider feedback.", no_args_is_help=True)
bike_app = typer.Typer(help="Add, list and select bikes.", no_args_is_help=True)
track_app = typer.Typer(help="Add tracks and their corners.", no_args_is_help=True)
session_app = typer.Typer(help="Start and review sessions.", no_args_is_help=True)
setup_app = typer.Typer(help="Show, change, compare and revert setups.", no_args_is_help=True)
lap_app = typer.Typer(help="Log lap times.", no_args_is_help=True)
app.add_typer(bike_app, name="bike")
app.add_typer(track_app, name="track")
app.add_typer(session_app, name="session")
app.add_typer(setup_app, name="setup")
app.add_typer(lap_app, name="lap")

console = Console()
_db_path: Optional[Path] = None


@app.callback()
def main(
    db_path: Optional[Path] = typer.Option(
        None, "--db", envvar=db.DB_ENV, help="Database file (default: ~/.race_engineer/race_engineer.db)."
    ),
) -> None:
    global _db_path
    _db_path = db_path


@contextmanager
def _open() -> Iterator[sqlite3.Connection]:
    conn = db.connect(_db_path)
    try:
        yield conn
    except (repo.RepoError, ValueError) as error:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(1) from None
    finally:
        conn.close()


# --- init / profile ------------------------------------------------------

@app.command()
def init(
    profile: Optional[str] = typer.Option(None, help='Your riding style, e.g. "late braker, likes a stiff front".'),
) -> None:
    """Create the database (safe to run again)."""
    with _open() as conn:
        if profile:
            repo.set_state(conn, "rider_profile", profile)
    console.print(f"Database ready at [bold]{db.resolve_path(_db_path)}[/bold]")
    console.print("Next: [cyan]re bike add[/cyan], [cyan]re track add[/cyan], then [cyan]re session start[/cyan].")


@app.command()
def profile(text: Optional[str] = typer.Argument(None, help="New profile text. Omit to show the current one.")) -> None:
    """Show or set your rider profile (riding style and preferences)."""
    with _open() as conn:
        if text:
            repo.set_state(conn, "rider_profile", text)
            console.print("Rider profile saved.")
        else:
            console.print(repo.get_state(conn, "rider_profile") or "[dim]No profile yet.[/dim]")


# --- bikes ---------------------------------------------------------------

@bike_app.command("add")
def bike_add(
    name: str = typer.Argument(..., help='Short name, e.g. "R6".'),
    make: Optional[str] = typer.Option(None),
    model: Optional[str] = typer.Option(None),
    year: Optional[int] = typer.Option(None),
    notes: Optional[str] = typer.Option(None),
    setting: Optional[List[str]] = typer.Option(
        None, "--set", help="Baseline setting key=value (repeatable), e.g. --set fork.rebound=10."
    ),
) -> None:
    """Add a bike with its baseline setup."""
    with _open() as conn:
        assignments = [setup_diff.parse_assignment(s) for s in setting or []]
        baseline, _ = setup_diff.apply_assignments({}, assignments)
        bike = repo.add_bike(conn, name, make=make, model=model, year=year, notes=notes, baseline=baseline)
        console.print(f"Added bike [bold]{bike.name}[/bold] (#{bike.id}) with baseline setup #{bike.baseline_setup_id}.")
        if repo.current_bike(conn).id == bike.id:
            console.print("It is now the current bike.")


@bike_app.command("list")
def bike_list() -> None:
    """List bikes."""
    with _open() as conn:
        current = repo.get_state(conn, "current_bike_id")
        table = Table("", "#", "Name", "Bike", "Current setup", "Baseline")
        for bike in repo.list_bikes(conn):
            marker = "*" if str(bike.id) == current else ""
            table.add_row(marker, str(bike.id), bike.name, bike.description, f"#{bike.current_setup_id}",
                          f"#{bike.baseline_setup_id}")
        console.print(table)


@bike_app.command("use")
def bike_use(bike: str = typer.Argument(..., help="Bike name or id.")) -> None:
    """Select the current bike."""
    with _open() as conn:
        selected = repo.use_bike(conn, bike)
        console.print(f"Current bike: [bold]{selected.name}[/bold]")


@bike_app.command("baseline")
def bike_baseline(setup_id: Optional[int] = typer.Argument(None, help="Setup id (default: current setup).")) -> None:
    """Mark a setup as the current bike's baseline."""
    with _open() as conn:
        bike = repo.set_baseline(conn, repo.current_bike(conn).id, setup_id)
        console.print(f"Baseline for {bike.name} is now setup #{bike.baseline_setup_id}.")


# --- tracks --------------------------------------------------------------

@track_app.command("add")
def track_add(
    name: str = typer.Argument(...),
    length: Optional[float] = typer.Option(None, help="Length in metres."),
    direction: Optional[str] = typer.Option(None, help="cw or ccw."),
    notes: Optional[str] = typer.Option(None),
    corner: Optional[List[str]] = typer.Option(
        None, help='Corner as "number[:name[:type]]" (repeatable), e.g. --corner "3:Hairpin:slow hairpin".'
    ),
) -> None:
    """Add a track."""
    with _open() as conn:
        corners = [_parse_corner(c) for c in corner or []]
        track = repo.add_track(conn, name, length_m=length, direction=direction, notes=notes, corners=corners)
        console.print(f"Added track [bold]{track.name}[/bold] (#{track.id}) with {len(corners)} corners.")


@track_app.command("list")
def track_list() -> None:
    """List tracks."""
    with _open() as conn:
        table = Table("#", "Name", "Length", "Direction", "Corners")
        for track in repo.list_tracks(conn):
            length = f"{track.length_m:g} m" if track.length_m else ""
            table.add_row(str(track.id), track.name, length, track.direction or "",
                          str(len(repo.list_corners(conn, track.id))))
        console.print(table)


@track_app.command("show")
def track_show(track: str = typer.Argument(..., help="Track name or id.")) -> None:
    """Show a track and its corners."""
    with _open() as conn:
        found = repo.get_track(conn, track)
        console.print(f"[bold]{found.name}[/bold]  {found.notes or ''}")
        table = Table("Corner", "Name", "Type")
        for c in repo.list_corners(conn, found.id):
            table.add_row(f"T{c.number}", c.name or "", c.type or "")
        console.print(table)


@track_app.command("corner")
def track_corner(
    track: str = typer.Argument(..., help="Track name or id."),
    number: int = typer.Argument(...),
    name: Optional[str] = typer.Option(None),
    type: Optional[str] = typer.Option(None, help='e.g. "slow hairpin", "fast sweeper", "chicane".'),
) -> None:
    """Add or update a corner."""
    with _open() as conn:
        found = repo.get_track(conn, track)
        c = repo.set_corner(conn, found.id, number, name, type)
        console.print(f"{found.name} T{c.number}: {c.name or ''} {c.type or ''}".rstrip())


def _parse_corner(spec: str) -> tuple[int, str | None, str | None]:
    parts = [p.strip() or None for p in spec.split(":")]
    if not parts[0] or not parts[0].isdigit() or len(parts) > 3:
        raise ValueError(f"Corner '{spec}' should look like 3:Hairpin:slow hairpin.")
    parts += [None] * (3 - len(parts))
    return int(parts[0]), parts[1], parts[2]


# --- sessions ------------------------------------------------------------

@session_app.command("start")
def session_start(
    track: str = typer.Option(..., help="Track name or id."),
    type: str = typer.Option("practice", help="e.g. FP1, practice, qualifying, race."),
    date: Optional[str] = typer.Option(None, help="YYYY-MM-DD (default: today)."),
    air_temp: Optional[float] = typer.Option(None, help="Air temperature, °C."),
    track_temp: Optional[float] = typer.Option(None, help="Track temperature, °C."),
    weather: Optional[str] = typer.Option(None),
    tire_front: Optional[str] = typer.Option(None, help="Front tire model / compound."),
    tire_rear: Optional[str] = typer.Option(None, help="Rear tire model / compound."),
    front_laps: Optional[int] = typer.Option(None, help="Laps already on the front tire."),
    rear_laps: Optional[int] = typer.Option(None, help="Laps already on the rear tire."),
    notes: Optional[str] = typer.Option(None),
    bike: Optional[str] = typer.Option(None, help="Bike name or id (default: current bike)."),
) -> None:
    """Start a session with the bike's current setup. It becomes the active session."""
    with _open() as conn:
        selected = repo.get_bike(conn, bike) if bike else repo.current_bike(conn)
        found = repo.get_track(conn, track)
        session = repo.start_session(
            conn, selected.id, found.id, type=type, date=date, air_temp=air_temp, track_temp=track_temp,
            weather=weather, tire_front=tire_front, tire_rear=tire_rear, tire_front_laps=front_laps,
            tire_rear_laps=rear_laps, notes=notes,
        )
        console.print(
            f"Session #{session.id} started: {selected.name} at {found.name}, {session.type}, "
            f"setup #{session.setup_id}."
        )


@session_app.command("list")
def session_list(
    track: Optional[str] = typer.Option(None, help="Only this track."),
    limit: int = typer.Option(20),
) -> None:
    """List recent sessions for the current bike."""
    with _open() as conn:
        bike = repo.current_bike(conn)
        track_id = repo.get_track(conn, track).id if track else None
        active = repo.get_state(conn, "current_session_id")
        table = Table("", "#", "Date", "Track", "Type", "Track °C", "Setup", "Best lap")
        for s in repo.list_sessions(conn, bike_id=bike.id, track_id=track_id, limit=limit):
            best = repo.best_lap(conn, s.id)
            table.add_row(
                "*" if str(s.id) == active else "", str(s.id), s.date, repo.get_track(conn, s.track_id).name,
                s.type, _num(s.track_temp), f"#{s.setup_id}", timing.format_time(best.time_ms) if best else "",
            )
        console.print(table)


@session_app.command("use")
def session_use(session_id: int = typer.Argument(...)) -> None:
    """Make a session the active one (e.g. to add laps or feedback later)."""
    with _open() as conn:
        session = repo.use_session(conn, session_id)
        console.print(f"Active session: #{session.id}")


@session_app.command("show")
def session_show(session_id: Optional[int] = typer.Argument(None, help="Default: the active session.")) -> None:
    """Show a session: conditions, laps, feedback and setup changes."""
    with _open() as conn:
        s = repo.get_session(conn, session_id) if session_id else repo.current_session(conn)
        bike, track = repo.get_bike(conn, s.bike_id), repo.get_track(conn, s.track_id)
        console.print(f"[bold]Session #{s.id}[/bold]  {s.date}  {bike.name} at {track.name}  ({s.type})")
        console.print(
            f"Air {_num(s.air_temp, ' °C')}  Track {_num(s.track_temp, ' °C')}  {s.weather or ''}\n"
            f"Tires: front {s.tire_front or '-'} ({_num(s.tire_front_laps, ' laps')}), "
            f"rear {s.tire_rear or '-'} ({_num(s.tire_rear_laps, ' laps')})\n"
            f"Setup #{s.setup_id}"
        )

        laps = repo.list_laps(conn, s.id)
        if laps:
            best = repo.best_lap(conn, s.id)
            table = Table("Lap", "Time", "Gap", "Sectors", title="Laps")
            for lap in laps:
                gap = timing.format_gap(lap.time_ms - best.time_ms) if best and lap.valid else "invalid"
                sectors = "  ".join(timing.format_time(ms) for ms in lap.sectors_ms)
                table.add_row(str(lap.lap_number), timing.format_time(lap.time_ms), gap, sectors)
            console.print(table)

        feedback = repo.list_feedback(conn, s.id)
        if feedback:
            table = Table("#", "Feedback", "End", "Phase", "Corners", "Sev.", title="Rider feedback")
            for f in feedback:
                table.add_row(str(f.id), f.text, f.end or "", f.phase or "", ", ".join(f.corners),
                              _num(f.severity))
            console.print(table)

        changes = repo.list_changes(conn, session_id=s.id)
        if changes:
            console.print("[bold]Setup changes made after this session[/bold]")
            for c in changes:
                console.print(f"  #{c.id}: " + "; ".join(setup_diff.format_delta(d) for d in c.deltas))


# --- setups --------------------------------------------------------------

@setup_app.command("show")
def setup_show(setup_id: Optional[int] = typer.Argument(None, help="Default: the current bike's current setup.")) -> None:
    """Show a setup."""
    with _open() as conn:
        setup = repo.get_setup(conn, setup_id or repo.current_bike(conn).current_setup_id)
        _print_setup(setup)


@setup_app.command("set")
def setup_set(
    assignments: List[str] = typer.Argument(..., help="key=value (absolute) or key=+N / key=-N (relative)."),
    note: Optional[str] = typer.Option(None, help="Note stored with the new setup."),
    why: Optional[str] = typer.Option(None, help='What the change should fix, e.g. "less push mid corner".'),
) -> None:
    """Change the current setup. Creates a new setup; the old one is kept."""
    with _open() as conn:
        bike = repo.current_bike(conn)
        parsed = [setup_diff.parse_assignment(a) for a in assignments]
        change = repo.change_setup(conn, bike.id, parsed, note=note, hypothesis=why,
                                   session_id=_active_session_id(conn, bike.id))
        console.print(f"Setup #{change.before_setup_id} -> [bold]#{change.after_setup_id}[/bold] (change #{change.id})")
        for delta in change.deltas:
            console.print(f"  {setup_diff.format_delta(delta)}")


@setup_app.command("diff")
def setup_diff_cmd(
    a: Optional[int] = typer.Argument(None, help="Setup id (default: parent of the current setup)."),
    b: Optional[int] = typer.Argument(None, help="Setup id (default: current setup)."),
) -> None:
    """Compare two setups."""
    with _open() as conn:
        current = repo.get_setup(conn, repo.current_bike(conn).current_setup_id)
        second = repo.get_setup(conn, b) if b else current
        if a is None and second.parent_id is None:
            raise repo.RepoError(f"Setup #{second.id} has no parent to compare with. Give two setup ids.")
        first = repo.get_setup(conn, a if a is not None else second.parent_id)
        deltas = setup_diff.diff(first.params, second.params)
        if not deltas:
            console.print(f"Setups #{first.id} and #{second.id} are identical.")
            return
        table = Table("Parameter", f"#{first.id}", f"#{second.id}", "Unit", title=f"Setup #{first.id} vs #{second.id}")
        for d in deltas:
            table.add_row(setup_diff.label(d["param"]), setup_diff.format_number(d["from"]),
                          setup_diff.format_number(d["to"]), setup_diff.unit(d["param"]))
        console.print(table)


@setup_app.command("history")
def setup_history() -> None:
    """Show how the current bike's setup has evolved."""
    with _open() as conn:
        bike = repo.current_bike(conn)
        changes = {c.after_setup_id: c for c in repo.list_changes(conn, bike_id=bike.id)}
        table = Table("", "Setup", "Created", "Change", "Why / note", title=f"{bike.name} setup history")
        for s in repo.setup_history(conn, bike.id):
            change = changes.get(s.id)
            marks = ("*" if s.id == bike.current_setup_id else "") + ("B" if s.id == bike.baseline_setup_id else "")
            what = "; ".join(setup_diff.format_delta(d) for d in change.deltas) if change else ""
            why = " / ".join(x for x in (change.hypothesis if change else None, s.notes) if x)
            table.add_row(marks, f"#{s.id}", s.created_at, what, why)
        console.print(table)
        console.print("[dim]* current   B baseline[/dim]")


@setup_app.command("revert")
def setup_revert(setup_id: int = typer.Argument(..., help="Setup to go back to.")) -> None:
    """Go back to an earlier setup (recorded as a new change)."""
    with _open() as conn:
        bike = repo.current_bike(conn)
        change = repo.revert_setup(conn, bike.id, setup_id, session_id=_active_session_id(conn, bike.id))
        console.print(f"Reverted to setup #{setup_id}: new setup [bold]#{change.after_setup_id}[/bold]")
        for delta in change.deltas:
            console.print(f"  {setup_diff.format_delta(delta)}")


@setup_app.command("params")
def setup_params() -> None:
    """List the setup parameters you can set."""
    table = Table("Key", "Parameter", "Unit")
    for key, param in setup_diff.PARAMETERS.items():
        table.add_row(key, param.label, param.unit)
    table.add_row("electronics.<name>", "Any electronics setting, e.g. electronics.tc", "free")
    console.print(table)


def _print_setup(setup: Setup) -> None:
    title = f"Setup #{setup.id}" + (f" (from #{setup.parent_id})" if setup.parent_id else "")
    table = Table("Parameter", "Value", "Unit", title=title)
    for key in sorted(setup.params, key=setup_diff.sort_key):
        table.add_row(setup_diff.label(key), setup_diff.format_number(setup.params[key]), setup_diff.unit(key))
    if not setup.params:
        console.print(f"{title}: no values yet. Use [cyan]re setup set key=value[/cyan].")
        return
    console.print(table)
    if setup.notes:
        console.print(f"[dim]{setup.notes}[/dim]")


# --- laps and feedback ---------------------------------------------------

@lap_app.command("add")
def lap_add(
    times: List[str] = typer.Argument(..., help="One or more lap times, e.g. 1:42.31 1:41.98."),
    sectors: Optional[str] = typer.Option(None, help="Sector times for a single lap, comma separated."),
    invalid: bool = typer.Option(False, "--invalid", help="Mark the laps as invalid (crash, traffic, out lap)."),
    session_id: Optional[int] = typer.Option(None, "--session", help="Default: the active session."),
) -> None:
    """Add lap times to a session."""
    with _open() as conn:
        session = repo.get_session(conn, session_id) if session_id else repo.current_session(conn)
        sector_ms = [timing.parse_time(t) for t in sectors.split(",")] if sectors else None
        laps = repo.add_laps(conn, session.id, [timing.parse_time(t) for t in times], sectors_ms=sector_ms,
                             valid=not invalid)
        best = repo.best_lap(conn, session.id)
        for lap in laps:
            console.print(f"Lap {lap.lap_number}: {timing.format_time(lap.time_ms)}")
        if best:
            console.print(f"Best: lap {best.lap_number}, {timing.format_time(best.time_ms)}")


@lap_app.command("list")
def lap_list(session_id: Optional[int] = typer.Option(None, "--session", help="Default: the active session.")) -> None:
    """List laps of a session."""
    session_show(session_id)


@app.command()
def feedback(
    text: str = typer.Argument(..., help='What you feel on the bike, e.g. "front slippery mid corner T3, T7".'),
    end: Optional[str] = typer.Option(None, help="front or rear (default: detected from the text)."),
    phase: Optional[str] = typer.Option(None, help="braking, entry, mid, exit or straight (default: detected)."),
    corner: Optional[List[str]] = typer.Option(None, help="Corner, e.g. T3 (repeatable; default: detected)."),
    symptom: Optional[str] = typer.Option(None, help='Short label, e.g. "front push".'),
    severity: Optional[int] = typer.Option(None, help="1 (minor) to 5 (scary)."),
    session_id: Optional[int] = typer.Option(None, "--session", help="Default: the active session."),
) -> None:
    """Record rider feedback for a session."""
    with _open() as conn:
        session = repo.get_session(conn, session_id) if session_id else repo.current_session(conn)
        f = repo.add_feedback(conn, session.id, text, end=end, phase=phase, corners=corner, symptom=symptom,
                              severity=severity)
        tags = [f"end={f.end}" if f.end else "", f"phase={f.phase}" if f.phase else "",
                f"corners={','.join(f.corners)}" if f.corners else ""]
        console.print(f"Feedback #{f.id} saved to session #{session.id}. " + " ".join(t for t in tags if t))


def _active_session_id(conn: sqlite3.Connection, bike_id: int) -> int | None:
    """The active session, if it belongs to this bike (changes are linked to the session they followed)."""
    try:
        session = repo.current_session(conn)
    except repo.NotFound:
        return None
    return session.id if session.bike_id == bike_id else None


def _num(value: float | int | None, suffix: str = "") -> str:
    return "-" if value is None else f"{value:g}{suffix}"

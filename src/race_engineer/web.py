"""A small read-only web page over the database, served on the laptop (`re web`).

Standard library only. render() turns a path into HTML so it can be tested
without a socket; serve() runs it on 127.0.0.1. Every request opens its own
connection, so the page always shows what the CLI has just logged.

Pages: / (sessions), /session/<id>, /setups, /effects. ?bike=<id> picks a bike.
"""

from __future__ import annotations

import sqlite3
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import analysis, db, history, repo, setup_diff, timing
from .models import Bike, Session

DEFAULT_PORT = 8765


class PageNotFound(Exception):
    pass


def render(conn: sqlite3.Connection, url: str) -> tuple[int, str]:
    """(HTTP status, HTML page) for a URL path with an optional query string."""
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    path = parts.path.rstrip("/") or "/"
    try:
        bike = _bike(conn, query.get("bike", [None])[0])
        if path == "/":
            return 200, _page("Sessions", bike, conn, _sessions(conn, bike))
        if path == "/setups":
            return 200, _page("Setups", bike, conn, _setups(conn, bike))
        if path == "/effects":
            return 200, _page("What worked", bike, conn, _effects(conn, bike))
        if path.startswith("/session/") and path.removeprefix("/session/").isdigit():
            session = repo.get_session(conn, int(path.removeprefix("/session/")))
            bike = repo.get_bike(conn, session.bike_id)
            return 200, _page(f"{session.type} {session.date}", bike, conn, _session(conn, session))
        raise PageNotFound(path)
    except (PageNotFound, repo.NotFound) as error:
        message = "No such page." if isinstance(error, PageNotFound) else str(error)
        return 404, _page("Not found", None, conn, f"<p>{escape(message)}</p><p><a href='/'>Back to sessions</a></p>")


def serve(db_path: str | Path | None = None, host: str = "127.0.0.1", port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    """Create the server (call serve_forever() on it). Binds to localhost only by default."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (http.server naming)
            conn = db.connect(db_path)
            try:
                status, body = render(conn, self.path)
            finally:
                conn.close()
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args) -> None:  # keep the terminal quiet
            pass

    return ThreadingHTTPServer((host, port), Handler)


# --- pages -------------------------------------------------------------------

def _bike(conn: sqlite3.Connection, ref: str | None) -> Bike | None:
    if ref:
        return repo.get_bike(conn, ref)
    try:
        return repo.current_bike(conn)
    except repo.NotFound:
        return None


def _sessions(conn: sqlite3.Connection, bike: Bike | None) -> str:
    if bike is None:
        return "<p>No bike yet. Add one with <code>re bike add</code>.</p>"
    sessions = repo.list_sessions(conn, bike_id=bike.id, limit=200)
    if not sessions:
        return "<p>No sessions yet. Start one with <code>re session start</code>.</p>"
    rows = []
    for s in sessions:
        best = repo.best_lap(conn, s.id)
        feedback = repo.list_feedback(conn, s.id)
        rows.append([
            f"<a href='/session/{s.id}'>{escape(s.date)}</a>", escape(s.type), escape(repo.get_track(conn, s.track_id).name),
            _num(s.track_temp), f"#{s.setup_id}", str(len(repo.list_laps(conn, s.id))),
            timing.format_time(best.time_ms) if best else "", escape(feedback[0].text) if feedback else "",
        ])
    return _table(["Date", "Session", "Track", "Track °C", "Setup", "Laps", "Best", "First feedback"], rows,
                  numeric={3, 5, 6})


def _session(conn: sqlite3.Connection, s: Session) -> str:
    track = repo.get_track(conn, s.track_id)
    setup = repo.get_setup(conn, s.setup_id)
    out = [f"<p class='meta'>{escape(track.name)} · air {_num(s.air_temp)} °C · track {_num(s.track_temp)} °C"
           f"{' · ' + escape(s.weather) if s.weather else ''} · setup #{s.setup_id}</p>"]
    tires = [f"front {escape(s.tire_front or '-')}", f"rear {escape(s.tire_rear or '-')}"]
    pressures = []
    targets = repo.session_tire_targets(conn, s)
    for position, hot in (("front", s.front_hot), ("rear", s.rear_hot)):
        cold = setup.params.get(f"tire.{position}_cold")
        if cold is None and hot is None:
            continue
        text = f"{position} {setup_diff.format_number(cold)} cold" + (f" / {hot:g} hot" if hot is not None else "")
        text += " bar"
        target = targets[position]
        if hot is not None and target:
            state = "high" if hot > target.hot_max else "low" if hot < target.hot_min else "ok"
            text += f" <span class='tag {state}'>target {target.hot_min:g}-{target.hot_max:g}</span>"
        pressures.append(text)
    out.append(f"<p class='meta'>Tires: {', '.join(tires)}" + (f" · Pressures: {'; '.join(pressures)}" if pressures else "")
               + "</p>")
    if s.notes:
        out.append(f"<p>{escape(s.notes)}</p>")

    laps = repo.list_laps(conn, s.id)
    if laps:
        best = repo.best_lap(conn, s.id)
        sector_count = max(len(lap.sectors_ms) for lap in laps)
        best_sectors = [min((lap.sectors_ms[i] for lap in laps if lap.valid and len(lap.sectors_ms) > i), default=None)
                        for i in range(sector_count)]
        rows = []
        for lap in laps:
            gap = timing.format_gap(lap.time_ms - best.time_ms) if lap.valid and best else "invalid"
            time = timing.format_time(lap.time_ms)
            cells = [str(lap.lap_number), f"<b>{time}</b>" if best and lap.id == best.id else time, gap]
            for i in range(sector_count):
                if i < len(lap.sectors_ms):
                    value = timing.format_time(lap.sectors_ms[i])
                    cells.append(f"<b>{value}</b>" if lap.valid and lap.sectors_ms[i] == best_sectors[i] else value)
                else:
                    cells.append("")
            rows.append(cells)
        out.append("<h2>Laps</h2>")
        out.append(_table(["Lap", "Time", "Gap"] + [f"S{i}" for i in range(1, sector_count + 1)], rows,
                          numeric=set(range(1, 3 + sector_count)), muted={i for i, l in enumerate(laps) if not l.valid}))

        other = analysis.default_comparison(conn, s)
        result = analysis.analyse(laps, repo.list_laps(conn, other.id) if other else None)
        facts = [f"{result.valid_laps} valid laps", f"median {timing.format_time(result.median_ms)}"
                 if result.median_ms else ""]
        if result.stdev_ms is not None:
            facts.append(f"spread {result.stdev_ms / 1000:.3f} s")
        if result.theoretical_best_ms:
            facts.append(f"theoretical best {timing.format_time(result.theoretical_best_ms)}")
        if result.delta_ms is not None:
            facts.append(f"best {timing.format_gap(result.delta_ms)} vs "
                         f"<a href='/session/{other.id}'>{escape(other.type)} {escape(other.date)}</a>")
        out.append(f"<p class='meta'>{' · '.join(f for f in facts if f)}</p>")

    feedback = repo.list_feedback(conn, s.id)
    if feedback:
        out.append("<h2>Rider feedback</h2><ul>")
        for f in feedback:
            tags = " ".join(f"<span class='tag'>{escape(t)}</span>" for t in
                            (f.end, f.phase, *f.corners, f.symptom, f"severity {f.severity}" if f.severity else None) if t)
            out.append(f"<li>{escape(f.text)} {tags}</li>")
        out.append("</ul>")

    changes = repo.list_changes(conn, session_id=s.id)
    if changes:
        out.append("<h2>Changed after this session</h2>")
        out += [_change(conn, c) for c in changes]
    return "\n".join(out)


def _change(conn: sqlite3.Connection, change) -> str:
    who = "engineer" if change.proposed_by == "agent" else "rider"
    what = "; ".join(escape(setup_diff.format_delta(d)) for d in change.deltas)
    lines = [f"<div class='card'><p><b>Change #{change.id}</b> by the {who}: {what}</p>"]
    for label, value in (("Targets", change.symptom), ("Why", change.hypothesis), ("Feel for", change.watch_for)):
        if value:
            lines.append(f"<p class='meta'>{label}: {escape(value)}</p>")
    outcomes = repo.list_outcomes(conn, change.id)
    lines.append("<p>" + (", ".join(_outcome(o) for o in outcomes) or "<span class='tag'>no result yet</span>") + "</p>")
    return "\n".join(lines) + "</div>"


def _outcome(o) -> str:
    state = {"better": "ok", "same": "", "worse": "high"}[o.verdict]
    text = f"<span class='tag {state}'>{o.verdict}</span>"
    if o.lap_delta_ms is not None:
        text += f" best lap {timing.format_gap(o.lap_delta_ms)}"
    if o.symptom_resolved:
        text += ", complaint resolved"
    if o.notes:
        text += f": “{escape(o.notes)}”"
    return text


def _setups(conn: sqlite3.Connection, bike: Bike | None) -> str:
    if bike is None:
        return "<p>No bike yet.</p>"
    changes = {c.after_setup_id: c for c in repo.list_changes(conn, bike_id=bike.id)}
    out = []
    current = repo.get_setup(conn, bike.current_setup_id)
    out.append(f"<h2>Current setup #{current.id}</h2>")
    out.append(_table(["Setting", "Value", "Unit"],
                      [[escape(setup_diff.label(k)), escape(setup_diff.format_number(current.params[k])),
                        setup_diff.unit(k)] for k in sorted(current.params, key=setup_diff.sort_key)], numeric={1}))
    out.append("<h2>History</h2>")
    for setup in reversed(repo.setup_history(conn, bike.id)):
        change = changes.get(setup.id)
        marks = " ".join(m for m in ("current" if setup.id == bike.current_setup_id else "",
                                     "baseline" if setup.id == bike.baseline_setup_id else "") if m)
        badge = f" <span class='tag'>{marks}</span>" if marks else ""
        if change:
            out.append(_change(conn, change).replace("<div class='card'>", f"<div class='card'><p class='meta'>"
                                                     f"Setup #{setup.id}{badge}</p>", 1))
        else:
            out.append(f"<div class='card'><p class='meta'>Setup #{setup.id}{badge}</p>"
                       f"<p>{escape(setup.notes or 'Starting setup')}</p></div>")
    return "\n".join(out)


def _effects(conn: sqlite3.Connection, bike: Bike | None) -> str:
    if bike is None:
        return "<p>No bike yet.</p>"
    effects = history.learned_effects(conn, bike.id)
    if not effects:
        return "<p>No results recorded yet. Record how each change worked with <code>re outcome</code>.</p>"
    rows = [[escape(e.symptom or "-"), escape(f"{e.param} {setup_diff.format_number(e.typical_step, signed=True)}"),
             str(e.tries), str(e.better), str(e.same), str(e.worse),
             timing.format_gap(-e.avg_lap_delta_ms) if e.avg_lap_delta_ms is not None else ""] for e in effects]
    return _table(["Symptom", "Change", "Tries", "Better", "Same", "Worse", "Avg lap gain"], rows,
                  numeric={2, 3, 4, 5, 6})


# --- layout ------------------------------------------------------------------

def _table(header: list[str], rows: list[list[str]], numeric: set[int] = frozenset(),
           muted: set[int] = frozenset()) -> str:
    head = "".join(f"<th{' class=num' if i in numeric else ''}>{h}</th>" for i, h in enumerate(header))
    body = "".join(
        f"<tr{' class=muted' if r in muted else ''}>"
        + "".join(f"<td{' class=num' if i in numeric else ''}>{c}</td>" for i, c in enumerate(row)) + "</tr>"
        for r, row in enumerate(rows))
    return f"<div class='scroll'><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"


def _num(value: float | None) -> str:
    return "" if value is None else f"{value:g}"


def _page(title: str, bike: Bike | None, conn: sqlite3.Connection, body: str) -> str:
    bikes = repo.list_bikes(conn)
    query = f"?bike={bike.id}" if bike else ""
    switch = ""
    if len(bikes) > 1:
        switch = " · " + " ".join(
            f"<a href='/?bike={b.id}'{' class=active' if bike and b.id == bike.id else ''}>{escape(b.name)}</a>"
            for b in bikes)
    heading = escape(title) + (f" <span class='meta'>{escape(bike.name)}</span>" if bike else "")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Race Engineer</title>
<style>{_CSS}</style>
</head>
<body>
<nav><b>Race Engineer</b> <a href="/{query}">Sessions</a> <a href="/setups{query}">Setups</a>
<a href="/effects{query}">What worked</a>{switch}</nav>
<main>
<h1>{heading}</h1>
{body}
</main>
</body>
</html>
"""


_CSS = """
:root { --bg: #f7f7f5; --fg: #1d1d1b; --muted: #6b6b66; --line: #deddd8; --card: #ffffff;
        --accent: #1f5fbf; --ok: #1d7a3a; --high: #b3261e; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #161615; --fg: #ecebe6; --muted: #a09f99; --line: #34332f; --card: #1f1f1d;
          --accent: #7aa7ff; --ok: #6fcf8a; --high: #ff8a80; }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
       font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
nav { padding: 12px 16px; border-bottom: 1px solid var(--line); display: flex; gap: 14px; flex-wrap: wrap; }
nav a, main a { color: var(--accent); text-decoration: none; }
nav a.active { font-weight: 600; }
main { max-width: 1000px; margin: 0 auto; padding: 16px; }
h1 { font-size: 22px; margin: 8px 0 12px; }
h2 { font-size: 17px; margin: 24px 0 8px; }
.meta { color: var(--muted); margin: 4px 0; }
.scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; background: var(--card); }
th, td { padding: 6px 10px; border-bottom: 1px solid var(--line); text-align: left; white-space: nowrap; }
th { font-weight: 600; color: var(--muted); font-size: 13px; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
tr.muted td { color: var(--muted); }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 8px; padding: 8px 12px; margin: 8px 0; }
.card p { margin: 4px 0; }
.tag { display: inline-block; font-size: 12px; padding: 0 6px; border: 1px solid var(--line); border-radius: 10px;
       color: var(--muted); }
.tag.ok { color: var(--ok); border-color: var(--ok); }
.tag.high, .tag.low { color: var(--high); border-color: var(--high); }
code { font-size: 13px; }
"""

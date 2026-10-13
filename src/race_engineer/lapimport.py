"""Import lap times from a lap timer's CSV export (phone apps such as RaceChrono or
Harry's LapTimer, or any spreadsheet).

Exports differ between apps and versions, so nothing is hard-coded to one format:
- the delimiter (comma, semicolon or tab) and a decimal comma are detected;
- the header row is the first row with a lap column and a lap-time column, so
  title or summary lines above it are skipped;
- sector columns are "Sector 1", "S1", "Split 2" and the like;
- a status column saying out / in / pit / invalid marks the lap invalid;
- times can be m:ss.xxx, h:mm:ss.xxx or plain seconds.
Any column can be named explicitly when detection gets it wrong.

Laps much slower than the median (out laps, traffic, a run-off) are marked
invalid, so they don't count as the best lap; they are still imported.
"""

from __future__ import annotations

import csv
import re
import statistics
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

SLOW_LAP_FACTOR = 1.15  # laps slower than median x this are marked invalid
HEADER_SEARCH_ROWS = 60

_LAP_NAMES = {"lap", "lap #", "lap no", "lap nr", "lap number", "lapnumber", "lap_number", "lap num", "#", "nr", "no"}
_TIME_NAMES = ("lap time", "laptime", "lap_time", "lap duration", "duration", "time")
_NOT_TIME = ("timestamp", "time of day", "start time", "end time", "date", "gap", "delta", "diff", "best", "ideal",
             "split time")
_STATUS_NAMES = {"valid", "invalid", "status", "type", "lap type", "flag", "note", "notes", "comment", "remark"}
_SECTOR = re.compile(r"^(?:sector|sec|split|s)\s*[_#-]?\s*(\d{1,2})(?:\s*\(.*\))?(?:\s*time)?$")
_INVALID_WORDS = ("out", "in lap", "inlap", "pit", "invalid", "false", "no", "x")


class LapImportError(ValueError):
    """The file can't be read as laps."""


@dataclass
class ImportedLap:
    number: int | None  # lap number in the file, if it has one
    time_ms: int
    sectors_ms: list[int]
    valid: bool
    note: str = ""  # why it was marked invalid


@dataclass
class ImportResult:
    laps: list[ImportedLap]
    columns: dict[str, str | list[str] | None]  # what was used: lap, time, sectors, status
    skipped: list[str] = field(default_factory=list)  # rows that weren't laps, briefly


def read_file(path: str | Path, **options) -> ImportResult:
    raw = Path(path).read_bytes()
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return parse(raw.decode(encoding), **options)
        except UnicodeDecodeError:
            continue
    raise LapImportError("Can't decode the file (expected UTF-8 or Windows text).")


def parse(
    text: str,
    *,
    lap_col: str | None = None,
    time_col: str | None = None,
    sector_cols: list[str] | None = None,
    keep_slow: bool = False,
) -> ImportResult:
    lines = text.splitlines()
    if not lines:
        raise LapImportError("The file is empty.")
    delimiter = _delimiter(lines[:HEADER_SEARCH_ROWS])
    rows = list(csv.reader(lines, delimiter=delimiter))
    decimal_comma = delimiter != ","

    header_index, columns = _find_header(rows, lap_col, time_col, sector_cols)
    header = rows[header_index]
    laps, skipped = [], []
    for row in rows[header_index + 1:]:
        if not any(cell.strip() for cell in row):
            continue
        cell = _getter(row, header)
        time_ms = _time(cell(columns["time"]), decimal_comma)
        if time_ms is None or time_ms <= 0:
            skipped.append(", ".join(c for c in row if c.strip())[:60])
            continue
        number = _int(cell(columns["lap"])) if columns["lap"] else None
        sectors = [_time(cell(c), decimal_comma) for c in columns["sectors"]]
        sectors_ms = [s for s in sectors if s is not None] if all(s is not None for s in sectors) else []
        status = cell(columns["status"]).strip().lower() if columns["status"] else ""
        invalid = _status_invalid(status, columns["status"])
        laps.append(ImportedLap(number, time_ms, sectors_ms, not invalid, f"marked '{status}'" if invalid else ""))

    if not laps:
        raise LapImportError(f"Found the columns ({columns['time']}), but no lap times under them.")
    if not keep_slow:
        _mark_slow(laps)
    return ImportResult(laps, columns, skipped)


def _delimiter(lines: list[str]) -> str:
    sample = "\n".join(lines)
    counts = {d: sample.count(d) for d in (";", "\t", ",")}
    return max(counts, key=lambda d: counts[d]) if any(counts.values()) else ","


def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower().replace("_", " "))


def _find_header(rows, lap_col, time_col, sector_cols) -> tuple[int, dict]:
    for index, row in enumerate(rows[:HEADER_SEARCH_ROWS]):
        names = [_norm(c) for c in row]
        time = _pick(names, row, time_col, _is_time_name)
        lap = _pick(names, row, lap_col, lambda n: n in _LAP_NAMES)
        if time is not None and lap_col and lap is None:
            raise LapImportError(f"No column called {lap_col}. Columns: {', '.join(row)}")
        if time is None or (lap is None and lap_col is None and not _looks_like_laps(rows, index, names.index(_norm(time)))):
            continue
        if sector_cols:
            missing = [c for c in sector_cols if _norm(c) not in names]
            if missing:
                raise LapImportError(f"No column called {', '.join(missing)}. Columns: {', '.join(row)}")
            sectors = [row[names.index(_norm(c))] for c in sector_cols]
        else:
            numbered = [(int(m.group(1)), c) for n, c in zip(names, row) if (m := _SECTOR.match(n))]
            sectors = [c for _, c in sorted(numbered)]
            sectors = [c for c in sectors if c not in (time, lap)]
        status = next((c for n, c in zip(names, row) if n in _STATUS_NAMES), None)
        return index, {"lap": lap, "time": time, "sectors": sectors, "status": status}
    wanted = f"'{time_col}'" if time_col else "a lap time column (e.g. 'Lap time')"
    raise LapImportError(f"Couldn't find {wanted} in the first {HEADER_SEARCH_ROWS} rows. "
                       "Name the columns with --time-col (and --lap-col, --sector-col).")


def _pick(names: list[str], row: list[str], explicit: str | None, test) -> str | None:
    if explicit:
        return row[names.index(_norm(explicit))] if _norm(explicit) in names else None
    # Prefer the most specific name: "lap time" over "time".
    for wanted in _TIME_NAMES if test is _is_time_name else ():
        for n, c in zip(names, row):
            if n == wanted or (n.startswith(wanted) and _is_time_name(n)):
                return c
    return next((c for n, c in zip(names, row) if test(n)), None)


def _is_time_name(name: str) -> bool:
    if any(word in name for word in _NOT_TIME) or _SECTOR.match(name):
        return False
    return any(name == t or name.startswith(t + " ") or name.startswith(t + "(") for t in _TIME_NAMES)


def _looks_like_laps(rows, header_index: int, time_index: int) -> bool:
    """Without a lap number column, accept the header if the next rows hold times."""
    following = [r for r in rows[header_index + 1:header_index + 4] if len(r) > time_index]
    return bool(following) and all(_time(r[time_index], True) is not None for r in following)


def _getter(row: list[str], header: list[str]):
    def cell(column: str | None) -> str:
        if column is None:
            return ""
        index = header.index(column)
        return row[index] if index < len(row) else ""
    return cell


_TIME = re.compile(r"^(?:(\d+):)?(?:(\d+):)?(\d+(?:\.\d+)?)$")


def _time(text: str, decimal_comma: bool) -> int | None:
    text = text.strip().strip('"')
    if decimal_comma or ("," in text and "." not in text):
        text = text.replace(",", ".")
    match = _TIME.match(text)
    if not match:
        return None
    first, second, seconds = match.groups()
    hours, minutes = (first, second) if second is not None else (None, first)
    try:
        total = Decimal(seconds) + 60 * int(minutes or 0) + 3600 * int(hours or 0)
    except InvalidOperation:
        return None
    if minutes is not None and Decimal(seconds) >= 60:
        return None
    return int(total * 1000)


def _int(text: str) -> int | None:
    try:
        return int(float(text.strip()))
    except ValueError:
        return None


def _status_invalid(status: str, column: str | None) -> bool:
    if not status or column is None:
        return False
    if _norm(column) == "valid":
        return status in ("0", "false", "no", "n")
    if _norm(column) == "invalid":
        return status in ("1", "true", "yes", "y", "x")
    return any(status == w or status.startswith(w + " ") for w in _INVALID_WORDS) or "invalid" in status


def _mark_slow(laps: list[ImportedLap]) -> None:
    valid = [lap.time_ms for lap in laps if lap.valid]
    if len(valid) < 3:
        return
    limit = statistics.median(valid) * SLOW_LAP_FACTOR
    for lap in laps:
        if lap.valid and lap.time_ms > limit:
            lap.valid, lap.note = False, f"over {int((SLOW_LAP_FACTOR - 1) * 100)}% slower than the median lap"

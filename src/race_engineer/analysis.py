"""Lap and sector analysis of a session, optionally against another session.

Works on valid laps only. Sector figures use the laps that have the full set of
sectors, so a lap with missing splits never skews a sector's best or median.
"""

from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass

from . import repo
from .models import Lap, Session


@dataclass
class SectorStats:
    sector: int  # 1-based
    best_ms: int
    best_lap: int
    median_ms: int
    spread_ms: int  # median - best: how much is left on the table, on a typical lap
    compare_best_ms: int | None = None

    @property
    def delta_ms(self) -> int | None:
        """Best here minus best in the comparison session (negative = faster now)."""
        return None if self.compare_best_ms is None else self.best_ms - self.compare_best_ms


@dataclass
class SessionAnalysis:
    valid_laps: int
    best_ms: int | None
    median_ms: int | None
    stdev_ms: int | None  # lap-to-lap consistency
    theoretical_best_ms: int | None  # sum of best sectors
    sectors: list[SectorStats]
    compare_best_ms: int | None = None

    @property
    def delta_ms(self) -> int | None:
        if self.best_ms is None or self.compare_best_ms is None:
            return None
        return self.best_ms - self.compare_best_ms


def analyse(laps: list[Lap], compare: list[Lap] | None = None) -> SessionAnalysis:
    valid = [lap for lap in laps if lap.valid]
    times = [lap.time_ms for lap in valid]
    sector_laps = _full_sector_laps(valid)
    sectors = []
    if sector_laps:
        count = len(sector_laps[0].sectors_ms)
        compare_best = _best_sectors(_full_sector_laps([lap for lap in compare or [] if lap.valid]), count)
        for i in range(count):
            values = [lap.sectors_ms[i] for lap in sector_laps]
            best_lap = min(sector_laps, key=lambda lap: lap.sectors_ms[i])
            median = round(statistics.median(values))
            sectors.append(SectorStats(i + 1, best_lap.sectors_ms[i], best_lap.lap_number, median,
                                       median - best_lap.sectors_ms[i],
                                       compare_best[i] if compare_best else None))
    compare_times = [lap.time_ms for lap in compare or [] if lap.valid]
    return SessionAnalysis(
        valid_laps=len(valid),
        best_ms=min(times) if times else None,
        median_ms=round(statistics.median(times)) if times else None,
        stdev_ms=round(statistics.stdev(times)) if len(times) > 1 else None,
        theoretical_best_ms=sum(s.best_ms for s in sectors) if sectors else None,
        sectors=sectors,
        compare_best_ms=min(compare_times) if compare_times else None,
    )


def default_comparison(conn: sqlite3.Connection, session: Session) -> Session | None:
    """What a session is naturally compared with: the previous session if it ran the same
    setup; otherwise, for the first session on a new setup, the session the change was made
    after (to judge that change); otherwise the previous session."""
    previous = repo.previous_session(conn, session)
    if previous and previous.setup_id == session.setup_id:
        return previous
    changes = [c for c in repo.list_changes(conn, bike_id=session.bike_id)
               if c.after_setup_id == session.setup_id and c.session_id not in (None, session.id)]
    if changes:
        return repo.get_session(conn, changes[-1].session_id)
    return previous


def _full_sector_laps(laps: list[Lap]) -> list[Lap]:
    """Laps with the most common (non-zero) number of sectors."""
    counts = [len(lap.sectors_ms) for lap in laps if lap.sectors_ms]
    if not counts:
        return []
    count = max(set(counts), key=counts.count)
    return [lap for lap in laps if len(lap.sectors_ms) == count]


def _best_sectors(laps: list[Lap], count: int) -> list[int] | None:
    if not laps or len(laps[0].sectors_ms) != count:
        return None
    return [min(lap.sectors_ms[i] for lap in laps) for i in range(count)]

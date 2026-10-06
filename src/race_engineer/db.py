"""SQLite connection and schema migrations.

Migrations are applied in order and tracked with PRAGMA user_version, so new
phases only ever append to MIGRATIONS.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DB_ENV = "RACE_ENGINEER_DB"

MIGRATIONS: list[str] = [
    # v1: core logging tables.
    """
    CREATE TABLE state (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    CREATE TABLE bike (
        id                INTEGER PRIMARY KEY,
        name              TEXT NOT NULL UNIQUE COLLATE NOCASE,
        make              TEXT,
        model             TEXT,
        year              INTEGER,
        notes             TEXT,
        baseline_setup_id INTEGER REFERENCES setup(id),
        current_setup_id  INTEGER REFERENCES setup(id),
        created_at        TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
    );

    -- Setups are immutable snapshots; a change always inserts a new row.
    CREATE TABLE setup (
        id         INTEGER PRIMARY KEY,
        bike_id    INTEGER NOT NULL REFERENCES bike(id),
        parent_id  INTEGER REFERENCES setup(id),
        params     TEXT NOT NULL,  -- JSON object, e.g. {"fork.rebound": 12}
        notes      TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
    );
    CREATE INDEX setup_bike ON setup(bike_id);

    CREATE TABLE track (
        id        INTEGER PRIMARY KEY,
        name      TEXT NOT NULL UNIQUE COLLATE NOCASE,
        length_m  REAL,
        direction TEXT CHECK (direction IN ('cw', 'ccw')),
        notes     TEXT
    );

    CREATE TABLE corner (
        id       INTEGER PRIMARY KEY,
        track_id INTEGER NOT NULL REFERENCES track(id),
        number   INTEGER NOT NULL,
        name     TEXT,
        type     TEXT,
        UNIQUE (track_id, number)
    );

    CREATE TABLE session (
        id              INTEGER PRIMARY KEY,
        bike_id         INTEGER NOT NULL REFERENCES bike(id),
        track_id        INTEGER NOT NULL REFERENCES track(id),
        setup_id        INTEGER NOT NULL REFERENCES setup(id),
        date            TEXT NOT NULL,
        type            TEXT NOT NULL,
        air_temp        REAL,
        track_temp      REAL,
        weather         TEXT,
        tire_front      TEXT,
        tire_rear       TEXT,
        tire_front_laps INTEGER,
        tire_rear_laps  INTEGER,
        notes           TEXT,
        created_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
    );
    CREATE INDEX session_bike ON session(bike_id);
    CREATE INDEX session_track ON session(track_id);

    CREATE TABLE lap (
        id         INTEGER PRIMARY KEY,
        session_id INTEGER NOT NULL REFERENCES session(id),
        lap_number INTEGER NOT NULL,
        time_ms    INTEGER NOT NULL,
        sectors_ms TEXT NOT NULL DEFAULT '[]',  -- JSON list of ints
        valid      INTEGER NOT NULL DEFAULT 1,
        UNIQUE (session_id, lap_number)
    );

    CREATE TABLE feedback (
        id         INTEGER PRIMARY KEY,
        session_id INTEGER NOT NULL REFERENCES session(id),
        text       TEXT NOT NULL,
        "end"      TEXT CHECK ("end" IN ('front', 'rear')),
        phase      TEXT CHECK (phase IN ('braking', 'entry', 'mid', 'exit', 'straight')),
        corners    TEXT NOT NULL DEFAULT '[]',  -- JSON list, e.g. ["T3", "T7"]
        symptom    TEXT,
        severity   INTEGER CHECK (severity BETWEEN 1 AND 5),
        created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
    );
    CREATE INDEX feedback_session ON feedback(session_id);

    CREATE TABLE change (
        id              INTEGER PRIMARY KEY,
        bike_id         INTEGER NOT NULL REFERENCES bike(id),
        session_id      INTEGER REFERENCES session(id),  -- session the change was decided in
        before_setup_id INTEGER NOT NULL REFERENCES setup(id),
        after_setup_id  INTEGER NOT NULL REFERENCES setup(id),
        deltas          TEXT NOT NULL,  -- JSON list of {"param", "from", "to"}
        hypothesis      TEXT,
        watch_for       TEXT,
        reasoning       TEXT,
        proposed_by     TEXT NOT NULL CHECK (proposed_by IN ('rider', 'agent')),
        created_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
    );
    CREATE INDEX change_bike ON change(bike_id);

    CREATE TABLE outcome (
        id               INTEGER PRIMARY KEY,
        change_id        INTEGER NOT NULL REFERENCES change(id),
        session_id       INTEGER REFERENCES session(id),  -- session the change was tested in
        verdict          TEXT NOT NULL CHECK (verdict IN ('better', 'same', 'worse')),
        lap_delta_ms     INTEGER,
        symptom_resolved INTEGER,
        notes            TEXT,
        created_at       TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
    );
    CREATE INDEX outcome_change ON outcome(change_id);
    """,
]


def resolve_path(path: str | Path | None = None) -> Path:
    if path:
        return Path(path)
    if env := os.environ.get(DB_ENV):
        return Path(env)
    return Path.home() / ".race_engineer" / "race_engineer.db"


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    db_path = resolve_path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for number, sql in enumerate(MIGRATIONS[version:], start=version + 1):
        conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {number};\nCOMMIT;")

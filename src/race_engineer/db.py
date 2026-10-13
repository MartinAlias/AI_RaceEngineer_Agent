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
    # v2: per-bike parameter ranges, symptom on changes, full-text search over notes.
    """
    CREATE TABLE param_range (
        bike_id INTEGER NOT NULL REFERENCES bike(id),
        param   TEXT NOT NULL,
        min     REAL NOT NULL,
        max     REAL NOT NULL,
        CHECK (min <= max),
        PRIMARY KEY (bike_id, param)
    );

    ALTER TABLE change ADD COLUMN symptom TEXT;  -- symptom id the change targets

    -- One row per searchable note. Kept in sync by the triggers below.
    CREATE VIRTUAL TABLE notes_fts USING fts5(
        text,
        kind UNINDEXED,        -- feedback | change | outcome
        ref_id UNINDEXED,
        bike_id UNINDEXED,
        session_id UNINDEXED
    );

    CREATE TRIGGER feedback_fts_insert AFTER INSERT ON feedback BEGIN
        INSERT INTO notes_fts (text, kind, ref_id, bike_id, session_id)
        SELECT new.text || ' ' || coalesce(new.symptom, ''), 'feedback', new.id, s.bike_id, new.session_id
        FROM session s WHERE s.id = new.session_id;
    END;

    CREATE TRIGGER feedback_fts_update AFTER UPDATE ON feedback BEGIN
        DELETE FROM notes_fts WHERE kind = 'feedback' AND ref_id = old.id;
        INSERT INTO notes_fts (text, kind, ref_id, bike_id, session_id)
        SELECT new.text || ' ' || coalesce(new.symptom, ''), 'feedback', new.id, s.bike_id, new.session_id
        FROM session s WHERE s.id = new.session_id;
    END;

    CREATE TRIGGER change_fts_insert AFTER INSERT ON change BEGIN
        INSERT INTO notes_fts (text, kind, ref_id, bike_id, session_id)
        VALUES (
            coalesce(new.symptom, '') || ' ' || coalesce(new.hypothesis, '') || ' ' ||
            coalesce(new.reasoning, '') || ' ' || coalesce(new.watch_for, '') || ' ' ||
            coalesce((SELECT group_concat(json_extract(value, '$.param'), ' ') FROM json_each(new.deltas)), ''),
            'change', new.id, new.bike_id, new.session_id
        );
    END;

    CREATE TRIGGER outcome_fts_insert AFTER INSERT ON outcome BEGIN
        INSERT INTO notes_fts (text, kind, ref_id, bike_id, session_id)
        SELECT new.verdict || ' ' || coalesce(new.notes, ''), 'outcome', new.id, c.bike_id,
               coalesce(new.session_id, c.session_id)
        FROM change c WHERE c.id = new.change_id;
    END;

    -- Index anything logged before this migration.
    INSERT INTO notes_fts (text, kind, ref_id, bike_id, session_id)
    SELECT f.text || ' ' || coalesce(f.symptom, ''), 'feedback', f.id, s.bike_id, f.session_id
    FROM feedback f JOIN session s ON s.id = f.session_id;

    INSERT INTO notes_fts (text, kind, ref_id, bike_id, session_id)
    SELECT coalesce(c.hypothesis, '') || ' ' || coalesce(c.reasoning, '') || ' ' || coalesce(c.watch_for, '') || ' ' ||
           coalesce((SELECT group_concat(json_extract(value, '$.param'), ' ') FROM json_each(c.deltas)), ''),
           'change', c.id, c.bike_id, c.session_id
    FROM change c;

    INSERT INTO notes_fts (text, kind, ref_id, bike_id, session_id)
    SELECT o.verdict || ' ' || coalesce(o.notes, ''), 'outcome', o.id, c.bike_id, coalesce(o.session_id, c.session_id)
    FROM outcome o JOIN change c ON c.id = o.change_id;
    """,
    # v3: engineer conversations and the offline queue.
    """
    CREATE TABLE pending_analysis (
        feedback_id INTEGER PRIMARY KEY REFERENCES feedback(id),
        created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
    );

    CREATE TABLE engineer_conversation (
        id          INTEGER PRIMARY KEY,
        bike_id     INTEGER NOT NULL REFERENCES bike(id),
        session_id  INTEGER REFERENCES session(id),
        feedback_id INTEGER REFERENCES feedback(id),
        model       TEXT NOT NULL,
        messages    TEXT NOT NULL,  -- JSON transcript, for review
        started_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
    );
    """,
    # v4: hot tire pressures as session measurements, and tire makers' hot targets.
    """
    ALTER TABLE session ADD COLUMN front_hot REAL;  -- bar, measured right after the session
    ALTER TABLE session ADD COLUMN rear_hot REAL;

    CREATE TABLE tire_target (
        name     TEXT NOT NULL COLLATE NOCASE,  -- as entered on sessions, e.g. "SC1"
        position TEXT NOT NULL CHECK (position IN ('front', 'rear')),
        hot_min  REAL NOT NULL,                 -- bar
        hot_max  REAL NOT NULL,
        CHECK (hot_min <= hot_max),
        PRIMARY KEY (name, position)
    );

    -- Hot pressures used to be logged as setup changes after a session; copy them over.
    UPDATE session SET front_hot = (
        SELECT json_extract(d.value, '$.to') FROM change c, json_each(c.deltas) d
        WHERE c.session_id = session.id AND json_extract(d.value, '$.param') = 'tire.front_hot'
        ORDER BY c.id DESC LIMIT 1);
    UPDATE session SET rear_hot = (
        SELECT json_extract(d.value, '$.to') FROM change c, json_each(c.deltas) d
        WHERE c.session_id = session.id AND json_extract(d.value, '$.param') = 'tire.rear_hot'
        ORDER BY c.id DESC LIMIT 1);
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

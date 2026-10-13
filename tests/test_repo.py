import pytest

from race_engineer import db, repo
from race_engineer.setup_diff import parse_assignment


def test_migrations_are_idempotent(tmp_path):
    path = tmp_path / "x.db"
    db.connect(path).close()
    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    conn.close()


def test_add_bike_creates_baseline_and_selects_it(conn):
    bike = repo.add_bike(conn, "R6", baseline={"fork.rebound": 10})
    assert bike.baseline_setup_id == bike.current_setup_id
    assert repo.get_setup(conn, bike.current_setup_id).params == {"fork.rebound": 10}
    assert repo.current_bike(conn).id == bike.id
    assert repo.get_bike(conn, "r6").id == bike.id  # case-insensitive name lookup


def test_duplicate_bike_name(conn):
    repo.add_bike(conn, "R6")
    with pytest.raises(repo.RepoError, match="already exists"):
        repo.add_bike(conn, "r6")


def test_setup_changes_are_immutable_snapshots(conn, weekend):
    bike, _, session = weekend
    original = bike.current_setup_id
    change = repo.change_setup(conn, bike.id, [parse_assignment("fork.rebound=+2")],
                               hypothesis="less push mid corner", session_id=session.id)

    assert repo.get_setup(conn, original).params["fork.rebound"] == 10
    after = repo.get_setup(conn, change.after_setup_id)
    assert after.params["fork.rebound"] == 12
    assert after.parent_id == original
    assert repo.get_bike(conn, bike.id).current_setup_id == after.id
    assert change.deltas == [{"param": "fork.rebound", "from": 10, "to": 12}]
    assert change.proposed_by == "rider"
    # The session keeps the setup it was run with.
    assert repo.get_session(conn, session.id).setup_id == original


def test_no_op_change_is_rejected(conn, weekend):
    bike, _, _ = weekend
    with pytest.raises(repo.RepoError, match="Nothing changed"):
        repo.change_setup(conn, bike.id, [parse_assignment("fork.rebound=10")])


def test_revert_creates_new_setup_with_old_values(conn, weekend):
    bike, _, _ = weekend
    baseline = bike.current_setup_id
    repo.change_setup(conn, bike.id, [parse_assignment("fork.rebound=+2"), parse_assignment("fork.height=3")])
    change = repo.revert_setup(conn, bike.id, baseline)

    reverted = repo.get_setup(conn, change.after_setup_id)
    assert reverted.id != baseline
    assert reverted.params == repo.get_setup(conn, baseline).params
    assert len(repo.setup_history(conn, bike.id)) == 3


def test_next_session_uses_changed_setup(conn, weekend):
    bike, track, _ = weekend
    change = repo.change_setup(conn, bike.id, [parse_assignment("tire.front_cold=2.0")])
    fp2 = repo.start_session(conn, bike.id, track.id, type="FP2")
    assert fp2.setup_id == change.after_setup_id
    assert repo.current_session(conn).id == fp2.id


def test_laps_are_numbered_and_best_ignores_invalid(conn, weekend):
    _, _, session = weekend
    repo.add_laps(conn, session.id, [110_000, 103_500])
    repo.add_laps(conn, session.id, [101_000], valid=False)
    laps = repo.add_laps(conn, session.id, [102_900], sectors_ms=[30_000, 40_000, 32_900])

    assert laps[0].lap_number == 4
    assert laps[0].sectors_ms == [30_000, 40_000, 32_900]
    assert repo.best_lap(conn, session.id).lap_number == 4


def test_sectors_only_for_single_lap(conn, weekend):
    _, _, session = weekend
    with pytest.raises(repo.RepoError):
        repo.add_laps(conn, session.id, [100_000, 101_000], sectors_ms=[1, 2])


def test_feedback_auto_tags_unless_given(conn, weekend):
    _, _, session = weekend
    auto = repo.add_feedback(conn, session.id, "front feels slippery mid corner T3, T7", severity=3)
    assert (auto.end, auto.phase, auto.corners) == ("front", "mid", ["T3", "T7"])

    manual = repo.add_feedback(conn, session.id, "weird feeling", end="rear", phase="exit", corners=["t5"])
    assert (manual.end, manual.phase, manual.corners) == ("rear", "exit", ["T5"])
    assert len(repo.list_feedback(conn, session.id)) == 2


def test_feedback_validation(conn, weekend):
    _, _, session = weekend
    with pytest.raises(repo.RepoError):
        repo.add_feedback(conn, session.id, "x", severity=9)
    with pytest.raises(repo.RepoError):
        repo.add_feedback(conn, session.id, "x", phase="apex")


def test_outcome_lap_delta_and_pending(conn, weekend):
    bike, track, fp1 = weekend
    repo.add_laps(conn, fp1.id, [122_000, 121_955])
    change = repo.change_setup(conn, bike.id, [parse_assignment("fork.rebound=-2")], session_id=fp1.id)
    assert [c.id for c in repo.changes_without_outcome(conn, bike.id)] == [change.id]

    fp2 = repo.start_session(conn, bike.id, track.id, type="FP2")
    assert repo.lap_delta(conn, change, fp2.id) is None  # no laps yet
    repo.add_laps(conn, fp2.id, [121_388])
    assert repo.lap_delta(conn, change, fp2.id) == -567
    assert repo.lap_delta(conn, change, fp1.id) is None  # same session: no comparison

    repo.add_outcome(conn, change.id, "better", session_id=fp2.id, lap_delta_ms=-567, symptom_resolved=True)
    assert repo.changes_without_outcome(conn, bike.id) == []
    with pytest.raises(repo.RepoError):
        repo.add_outcome(conn, change.id, "great")


def test_update_feedback_keeps_unset_fields(conn, weekend):
    _, _, session = weekend
    f = repo.add_feedback(conn, session.id, "front feels vague T3")
    updated = repo.update_feedback(conn, f.id, phase="entry", symptom="front_push_entry")
    assert (updated.end, updated.phase, updated.corners, updated.symptom) == (
        "front", "entry", ["T3"], "front_push_entry")


def test_use_bike_clears_session_of_other_bike(conn, weekend):
    repo.add_bike(conn, "Spare")
    repo.use_bike(conn, "Spare")
    with pytest.raises(repo.NotFound):
        repo.current_session(conn)


def test_hot_pressures_are_checked_against_cold(conn, weekend):
    _, _, session = weekend  # runs front cold 2.1, no rear cold recorded
    with pytest.raises(repo.RepoError, match="not above the 2.1 bar cold"):
        repo.record_hot_pressures(conn, session.id, front=1.93)
    with pytest.raises(repo.RepoError, match="not a believable"):
        repo.record_hot_pressures(conn, session.id, rear=28)
    with pytest.raises(repo.RepoError, match="front and/or rear"):
        repo.record_hot_pressures(conn, session.id)

    saved = repo.record_hot_pressures(conn, session.id, front=2.45)
    saved = repo.record_hot_pressures(conn, session.id, rear=1.9)  # front kept
    assert (saved.front_hot, saved.rear_hot) == (2.45, 1.9)
    assert repo.record_hot_pressures(conn, session.id, front=2.0, force=True).front_hot == 2.0


def test_tire_targets(conn, weekend):
    _, _, session = weekend
    conn.execute("UPDATE session SET tire_front = 'SC1' WHERE id = ?", (session.id,))
    repo.set_tire_target(conn, "sc1", "front", 2.3, 2.5)
    repo.set_tire_target(conn, "SC1", "front", 2.35, 2.55)  # replaces, name is case-insensitive
    targets = repo.session_tire_targets(conn, repo.get_session(conn, session.id))
    assert (targets["front"].hot_min, targets["front"].hot_max, targets["rear"]) == (2.35, 2.55, None)
    assert len(repo.list_tire_targets(conn)) == 1
    with pytest.raises(repo.RepoError):
        repo.set_tire_target(conn, "SC1", "middle", 2, 3)


def test_change_already_on_the_bike_for_a_session(conn, weekend):
    bike, track, fp1 = weekend
    fp2 = repo.start_session(conn, bike.id, track.id, type="FP2", date=fp1.date)
    assert repo.previous_session(conn, fp2).id == fp1.id
    assert repo.previous_session(conn, fp1) is None

    change = repo.change_setup(conn, bike.id, [parse_assignment("tire.front_cold=2.2")], ran_in_session_id=fp2.id)
    assert change.session_id == fp1.id  # results compare against FP1
    assert repo.get_session(conn, fp2.id).setup_id == change.after_setup_id

    fp3 = repo.start_session(conn, bike.id, track.id, type="FP3", date=fp1.date)
    repo.change_setup(conn, bike.id, [parse_assignment("fork.rebound=-1")])  # change for the next session
    with pytest.raises(repo.RepoError, match="ran setup"):
        repo.change_setup(conn, bike.id, [parse_assignment("fork.rebound=-1")], ran_in_session_id=fp3.id)


def test_migration_v4_moves_hot_pressures_to_sessions(tmp_path):
    import json
    import sqlite3
    path = tmp_path / "v3.db"
    raw = sqlite3.connect(path)
    for sql in db.MIGRATIONS[:3]:
        raw.executescript(sql)
    raw.executescript(f"""
        PRAGMA user_version = 3;
        INSERT INTO bike (id, name) VALUES (1, 'R6');
        INSERT INTO setup (id, bike_id, params) VALUES (1, 1, '{{}}'), (2, 1, '{{"tire.front_hot": 2.4}}');
        INSERT INTO track (id, name) VALUES (1, 'Brno');
        INSERT INTO session (id, bike_id, track_id, setup_id, date, type) VALUES (1, 1, 1, 1, '2026-01-01', 'FP');
        INSERT INTO change (bike_id, session_id, before_setup_id, after_setup_id, deltas, proposed_by)
        VALUES (1, 1, 1, 2, '{json.dumps([{"param": "tire.front_hot", "from": None, "to": 2.4}])}', 'rider');
    """)
    raw.close()
    conn = db.connect(path)
    session = repo.get_session(conn, 1)
    assert (session.front_hot, session.rear_hot) == (2.4, None)
    conn.close()

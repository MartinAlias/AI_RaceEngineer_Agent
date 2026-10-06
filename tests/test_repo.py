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


def test_use_bike_clears_session_of_other_bike(conn, weekend):
    repo.add_bike(conn, "Spare")
    repo.use_bike(conn, "Spare")
    with pytest.raises(repo.NotFound):
        repo.current_session(conn)

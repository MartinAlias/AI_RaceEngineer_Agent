import pytest

from race_engineer import advisor, db, history, repo
from race_engineer.setup_diff import parse_assignment


def _session_with_push(conn, bike, track, *, track_temp, rebound_change, verdict, date):
    """Log a session with a mid-corner push, a fork-rebound change, and its result in the next session."""
    session = repo.start_session(conn, bike.id, track.id, type="FP", date=date, track_temp=track_temp,
                                 tire_front="SC1")
    repo.add_feedback(conn, session.id, "front slides mid corner")
    change = repo.change_setup(conn, bike.id, [parse_assignment(f"fork.rebound={rebound_change:+d}")],
                               session_id=session.id, symptom="front_push_mid", hypothesis="hold the front down")
    test = repo.start_session(conn, bike.id, track.id, type="FP", date=date, track_temp=track_temp)
    repo.add_outcome(conn, change.id, verdict, session_id=test.id, lap_delta_ms=-200 if verdict == "better" else 50,
                     symptom_resolved=verdict == "better", notes=f"front {verdict} after rebound change")
    return change


def test_fts_query_is_forgiving():
    assert history.fts_query("The front feels SLIPPERY mid-corner") == '"fron"* OR "slipp"* OR "mid"* OR "corn"*'
    assert history.fts_query("the a to") is None


def test_search_finds_notes_and_filters(conn, weekend):
    bike, track, session = weekend
    repo.add_feedback(conn, session.id, "front feels slippery mid corner T3")
    other = repo.add_track(conn, "Mugello")
    hot = repo.start_session(conn, bike.id, other.id, type="FP1", track_temp=55, tire_front="SC2")
    repo.add_feedback(conn, hot.id, "front sliding in the fast corners")

    assert {h.track for h in history.search(conn, bike.id, "front slipping")} == {"Brno", "Mugello"}
    assert [h.track for h in history.search(conn, bike.id, "front", track_id=other.id)] == ["Mugello"]
    assert [h.track for h in history.search(conn, bike.id, "front", track_temp=40)] == ["Brno"]
    assert [h.track for h in history.search(conn, bike.id, "front", tire="sc2")] == ["Mugello"]
    assert [h.track for h in history.search(conn, bike.id, "front", exclude_session_id=hot.id)] == ["Brno"]


def test_search_includes_change_reasoning_with_result(conn, weekend):
    bike, track, _ = weekend
    change = _session_with_push(conn, bike, track, track_temp=38, rebound_change=-2, verdict="better",
                                date="2026-06-12")
    hits = history.search(conn, bike.id, "hold the front")
    change_hit = next(h for h in hits if h.kind == "change")
    assert change_hit.ref_id == change.id
    assert "fork.rebound 10 -> 8 (-2)" in change_hit.detail and "better" in change_hit.detail


def test_search_is_scoped_to_bike(conn, weekend):
    bike, _, session = weekend
    repo.add_feedback(conn, session.id, "front chatter")
    spare = repo.add_bike(conn, "Spare")
    assert history.search(conn, spare.id, "chatter") == []


def test_learned_effects_aggregate_outcomes(conn, weekend):
    bike, track, _ = weekend
    _session_with_push(conn, bike, track, track_temp=38, rebound_change=-2, verdict="better", date="2026-06-12")
    _session_with_push(conn, bike, track, track_temp=40, rebound_change=-1, verdict="better", date="2026-06-13")
    _session_with_push(conn, bike, track, track_temp=52, rebound_change=-1, verdict="same", date="2026-07-01")
    _session_with_push(conn, bike, track, track_temp=39, rebound_change=+3, verdict="worse", date="2026-07-02")

    effects = history.learned_effects(conn, bike.id, symptom="front_push_mid")
    more_damping = history.effect_for(effects, "fork.rebound", -1)
    assert (more_damping.tries, more_damping.better, more_damping.same) == (3, 2, 1)
    assert more_damping.resolved == 2
    assert more_damping.typical_step == -1
    assert more_damping.avg_lap_delta_ms == round((-200 - 200 + 50) / 3)
    assert history.effect_for(effects, "fork.rebound", +1).worse == 1

    hot_only = history.learned_effects(conn, bike.id, symptom="front_push_mid", track_temp=52, temp_window=3)
    assert [e.tries for e in hot_only] == [1]


def test_combined_changes_are_flagged(conn, weekend):
    bike, _, session = weekend
    change = repo.change_setup(conn, bike.id, [parse_assignment("fork.rebound=-1"),
                                               parse_assignment("tire.front_cold=-0.1")], symptom="front_push_mid")
    repo.add_outcome(conn, change.id, "better", session_id=session.id)
    effects = history.learned_effects(conn, bike.id)
    assert len(effects) == 2
    assert all(e.combined == 1 for e in effects)


def test_change_range_check_and_force(conn, weekend):
    bike, _, _ = weekend
    repo.set_param_range(conn, bike.id, "fork.rebound", 0, 11)
    with pytest.raises(repo.RangeError, match="outside the allowed range 0 to 11"):
        repo.change_setup(conn, bike.id, [parse_assignment("fork.rebound=+2")])
    change = repo.change_setup(conn, bike.id, [parse_assignment("fork.rebound=+2")], force=True)
    assert change.deltas[0]["to"] == 12
    # An existing out-of-range value doesn't block changing something else.
    repo.change_setup(conn, bike.id, [parse_assignment("fork.compression=-1")])


def test_bike_range_overrides_default(conn, weekend):
    bike, _, _ = weekend
    repo.set_param_range(conn, bike.id, "tire.front_cold", 1.9, 2.3)
    assert repo.get_param_ranges(conn, bike.id)["tire.front_cold"] == (1.9, 2.3)
    repo.clear_param_range(conn, bike.id, "tire.front_cold")
    assert repo.get_param_ranges(conn, bike.id)["tire.front_cold"] == (1.5, 2.8)


def test_baseline_outside_default_range_needs_force(conn):
    with pytest.raises(repo.RangeError):
        repo.add_bike(conn, "Odd", baseline={"tire.front_cold": 3.5})
    assert repo.add_bike(conn, "Odd", baseline={"tire.front_cold": 3.5}, force=True)


def test_feedback_gets_symptom_from_guide(conn, weekend):
    _, _, session = weekend
    assert repo.add_feedback(conn, session.id, "front feels slippery mid corner").symptom == "front_push_mid"
    assert repo.add_feedback(conn, session.id, "it rained").symptom is None
    assert repo.latest_symptom(conn, session.id) == "front_push_mid"


def test_advise_uses_setup_ranges_and_history(conn, weekend):
    bike, track, _ = weekend
    _session_with_push(conn, bike, track, track_temp=38, rebound_change=-2, verdict="better", date="2026-06-12")
    repo.set_param_range(conn, bike.id, "fork.compression", 0, 13)  # current is 12: +2 would exceed

    session = repo.start_session(conn, bike.id, track.id, type="FP3", track_temp=39)
    feedback = repo.add_feedback(conn, session.id, "front feels slippery mid corner T3")
    advice = advisor.advise(conn, feedback.id)

    top = advice.symptoms[0]
    assert top.match.symptom.id == "front_push_mid"
    rebound = next(s for s in top.suggestions if s.adjustment.param == "fork.rebound")
    assert (rebound.current, rebound.proposed, rebound.problem) == (8, 6, None)
    assert rebound.track_record.better == 1
    height = next(s for s in top.suggestions if s.adjustment.param == "fork.height")
    assert height.problem == "not in your current setup"
    assert any(h.kind == "feedback" for h in advice.similar)
    assert all(h.session_id != session.id for h in advice.similar)


def test_advise_with_entry_symptom_flags_out_of_range(conn, weekend):
    bike, _, session = weekend
    repo.set_param_range(conn, bike.id, "fork.compression", 0, 13)
    feedback = repo.add_feedback(conn, session.id, "front tucks on the brakes at turn in")
    advice = advisor.advise(conn, feedback.id)
    softer = next(s for s in advice.symptoms[0].suggestions
                  if s.adjustment.param == "fork.compression" and s.adjustment.step > 0)
    assert "outside the allowed range" in softer.problem


def test_migration_v2_indexes_existing_notes(tmp_path):
    """Data logged before v2 becomes searchable after upgrading."""
    path = tmp_path / "old.db"
    conn = db.connect(path)
    conn.executescript("PRAGMA user_version = 1;")  # pretend v2 never ran...
    conn.close()

    import sqlite3
    raw = sqlite3.connect(path)
    raw.executescript("""
        DROP TABLE pending_analysis; DROP TABLE engineer_conversation;
        DROP TABLE notes_fts; DROP TABLE param_range;
        DROP TRIGGER feedback_fts_insert; DROP TRIGGER feedback_fts_update;
        DROP TRIGGER change_fts_insert; DROP TRIGGER outcome_fts_insert;
        ALTER TABLE change DROP COLUMN symptom;
        DROP TABLE tire_target; ALTER TABLE session DROP COLUMN front_hot; ALTER TABLE session DROP COLUMN rear_hot;
        INSERT INTO bike (id, name) VALUES (1, 'R6');
        INSERT INTO setup (id, bike_id, params) VALUES (1, 1, '{}');
        INSERT INTO track (id, name) VALUES (1, 'Brno');
        INSERT INTO session (id, bike_id, track_id, setup_id, date, type) VALUES (1, 1, 1, 1, '2026-01-01', 'FP');
        INSERT INTO feedback (session_id, text) VALUES (1, 'rear pumping on exit');
    """)
    raw.close()

    conn = db.connect(path)  # ...so it runs now
    assert [h.kind for h in history.search(conn, 1, "pumping")] == ["feedback"]
    conn.close()

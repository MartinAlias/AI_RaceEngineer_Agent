"""End-to-end run of a practice session through the CLI."""

from typer.testing import CliRunner

from race_engineer.cli import app, console

runner = CliRunner()
console.width = 200  # keep table cells on one line so substrings can be asserted


def run(db_path, *args):
    result = runner.invoke(app, ["--db", str(db_path), *args])
    assert result.exit_code == 0, result.output
    return result.output


def test_practice_session_flow(tmp_path):
    db_path = tmp_path / "cli.db"
    run(db_path, "init", "--profile", "late braker, likes a stiff front")
    run(db_path, "bike", "add", "R6", "--make", "Yamaha", "--year", "2019",
        "--set", "fork.rebound=10", "--set", "tire.front_cold=2.1")
    run(db_path, "track", "add", "Brno", "--direction", "cw", "--corner", "3:Hairpin:slow hairpin")
    assert "Session #1 started" in run(db_path, "session", "start", "--track", "brno", "--type", "FP1",
                                       "--track-temp", "38")

    out = run(db_path, "lap", "add", "1:43.10", "1:42.31")
    assert "Best: lap 2, 1:42.310" in out

    out = run(db_path, "feedback", "front feels slippery mid corner T3", "--severity", "3")
    assert "end=front" in out and "phase=mid" in out and "corners=T3" in out

    out = run(db_path, "setup", "set", "fork.rebound=+2", "--why", "less push mid corner")
    assert "fork.rebound 10 -> 12 (+2)" in out

    assert "Fork rebound" in run(db_path, "setup", "diff")
    assert "less push mid corner" in run(db_path, "setup", "history")

    out = run(db_path, "session", "show")
    assert "1:42.310" in out and "slippery" in out


def test_guide_advise_and_history(tmp_path):
    db_path = tmp_path / "cli2.db"
    run(db_path, "bike", "add", "R6", "--set", "fork.rebound=10", "--set", "tire.front_cold=2.1")
    run(db_path, "track", "add", "Brno")
    run(db_path, "session", "start", "--track", "Brno", "--track-temp", "38")
    out = run(db_path, "feedback", "front feels slippery mid corner [T3]")
    assert "symptom=front_push_mid" in out

    assert "front_push_mid" in run(db_path, "guide")
    assert "Fork rebound too fast" in run(db_path, "guide", "front_push_mid")

    out = run(db_path, "advise")
    assert "Front slides / feels slippery mid corner" in out
    assert "fork.rebound 10 -> 8 (-2)" in out
    assert "[T3]" in out  # user text is not parsed as markup

    run(db_path, "bike", "range", "fork.rebound", "0", "9")
    result = runner.invoke(app, ["--db", str(db_path), "setup", "set", "fork.rebound=12"])
    assert result.exit_code == 1 and "outside the allowed range" in result.output
    out = run(db_path, "setup", "set", "fork.rebound=-2", "--why", "hold the front down")
    assert "Targets: front_push_mid" in out

    assert "change #1: fork.rebound 10 -> 8" in run(db_path, "history", "search", "hold front")
    assert "No results recorded yet" in run(db_path, "history", "effects")


def test_outcome_flow(tmp_path):
    db_path = tmp_path / "cli3.db"
    run(db_path, "bike", "add", "R6", "--set", "fork.rebound=10")
    run(db_path, "track", "add", "Brno")
    run(db_path, "session", "start", "--track", "Brno", "--type", "FP1", "--track-temp", "38")
    run(db_path, "lap", "add", "2:01.955")
    run(db_path, "feedback", "front feels slippery mid corner")
    run(db_path, "setup", "set", "fork.rebound=-2")
    run(db_path, "session", "start", "--track", "Brno", "--type", "FP2", "--track-temp", "41")
    run(db_path, "lap", "add", "2:01.388")

    out = run(db_path, "outcome", "better", "--resolved", "--notes", "front stays planted")
    assert "Change #1 (fork.rebound 10 -> 8 (-2)): better, complaint resolved" in out
    assert "Best lap -0.567 vs FP1" in out

    assert "better (-0.567)" in run(db_path, "setup", "history")
    out = run(db_path, "history", "effects")
    assert "front_push_mid" in out and "fork.rebound -2" in out

    result = runner.invoke(app, ["--db", str(db_path), "outcome", "same"])
    assert result.exit_code == 1 and "No changes waiting" in result.output


def test_engineer_offline_queues_and_advises(tmp_path, monkeypatch):
    from race_engineer import offline
    monkeypatch.setattr(offline, "is_online", lambda timeout=3.0: False)
    db_path = tmp_path / "cli4.db"
    run(db_path, "bike", "add", "R6", "--set", "fork.rebound=10")
    run(db_path, "track", "add", "Brno")
    run(db_path, "session", "start", "--track", "Brno")
    run(db_path, "feedback", "front feels slippery mid corner")

    out = run(db_path, "engineer")
    assert "No connection" in out and "Feedback #1 is queued" in out
    assert "fork.rebound 10 -> 8 (-2)" in out  # offline advice shown

    out = run(db_path, "analyze-pending")
    assert "Feedback #1 is queued" in out  # still offline: stays queued


def test_errors_are_reported_without_traceback(tmp_path):
    result = runner.invoke(app, ["--db", str(tmp_path / "e.db"), "lap", "add", "1:42.3"])
    assert result.exit_code == 1
    assert "No active session" in result.output

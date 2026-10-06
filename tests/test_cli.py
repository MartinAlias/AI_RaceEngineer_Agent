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


def test_errors_are_reported_without_traceback(tmp_path):
    result = runner.invoke(app, ["--db", str(tmp_path / "e.db"), "lap", "add", "1:42.3"])
    assert result.exit_code == 1
    assert "No active session" in result.output

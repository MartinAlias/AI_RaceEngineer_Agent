import csv
import io

import pytest

from race_engineer import repo, report
from race_engineer.setup_diff import parse_assignment


@pytest.fixture
def race_weekend(conn, weekend):
    """FP1 with a complaint, a change, FP2 with its result; plus an older weekend elsewhere."""
    bike, brno, fp1 = weekend  # 2026-06-12
    other = repo.add_track(conn, "Most")
    old = repo.start_session(conn, bike.id, other.id, type="Race", date="2026-05-01")
    repo.add_laps(conn, old.id, [95_000])

    repo.add_laps(conn, fp1.id, [122_000, 121_955])
    repo.add_laps(conn, fp1.id, [130_000], valid=False)
    repo.add_feedback(conn, fp1.id, "front feels slippery mid corner T3 | worst", severity=3)
    repo.record_hot_pressures(conn, fp1.id, front=2.5)
    change = repo.change_setup(conn, bike.id, [parse_assignment("fork.rebound=-2")], session_id=fp1.id,
                               proposed_by="agent", hypothesis="front unloads at brake release",
                               symptom="front_push_mid")
    fp2 = repo.start_session(conn, bike.id, brno.id, type="FP2", date="2026-06-13", track_temp=41)
    repo.add_laps(conn, fp2.id, [121_388], sectors_ms=[40_000, 41_000, 40_388])
    repo.add_outcome(conn, change.id, "better", session_id=fp2.id, lap_delta_ms=-567, symptom_resolved=True,
                     notes="front planted")
    return bike, fp1, fp2, change


def test_default_weekend_is_latest_track_and_days(conn, race_weekend):
    bike, fp1, fp2, _ = race_weekend
    weekend = report.build(conn, bike.id)
    assert [s.session.id for s in weekend.sessions] == [fp1.id, fp2.id]  # not the May race at Most
    assert (weekend.date_from, weekend.date_to) == ("2026-06-10", "2026-06-13")

    everything = report.build(conn, bike.id, date_from="2026-01-01", date_to="2026-12-31")
    assert len(everything.sessions) == 3 and everything.tracks == ["Most", "Brno"]

    earlier = report.build(conn, bike.id, date_to="2026-05-31")
    assert [s.session.type for s in earlier.sessions] == ["Race"]


def test_markdown(conn, race_weekend):
    bike = race_weekend[0]
    text = report.to_markdown(report.build(conn, bike.id))
    assert text.startswith("# R6 at Brno, 2026-06-12 to 2026-06-13")
    assert "| FP1 | 2026-06-12 | 38 | #1 | 3 | 2:01.955 | +0.567 |" in text
    assert "| FP2 | 2026-06-13 | 41 | #2 | 1 | 2:01.388 | +0.000 |" in text
    assert "| Fork rebound | 10 | clicks out |" in text  # starting setup
    assert "**Change #1** after FP1 (2026-06-12), by the engineer: fork.rebound 10 -> 8 (-2)" in text
    assert '- Result: better, best lap -0.567, complaint resolved: "front planted"' in text
    assert "| 3 | 2:10.000 | invalid |" in text
    assert "| 1 | 2:01.388 | +0.000 | 40.000 | 41.000 | 40.388 |" in text
    assert "- Pressures: front 2.1 cold / 2.5 hot bar" in text
    assert '"front feels slippery mid corner T3 | worst" (front, mid, T3, front_push_mid, severity 3)' in text


def test_csv(conn, race_weekend):
    bike = race_weekend[0]
    rows = list(csv.DictReader(io.StringIO(report.to_csv(report.build(conn, bike.id)))))
    assert len(rows) == 4
    assert rows[1]["time_s"] == "121.955" and rows[1]["gap_to_session_best_s"] == "0.0"
    assert rows[2]["valid"] == "0" and rows[2]["gap_to_session_best_s"] == ""
    assert (rows[3]["session"], rows[3]["s3_s"], rows[0]["s1_s"]) == ("FP2", "40.388", "")


def test_errors(conn, weekend):
    bike = weekend[0]
    with pytest.raises(repo.NotFound):
        report.build(conn, bike.id, date_from="2020-01-01", date_to="2020-01-02")
    with pytest.raises(repo.RepoError, match="after"):
        report.build(conn, bike.id, date_from="2026-07-01", date_to="2026-06-01")
    with pytest.raises(repo.RepoError, match="look like"):
        report.build(conn, bike.id, date_to="13.10.2026")

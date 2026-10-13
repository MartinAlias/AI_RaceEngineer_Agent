from race_engineer import analysis, repo
from race_engineer.models import Lap
from race_engineer.setup_diff import parse_assignment


def lap(number, time_ms, sectors=(), valid=True):
    return Lap(id=number, session_id=1, lap_number=number, time_ms=time_ms, sectors_ms=list(sectors), valid=valid)


def test_analyse_laps_and_sectors():
    laps = [
        lap(1, 135_000, [45_000, 45_000, 45_000], valid=False),  # out lap: ignored
        lap(2, 122_000, [40_500, 41_000, 40_500]),
        lap(3, 121_500, [40_000, 41_200, 40_300]),
        lap(4, 121_800, [40_400, 40_900, 40_500]),
        lap(5, 122_400),  # no sectors: counts for laps, not sectors
    ]
    compare = [lap(1, 122_300, [40_200, 41_300, 40_800]), lap(2, 122_100, [40_600, 41_100, 40_400])]
    result = analysis.analyse(laps, compare)
    assert (result.valid_laps, result.best_ms, result.median_ms) == (4, 121_500, 121_900)
    assert result.stdev_ms == 377
    assert result.theoretical_best_ms == 40_000 + 40_900 + 40_300
    assert result.delta_ms == 121_500 - 122_100
    s1, s2, s3 = result.sectors
    assert (s1.best_ms, s1.best_lap, s1.median_ms, s1.spread_ms) == (40_000, 3, 40_400, 400)
    assert (s2.best_lap, s2.delta_ms) == (4, 40_900 - 41_100)
    assert s3.delta_ms == 40_300 - 40_400


def test_analyse_without_laps_or_comparison():
    empty = analysis.analyse([])
    assert (empty.valid_laps, empty.best_ms, empty.sectors, empty.delta_ms) == (0, None, [], None)
    one = analysis.analyse([lap(1, 121_000, [60_000, 61_000])])
    assert one.stdev_ms is None and one.sectors[0].delta_ms is None


def test_default_comparison(conn, weekend):
    bike, track, fp1 = weekend
    fp2 = repo.start_session(conn, bike.id, track.id, type="FP2", date=fp1.date)
    assert analysis.default_comparison(conn, fp2).id == fp1.id  # no change: previous session
    assert analysis.default_comparison(conn, fp1) is None

    repo.change_setup(conn, bike.id, [parse_assignment("fork.rebound=-2")], session_id=fp1.id)
    fp3 = repo.start_session(conn, bike.id, track.id, type="FP3", date=fp1.date)
    assert analysis.default_comparison(conn, fp3).id == fp1.id  # judge the change made after FP1


def test_default_comparison_prefers_same_setup(conn, weekend):
    bike, track, fp1 = weekend
    repo.change_setup(conn, bike.id, [parse_assignment("fork.rebound=-2")], session_id=fp1.id)
    fp2 = repo.start_session(conn, bike.id, track.id, type="FP2", date=fp1.date)
    fp3 = repo.start_session(conn, bike.id, track.id, type="FP3", date=fp1.date)
    assert analysis.default_comparison(conn, fp2).id == fp1.id  # first on the new setup: judge the change
    assert analysis.default_comparison(conn, fp3).id == fp2.id  # same setup as FP2

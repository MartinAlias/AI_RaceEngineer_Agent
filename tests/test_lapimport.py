"""CSV lap import. The sample files are illustrative shapes of lap timer exports, not
verbatim exports from a specific app version: replace them with real files when available."""

import pytest

from race_engineer import lapimport, repo

COMMA_WITH_TITLE = """\
Session export,Brno,2026-06-12
Best lap,2:01.388

Lap,Lap time,Sector 1,Sector 2,Sector 3,Status
1,2:15.900,45.100,46.000,44.800,Out lap
2,2:02.010,40.500,41.010,40.500,
3,2:01.388,40.100,41.000,40.288,
4,2:01.900,40.300,41.200,40.400,
5,2:14.500,44.000,46.000,44.500,In lap
"""

SEMICOLON_DECIMAL_COMMA = """\
Lap;Laptime;S1;S2
1;121,955;60,100;61,855
2;121,388;59,900;61,488
3;122,500;60,500;62,000
"""

SECONDS_NO_LAP_COLUMN = """\
Time (s),Max speed
121.955,265
121.388,268
122.010,266
"""

HMS_AND_VALID_COLUMN = """\
Lap number\tDuration\tValid\tTimestamp
1\t00:02:01.955\t1\t10:01:00
2\t00:02:01.388\t0\t10:03:02
3\t00:02:02.100\t1\t10:05:04
"""


def test_title_rows_status_and_sectors():
    result = lapimport.parse(COMMA_WITH_TITLE)
    assert result.columns == {"lap": "Lap", "time": "Lap time", "sectors": ["Sector 1", "Sector 2", "Sector 3"],
                              "status": "Status"}
    assert [(lap.number, lap.time_ms, lap.valid) for lap in result.laps] == [
        (1, 135_900, False), (2, 122_010, True), (3, 121_388, True), (4, 121_900, True), (5, 134_500, False)]
    assert result.laps[2].sectors_ms == [40_100, 41_000, 40_288]
    assert result.laps[0].note == "marked 'out lap'"


def test_semicolon_and_decimal_comma():
    result = lapimport.parse(SEMICOLON_DECIMAL_COMMA)
    assert [lap.time_ms for lap in result.laps] == [121_955, 121_388, 122_500]
    assert result.laps[1].sectors_ms == [59_900, 61_488]


def test_plain_seconds_without_lap_numbers():
    result = lapimport.parse(SECONDS_NO_LAP_COLUMN)
    assert result.columns["lap"] is None and result.columns["time"] == "Time (s)"
    assert [lap.number for lap in result.laps] == [None, None, None]
    assert result.laps[1].time_ms == 121_388


def test_hours_tabs_and_valid_column():
    result = lapimport.parse(HMS_AND_VALID_COLUMN)
    assert result.columns["time"] == "Duration"  # not Timestamp
    assert [(lap.time_ms, lap.valid) for lap in result.laps] == [(121_955, True), (121_388, False), (122_100, True)]


def test_slow_laps_marked_unless_kept():
    text = "Lap,Lap time\n1,2:30.000\n2,2:01.000\n3,2:01.500\n4,2:02.000\n"
    assert [lap.valid for lap in lapimport.parse(text).laps] == [False, True, True, True]
    assert all(lap.valid for lap in lapimport.parse(text, keep_slow=True).laps)


def test_explicit_columns():
    text = "Round,Result,A,B\n1,2:01.000,60.0,61.0\n2,2:00.500,59.9,60.6\n"
    with pytest.raises(lapimport.LapImportError, match="--time-col"):
        lapimport.parse(text)
    result = lapimport.parse(text, lap_col="Round", time_col="Result", sector_cols=["A", "B"])
    assert [lap.number for lap in result.laps] == [1, 2] and result.laps[1].sectors_ms == [59_900, 60_600]
    with pytest.raises(lapimport.LapImportError, match="No column called C"):
        lapimport.parse(text, time_col="Result", sector_cols=["A", "C"])


def test_no_laps_under_the_header():
    with pytest.raises(lapimport.LapImportError, match="no lap times"):
        lapimport.parse("Lap,Lap time\nBest,-\n")


def test_import_into_session(conn, weekend, tmp_path):
    session = weekend[2]
    path = tmp_path / "export.csv"
    path.write_bytes(COMMA_WITH_TITLE.encode("cp1252"))
    result = lapimport.read_file(path)
    laps = repo.import_laps(conn, session.id, [(lap.time_ms, lap.sectors_ms, lap.valid) for lap in result.laps])
    assert [lap.lap_number for lap in laps] == [1, 2, 3, 4, 5]
    assert repo.best_lap(conn, session.id).time_ms == 121_388

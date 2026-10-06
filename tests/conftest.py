import pytest

from race_engineer import db, repo


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "test.db")
    yield connection
    connection.close()


@pytest.fixture
def weekend(conn):
    """A bike, a track and an active session."""
    bike = repo.add_bike(conn, "R6", make="Yamaha", model="YZF-R6", year=2019,
                         baseline={"fork.rebound": 10, "fork.compression": 12, "tire.front_cold": 2.1})
    track = repo.add_track(conn, "Brno", length_m=5403, direction="cw",
                           corners=[(1, None, "fast right"), (3, "Hairpin", "slow hairpin")])
    session = repo.start_session(conn, bike.id, track.id, type="FP1", date="2026-06-12", track_temp=38)
    return bike, track, session

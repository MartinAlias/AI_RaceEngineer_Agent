import threading
import urllib.request

from race_engineer import repo, web
from race_engineer.setup_diff import parse_assignment


def test_pages(conn, weekend):
    bike, track, fp1 = weekend
    repo.import_laps(conn, fp1.id, [(122_000, [61_000, 61_000], True), (121_955, [60_900, 61_055], True),
                                    (140_000, [], False)])
    repo.add_feedback(conn, fp1.id, "front <slippery> mid corner T3", severity=3)
    repo.record_hot_pressures(conn, fp1.id, front=2.5)
    change = repo.change_setup(conn, bike.id, [parse_assignment("fork.rebound=-2")], session_id=fp1.id,
                               proposed_by="agent", hypothesis="hold the front down", symptom="front_push_mid")
    fp2 = repo.start_session(conn, bike.id, track.id, type="FP2", date=fp1.date)
    repo.import_laps(conn, fp2.id, [(121_388, [60_500, 60_888], True)])
    repo.add_outcome(conn, change.id, "better", session_id=fp2.id, lap_delta_ms=-567)

    status, html = web.render(conn, "/")
    assert status == 200 and f"<a href='/session/{fp1.id}'>2026-06-12</a>" in html
    assert "front &lt;slippery&gt; mid corner T3" in html  # rider text is escaped

    status, html = web.render(conn, f"/session/{fp1.id}")
    assert status == 200 and "<b>2:01.955</b>" in html and "<b>1:00.900</b>" in html
    assert "<tr class=muted>" in html  # the invalid lap
    assert "front 2.1 cold / 2.5 hot bar" in html
    assert "Change #1</b> by the engineer: fork.rebound 10 -&gt; 8 (-2)" in html
    assert "<span class='tag ok'>better</span> best lap -0.567" in html

    status, html = web.render(conn, f"/session/{fp2.id}")
    assert f"best -0.567 vs <a href='/session/{fp1.id}'>FP1 2026-06-12</a>" in html

    status, html = web.render(conn, "/setups")
    assert status == 200 and "Current setup #2" in html and "Setup #1 <span class='tag'>baseline</span>" in html
    status, html = web.render(conn, f"/effects?bike={bike.id}")
    assert status == 200 and "fork.rebound -2" in html


def test_not_found_and_empty(conn):
    assert web.render(conn, "/")[1].count("No bike yet") == 1
    assert web.render(conn, "/nope")[0] == 404
    assert web.render(conn, "/session/99")[0] == 404
    assert web.render(conn, "/?bike=7")[0] == 404


def test_server_serves_over_http(tmp_path, weekend):
    server = web.serve(tmp_path / "test.db", port=0)  # the weekend fixture's database
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/") as response:
            assert response.status == 200 and "Brno" in response.read().decode("utf-8")
    finally:
        server.shutdown()
        server.server_close()

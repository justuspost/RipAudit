import json
import re

import httpx
import pytest
from conftest import PLEX_TOKEN, PLEX_URL, TMDB_KEY, FakePlex, FakeProbe, FakeTmdb
from fastapi.testclient import TestClient

from ripaudit.web import create_app, parse_runtime


@pytest.fixture
def web(tmp_path):
    media = tmp_path / "media"
    (media / "Movies").mkdir(parents=True)
    fplex, ftmdb, fprobe = FakePlex(), FakeTmdb(), FakeProbe()
    app = create_app(tmp_path / "config", start_worker=False, engine_kwargs={
        "plex_transport": httpx.MockTransport(fplex.handler), "tmdb_transport": httpx.MockTransport(ftmdb.handler),
        "ffprobe_runner": fprobe})
    app.state.settings.update({"media_roots": [str(media)], "stability_observations": 0, "min_file_age_seconds": 0,
                               "plex": {"url": PLEX_URL, "token": PLEX_TOKEN,
                                        "path_mappings": [{"plex": "/data", "container": str(media)}]},
                               "tmdb": {"credential": TMDB_KEY}})
    client = TestClient(app)
    client.__enter__()
    yield client, app, tmp_path, media, fplex, ftmdb, fprobe
    client.__exit__(None, None, None)


def csrf(client, path):
    html = client.get(path).text
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    return m.group(1)


def do_setup(client, tmp_path, username="admin", password="a-long-passphrase"):
    token = (tmp_path / "config" / "setup-token").read_text()
    t = csrf(client, "/setup")
    return client.post("/setup", data={"csrf_token": t, "setup_token": token, "username": username,
                                       "password": password, "confirm": password}, follow_redirects=False)


def test_health_unauthenticated(web):
    client = web[0]
    assert client.get("/healthz").json()["status"] == "ok"


def test_ui_requires_setup_then_login(web):
    client, app, tmp_path, *_ = web
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/setup"
    assert client.get("/export.json", follow_redirects=False).status_code == 303


def test_setup_requires_token_and_strong_password(web):
    client, app, tmp_path, *_ = web
    t = csrf(client, "/setup")
    r = client.post("/setup", data={"csrf_token": t, "setup_token": "wrong", "username": "a",
                                    "password": "short", "confirm": "short"})
    assert r.status_code == 400 and "Setup token is incorrect" in r.text and "12 characters" in r.text
    assert do_setup(client, tmp_path).status_code == 303
    assert not (tmp_path / "config" / "setup-token").exists()
    assert client.get("/").status_code == 200
    # Setup cannot be repeated
    assert client.get("/setup", follow_redirects=False).headers["location"] == "/login"


def test_csrf_required(web):
    client, app, tmp_path, *_ = web
    do_setup(client, tmp_path)
    assert client.post("/scan-now", data={}).status_code == 403
    assert client.post("/scan-now", data={"csrf_token": "bogus"}).status_code == 403
    t = csrf(client, "/")
    assert client.post("/scan-now", data={"csrf_token": t}, follow_redirects=False).status_code == 303


def test_login_lockout(web):
    client, app, tmp_path, *_ = web
    do_setup(client, tmp_path)
    t = csrf(client, "/")
    client.post("/logout", data={"csrf_token": t})
    for _ in range(5):
        t = csrf(client, "/login")
        assert client.post("/login", data={"csrf_token": t, "username": "admin", "password": "nope"}).status_code == 401
    t = csrf(client, "/login")
    r = client.post("/login", data={"csrf_token": t, "username": "admin", "password": "a-long-passphrase"})
    assert r.status_code == 429


def test_full_flow_dashboard_detail_actions_exports(web):
    client, app, tmp_path, media, fplex, ftmdb, fprobe = web
    do_setup(client, tmp_path)
    (media / "Movies" / "=cmd.mkv").write_bytes(b"x")
    fplex.add("Heat", 1995, ["/data/Movies/=cmd.mkv"], tmdb=949)
    ftmdb.add(949, "Heat", 1995, 170)
    fprobe.durations["=cmd.mkv"] = 140 * 60
    eng = app.state.engine
    eng.run_discovery()
    eng.process_probes()
    eng.plex_sync()
    eng.resolve_metadata()
    eng.classify_all()
    home = client.get("/").text
    assert "Possible incomplete rip" in home and "Heat" in home
    fid = app.state.db.scalar("SELECT id FROM files")
    detail = client.get(f"/files/{fid}").text
    assert "TMDB general runtime" in detail and "-0:30:00" in detail
    t = csrf(client, f"/files/{fid}")
    assert client.post(f"/files/{fid}/approve", data={"csrf_token": t, "reason": ""},
                       follow_redirects=True).status_code == 200
    assert app.state.db.scalar("SELECT COUNT(*) FROM approvals") == 0
    client.post(f"/files/{fid}/approve", data={"csrf_token": t, "reason": "Theatrical disc"})
    assert app.state.db.scalar("SELECT outcome FROM results") == "approved_exception"
    client.post(f"/files/{fid}/reopen", data={"csrf_token": t, "reason": "Re-check"})
    assert app.state.db.scalar("SELECT outcome FROM results") == "possible_incomplete"
    client.post(f"/files/{fid}/override", data={"csrf_token": t, "runtime": "2:20:10", "note": "DVDFab"})
    assert app.state.db.scalar("SELECT outcome FROM results") == "no_runtime_issue"
    csv_text = client.get("/export.csv").text
    assert "'=cmd.mkv" not in csv_text.split("\n")[0]
    assert ",'" in csv_text or "'/" not in csv_text
    data = client.get("/export.json").json()
    assert data["files"][0]["outcome"] == "no_runtime_issue" and "does not certify" in data["note"]
    files_page = client.get("/files?outcome=no_runtime_issue&sort=difference&dir=desc").text
    assert "Heat" in files_page


def test_settings_never_render_or_export_secrets(web):
    client, app, tmp_path, *_ = web
    do_setup(client, tmp_path)
    html = client.get("/settings").text
    assert PLEX_TOKEN not in html and TMDB_KEY not in html
    diag = client.get("/diagnostics.json").text
    assert PLEX_TOKEN not in diag and TMDB_KEY not in diag
    assert json.loads(diag)["secret_sources"]["plex"] == "settings"


def test_settings_validation(web):
    client, app, tmp_path, media, *_ = web
    do_setup(client, tmp_path)
    t = csrf(client, "/settings")
    base = {"csrf_token": t, "media_roots": str(media), "extensions": ".mkv", "scan_interval_minutes": "15",
            "stability_observations": "1", "min_file_age_seconds": "60", "probe_concurrency": "1",
            "probe_timeout_seconds": "120", "plex_url": "http://192.168.1.2:32400", "plex_token": "",
            "plex_library_keys": "", "path_mappings": f"/data => {media}", "plex_sync_interval_minutes": "30",
            "pending_grace_hours": "48", "tmdb_credential": "", "tmdb_cache_days": "30",
            "short_review_seconds": "120", "short_high_seconds": "300", "short_review_percent": "3",
            "short_high_percent": "10", "long_review_seconds": "120", "long_review_percent": "3",
            "notify_kind": "none", "notify_url": "", "notify_token": "", "digest_hour": "8"}
    assert client.post("/settings", data=base, follow_redirects=False).status_code == 303
    assert app.state.settings.get()["plex"]["token"] == PLEX_TOKEN  # blank kept the stored secret
    bad = dict(base, plex_url="file:///etc/passwd", path_mappings="/data => /etc")
    r = client.post("/settings", data=bad)
    assert r.status_code == 400 and "http://" in r.text and "not inside a configured media root" in r.text


def test_parse_runtime():
    assert parse_runtime("2:14:07") == 8047
    assert parse_runtime("134") == 134 * 60
    assert parse_runtime("abc") is None

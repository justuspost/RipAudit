import json
import os

import httpx

from ripaudit.config import Settings
from ripaudit.db import Database
from ripaudit.engine import Engine


def setup_movie(env, rel="Movies/Heat (1995)/Heat.mkv", runtime_min=170, actual=170 * 60, tmdb_id=949, **kw):
    env.write(rel)
    env.plex.add("Heat", 1995, [f"/data/{rel}"], tmdb=tmdb_id, **kw)
    env.tmdb.add(tmdb_id, "Heat", 1995, runtime_min)
    env.probe.durations[os.path.basename(rel)] = actual
    return rel


def test_end_to_end_match(env):
    rel = setup_movie(env)
    env.run()
    r = env.result(rel)
    assert r["outcome"] == "no_runtime_issue"
    assert r["expected_source"] == "tmdb" and r["expected_seconds"] == 170 * 60


def test_shortened_file_flagged(env):
    rel = setup_movie(env, actual=170 * 60 - 25 * 60)
    env.run()
    r = env.result(rel)
    assert (r["outcome"], r["severity"]) == ("possible_incomplete", "high")


def test_media_files_are_never_modified(env):
    rel = setup_movie(env)
    p = env.media / rel
    before = (p.read_bytes(), p.stat().st_mtime_ns)
    env.run()
    assert (p.read_bytes(), p.stat().st_mtime_ns) == before


def test_imdb_only_identity_resolves_through_find(env):
    env.write("Movies/A.mkv")
    env.plex.add("Alien", 1979, ["/data/Movies/A.mkv"], imdb="tt0078748")
    env.tmdb.add(348, "Alien", 1979, 117, imdb="tt0078748")
    env.probe.durations["A.mkv"] = 117 * 60
    env.run()
    env.engine.resolve_metadata()
    env.engine.classify_all()
    assert env.result("Movies/A.mkv")["outcome"] == "no_runtime_issue"
    assert env.db.scalar("SELECT tmdb_id FROM identities") == 348


def test_ambiguous_imdb_match_is_not_accepted(env):
    env.write("Movies/A.mkv")
    env.plex.add("Thing", 2000, ["/data/Movies/A.mkv"], imdb="tt1")
    env.tmdb.imdb["tt1"] = [1, 2]
    env.run()
    assert env.db.scalar("SELECT state FROM identities") == "ambiguous"
    assert env.result("Movies/A.mkv")["outcome"] == "unverified"


def test_title_search_only_offers_candidates(env):
    env.settings.update({"tmdb": {"title_search_fallback": True}})
    env.write("Movies/B.mkv")
    env.plex.add("Obscure", 1970, ["/data/Movies/B.mkv"])
    env.tmdb.search_results = [{"id": 5, "title": "Obscure", "release_date": "1970-01-01"}]
    env.run()
    row = env.db.one("SELECT * FROM identities")
    assert row["state"] == "ambiguous" and row["tmdb_id"] is None
    assert json.loads(row["candidates"])[0]["tmdb_id"] == 5
    assert env.result("Movies/B.mkv")["outcome"] == "unverified"
    fid = env.file_id("Movies/B.mkv")
    env.tmdb.add(5, "Obscure", 1970, 120)
    env.engine.action_select_match(fid, 5, "admin")
    env.engine.resolve_metadata()
    env.engine.classify_all()
    assert env.result("Movies/B.mkv")["outcome"] == "no_runtime_issue"


def test_unmatched_file_stays_pending_then_unverified_and_retries(env):
    env.write("Movies/New.mkv")
    env.run()
    assert env.result("Movies/New.mkv")["outcome"] == "pending"
    env.clock.advance(49 * 3600)
    env.engine.classify_all()
    assert env.result("Movies/New.mkv")["outcome"] == "unverified"
    # Plex later indexes it: the next sync picks it up.
    env.plex.add("New", 2024, ["/data/Movies/New.mkv"], tmdb=77)
    env.tmdb.add(77, "New", 2024, 120)
    env.engine.plex_sync()
    env.engine.resolve_metadata()
    env.engine.classify_all()
    assert env.result("Movies/New.mkv")["outcome"] == "no_runtime_issue"


def test_plex_unavailable_keeps_existing_identity(env):
    rel = setup_movie(env)
    env.run()
    env.plex.down = True
    out = env.engine.plex_sync()
    assert "error" in out
    assert env.engine.get_status("plex")["ok"] is False
    assert env.db.scalar("SELECT state FROM identities") == "matched"
    env.engine.classify_all()
    assert env.result(rel)["outcome"] == "no_runtime_issue"


def test_tmdb_rate_limited_backs_off(env):
    rel = setup_movie(env)
    env.tmdb.mode = "ratelimit"
    env.run()
    assert env.result(rel)["outcome"] == "unverified"
    calls = env.tmdb.calls
    env.engine.resolve_metadata()
    assert env.tmdb.calls == calls  # backing off
    env.tmdb.mode = "ok"
    env.clock.advance(8)
    env.engine.resolve_metadata()
    env.engine.classify_all()
    assert env.result(rel)["outcome"] == "no_runtime_issue"


def test_tmdb_unavailable(env):
    rel = setup_movie(env)
    env.tmdb.mode = "error"
    env.run()
    assert env.result(rel)["outcome"] == "unverified"
    assert env.engine.get_status("tmdb")["ok"] is False


def test_missing_tmdb_runtime_is_unverified(env):
    rel = setup_movie(env, runtime_min=None)
    env.run()
    assert env.result(rel)["outcome"] == "unverified"


def test_invalid_probe_duration(env):
    rel = setup_movie(env)
    env.probe.durations["Heat.mkv"] = None
    env.run()
    assert env.result(rel)["outcome"] == "probe_error"


def test_probe_timeout_and_one_bad_file_does_not_block_queue(env):
    setup_movie(env, rel="Movies/Bad.mkv", tmdb_id=1)
    good = setup_movie(env, rel="Movies/Good.mkv", tmdb_id=2)
    env.probe.status["Bad.mkv"] = "timeout"
    env.run()
    assert env.result("Movies/Bad.mkv")["outcome"] == "probe_error"
    assert env.result(good)["outcome"] == "no_runtime_issue"
    job = env.db.one("SELECT * FROM jobs j JOIN files f ON f.id=j.file_id WHERE f.path LIKE '%Bad.mkv'")
    assert job["status"] == "queued" and job["not_before"] > env.clock()  # bounded retry scheduled


def test_file_changing_during_probe_is_retried(env):
    rel = setup_movie(env)

    def grow(path):
        if path.name == "Heat.mkv" and len(env.probe.calls) == 1:
            with open(path, "ab") as fh:
                fh.write(b"more data")
    env.probe.side_effect = grow
    env.run()
    assert env.result(rel)["outcome"] == "pending"
    env.engine.run_discovery()
    while env.engine.process_probes():
        pass
    env.engine.classify_all()
    assert env.result(rel)["outcome"] == "no_runtime_issue"


def test_stability_and_min_age(env):
    env.settings.update({"stability_observations": 2, "min_file_age_seconds": 0})
    env.write("Movies/S.mkv")
    env.engine.run_discovery()
    assert env.probe.calls == [] and not env.db.scalar("SELECT ready FROM files")
    env.engine.run_discovery()
    assert not env.db.scalar("SELECT ready FROM files")
    env.engine.run_discovery()
    assert env.db.scalar("SELECT ready FROM files") == 1


def test_interrupted_scan_recovery(env):
    rel = setup_movie(env)
    env.engine.run_discovery()
    with env.db.tx() as conn:
        conn.execute("UPDATE jobs SET status='running'")
        conn.execute("INSERT INTO scans(kind, started_at, status) VALUES ('discovery', 1, 'running')")
    # Simulate restart
    engine2 = Engine(env.db, env.settings, clock=env.clock, plex_transport=env.engine.plex_transport,
                     tmdb_transport=env.engine.tmdb_transport, ffprobe_runner=env.probe)
    engine2.recover()
    assert env.db.scalar("SELECT COUNT(*) FROM scans WHERE status='interrupted'") == 1
    while engine2.process_probes():
        pass
    engine2.plex_sync()
    engine2.resolve_metadata()
    engine2.classify_all()
    assert env.result(rel)["outcome"] == "no_runtime_issue"


def test_unavailable_mount_is_not_mass_deletion(env, tmp_path):
    rel = setup_movie(env)
    env.run()
    # Root disappears entirely
    moved = tmp_path / "offline"
    os.rename(env.media, moved)
    report = env.engine.run_discovery()
    assert report.roots[0].status == "unavailable"
    assert env.db.scalar("SELECT state FROM files") == "present"
    # Root present but empty (unmounted share)
    env.media.mkdir()
    report = env.engine.run_discovery()
    assert report.roots[0].status == "unavailable"
    assert env.db.scalar("SELECT state FROM files") == "present"
    os.rmdir(env.media)
    os.rename(moved, env.media)
    env.engine.run_discovery()
    assert env.result(rel) is not None


def test_mass_disappearance_guard_needs_confirmation(env):
    for i in range(4):
        env.write(f"Movies/M{i}.mkv")
    env.write("Movies/keep.mkv")
    env.engine.run_discovery()
    for i in range(4):
        os.remove(env.media / f"Movies/M{i}.mkv")
    r = env.engine.run_discovery()
    assert r.roots[0].status == "guarded" and r.missing == 0
    r = env.engine.run_discovery()
    assert r.missing == 4


def test_multiple_versions_are_classified_separately(env):
    env.write("Movies/Blade Runner/BR - Final Cut.mkv")
    env.write("Movies/Blade Runner/BR - Theatrical.mkv")
    item = env.plex.add("Blade Runner", 1982, ["/data/Movies/Blade Runner/BR - Final Cut.mkv"], tmdb=78)
    theatrical = "/data/Movies/Blade Runner/BR - Theatrical.mkv"
    item["Media"].append({"id": "m-second", "Part": [{"id": "p2", "file": theatrical}]})
    env.tmdb.add(78, "Blade Runner", 1982, 117)
    env.probe.durations["BR - Final Cut.mkv"] = 117 * 60
    env.probe.durations["BR - Theatrical.mkv"] = 90 * 60
    env.run()
    assert env.result("Movies/Blade Runner/BR - Final Cut.mkv")["outcome"] == "no_runtime_issue"
    assert env.result("Movies/Blade Runner/BR - Theatrical.mkv")["outcome"] == "possible_incomplete"
    medias = {r[0] for r in env.db.query("SELECT plex_media_id FROM identities")}
    assert len(medias) == 2


def test_multipart_sums_all_parts(env):
    env.write("Movies/Long/cd1.mkv")
    env.write("Movies/Long/cd2.mkv")
    env.plex.add("Long", 1960, ["/data/Movies/Long/cd1.mkv", "/data/Movies/Long/cd2.mkv"], tmdb=9)
    env.tmdb.add(9, "Long", 1960, 220)
    env.probe.durations["cd1.mkv"] = 110 * 60
    env.probe.durations["cd2.mkv"] = 110 * 60
    env.run()
    for p in ("cd1", "cd2"):
        r = env.result(f"Movies/Long/{p}.mkv")
        assert r["outcome"] == "no_runtime_issue", p
        assert r["actual_seconds"] == 220 * 60


def test_multipart_with_missing_part_is_not_compared_on_first_file_only(env):
    env.write("Movies/Long/cd1.mkv")
    env.plex.add("Long", 1960, ["/data/Movies/Long/cd1.mkv", "/data/Movies/Long/cd2.mkv"], tmdb=9)
    env.tmdb.add(9, "Long", 1960, 220)
    env.probe.durations["cd1.mkv"] = 220 * 60  # would "match" if compared alone
    env.run()
    assert env.result("Movies/Long/cd1.mkv")["outcome"] == "pending"


def test_replaced_file_reaudited_and_approval_invalidated(env):
    rel = setup_movie(env, actual=100 * 60)
    env.run()
    fid = env.file_id(rel)
    env.engine.action_approve(fid, "Short theatrical cut", False, "admin")
    env.engine.classify_all()
    assert env.result(rel)["outcome"] == "approved_exception"
    env.write(rel, b"y" * 128)  # re-rip replaces the file
    env.probe.durations["Heat.mkv"] = 100 * 60
    env.run()
    assert env.result(rel)["outcome"] == "possible_incomplete"
    a = env.db.one("SELECT * FROM approvals")
    assert a["revoked_by"] == "system"
    kinds = [r[0] for r in env.db.query("SELECT kind FROM events WHERE file_id=?", (fid,))]
    assert "approval_invalidated" in kinds and env.probe.calls.count("Heat.mkv") == 2


def test_preserved_approval_carries_forward(env):
    rel = setup_movie(env, actual=100 * 60)
    env.run()
    fid = env.file_id(rel)
    env.engine.action_approve(fid, "Known short cut", True, "admin")
    env.write(rel, b"z" * 200)
    env.run()
    assert env.result(rel)["outcome"] == "approved_exception"


def test_approval_does_not_transfer_to_a_different_file(env):
    rel = setup_movie(env, actual=100 * 60)
    env.run()
    env.engine.action_approve(env.file_id(rel), "ok", True, "admin")
    os.rename(env.media / rel, env.media / "Movies/Heat (1995)/Heat-renamed.mkv")
    env.plex.movies[0]["Media"][0]["Part"][0]["file"] = "/data/Movies/Heat (1995)/Heat-renamed.mkv"
    env.probe.durations["Heat-renamed.mkv"] = 100 * 60
    env.run()
    assert env.result("Movies/Heat (1995)/Heat-renamed.mkv")["outcome"] == "possible_incomplete"


def test_override_scoped_to_file_version(env):
    rel = setup_movie(env, actual=150 * 60)
    env.run()
    fid = env.file_id(rel)
    env.engine.action_set_override(fid, 150 * 60 + 20, "DVDFab title", "admin")
    env.engine.classify_all()
    r = env.result(rel)
    assert (r["outcome"], r["expected_source"]) == ("no_runtime_issue", "user_override")
    env.write(rel, b"q" * 99)
    env.run()
    assert env.result(rel)["expected_source"] == "tmdb"


def test_approvals_survive_restart(env):
    rel = setup_movie(env, actual=100 * 60)
    env.run()
    env.engine.action_approve(env.file_id(rel), "Disc is the short cut", False, "admin")
    env.engine.classify_all()
    env.db.close()
    db2 = Database(env.config / "ripaudit.db")
    s2 = Settings(env.config, environ={})
    e2 = Engine(db2, s2, clock=env.clock, plex_transport=env.engine.plex_transport,
                tmdb_transport=env.engine.tmdb_transport, ffprobe_runner=env.probe)
    e2.recover()
    e2.tick()
    row = db2.one("SELECT outcome FROM results")
    assert row["outcome"] == "approved_exception"
    db2.close()


def test_threshold_change_recalculates_without_reprobing(env):
    rel = setup_movie(env, actual=170 * 60 - 200)
    env.run()
    assert env.result(rel)["outcome"] == "possible_incomplete"
    calls = len(env.probe.calls)
    env.settings.update({"thresholds": {"short_review_seconds": 600, "short_review_percent": 10.0}})
    env.engine.classify_all()
    assert env.result(rel)["outcome"] == "no_runtime_issue"
    assert len(env.probe.calls) == calls


def test_notification_dedup_and_retry(env):
    sent = []
    state = {"fail": True}

    def handler(request):
        if state["fail"]:
            return httpx.Response(500)
        sent.append(request)
        return httpx.Response(200)

    env.engine.notify_transport = httpx.MockTransport(handler)
    env.settings.update({"notifications": {"kind": "ntfy", "url": "http://ntfy.test/topic", "token": "ntfy-secret"}})
    rel = setup_movie(env, actual=60 * 60)
    env.run()
    env.engine.deliver_notifications()
    n = env.db.one("SELECT * FROM notifications")
    assert n["status"] == "pending" and n["attempts"] == 1 and n["next_attempt"] > env.clock()
    assert "ntfy-secret" not in (n["last_error"] or "")
    state["fail"] = False
    env.clock.advance(120)
    env.engine.deliver_notifications()
    for _ in range(3):
        env.engine.classify_all()
        env.engine.deliver_notifications()
    assert len(sent) == 1
    assert sent[0].headers["Priority"] == "high"
    assert b"Heat.mkv" in sent[0].content and str(env.media).encode() not in sent[0].content
    assert env.result(rel)["outcome"] == "possible_incomplete"


def test_plex_path_mapping_mismatch_is_reported(env):
    env.write("Movies/X.mkv")
    env.plex.add("X", 2000, ["/elsewhere/Movies/X.mkv"], tmdb=3)
    env.run()
    assert "no matching path mapping" in env.engine.get_status("plex")["message"]
    assert env.result("Movies/X.mkv")["outcome"] == "pending"


def test_symlink_escape_is_skipped(env, tmp_path):
    outside = tmp_path / "secret.mkv"
    outside.write_bytes(b"x")
    os.symlink(outside, env.media / "Movies" / "link.mkv")
    env.engine.run_discovery()
    assert env.db.scalar("SELECT COUNT(*) FROM files") == 0


def test_malicious_filenames_are_handled_as_data(env):
    names = ["-rf.mkv", "$(touch pwned).mkv", "a;b|c.mkv", "<script>.mkv"]
    for n in names:
        env.write(f"Movies/{n}")
    env.engine.run_discovery()
    while env.engine.process_probes():
        pass
    assert sorted(env.probe.calls) == sorted(names)
    assert not (env.media / "Movies" / "pwned").exists()


def test_ignored_patterns(env):
    env.write("Movies/A.mkv.part")
    env.write("Movies/.hidden/B.mkv")
    env.write("Movies/C.tmp")
    env.write("Movies/real.MKV")
    env.engine.run_discovery()
    assert [os.path.basename(r[0]) for r in env.db.query("SELECT path FROM files")] == ["real.MKV"]


def test_idle_classification_does_not_rewrite_rows(env):
    rel = setup_movie(env)
    env.run()
    before = env.result(rel)["computed_at"]
    env.clock.advance(60)
    env.engine.classify_all()
    assert env.result(rel)["computed_at"] == before


def test_daily_digest_once_per_day(env):
    import time as _t
    env.settings.update({"notifications": {"kind": "webhook", "url": "http://hook.test/x", "daily_digest": True,
                                           "digest_hour": 0}})
    setup_movie(env, actual=60 * 60)
    env.run()
    assert env.engine.queue_daily_digest() is True
    assert env.engine.queue_daily_digest() is False
    key = f"digest:{_t.strftime('%Y-%m-%d', _t.localtime(env.clock()))}"
    assert env.db.scalar("SELECT COUNT(*) FROM notifications WHERE dedupe_key=?", (key,)) == 1

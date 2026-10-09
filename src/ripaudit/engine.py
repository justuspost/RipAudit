"""Processing engine: discovery -> probe -> Plex identity -> TMDB runtime -> classify -> notify.

All state lives in SQLite so processing is restart-safe. ``tick()`` performs one
bounded unit of work and is called repeatedly by :class:`Worker`.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

import httpx

from . import classify as cls
from . import discovery, probe
from .config import Settings
from .db import Database
from .integrations import notify, plex, tdarr, tmdb
from .security import redact, resolve_within

log = logging.getLogger("ripaudit")

PROBE_MAX_ATTEMPTS = 3
PLEX_MIN_GAP_SECONDS = 300
CLASSIFY_IDLE_SECONDS = 300


class Engine:
    def __init__(self, db: Database, settings: Settings, clock: Callable[[], float] = time.time,
                 plex_transport: httpx.BaseTransport | None = None,
                 tmdb_transport: httpx.BaseTransport | None = None,
                 notify_transport: httpx.BaseTransport | None = None,
                 ffprobe_runner: Callable[[Path, float], probe.ProbeResult] | None = None):
        self.db = db
        self.settings = settings
        self.clock = clock
        self.plex_transport = plex_transport
        self.tmdb_transport = tmdb_transport
        self.notify_transport = notify_transport
        self.ffprobe_runner = ffprobe_runner or (lambda p, t: probe.run_ffprobe(p, t))
        self._last_classify = 0.0
        self.scan_requested = threading.Event()
        self.plex_requested = threading.Event()

    # ----- utilities ----------------------------------------------------
    def _redact(self, text: str) -> str:
        return redact(text, self.settings.secret_values())

    def set_status(self, name: str, ok: bool, message: str) -> None:
        with self.db.tx() as conn:
            conn.execute("INSERT OR REPLACE INTO kv(key, value) VALUES (?, ?)",
                         (f"status:{name}", json.dumps({"ok": ok, "message": self._redact(message),
                                                         "at": self.clock()})))

    def get_status(self, name: str) -> dict | None:
        raw = self.db.scalar("SELECT value FROM kv WHERE key = ?", (f"status:{name}",))
        return json.loads(raw) if raw else None

    def _kv_float(self, key: str) -> float:
        raw = self.db.scalar("SELECT value FROM kv WHERE key = ?", (key,))
        return float(raw) if raw else 0.0

    def _kv_set(self, key: str, value: object) -> None:
        with self.db.tx() as conn:
            conn.execute("INSERT OR REPLACE INTO kv(key, value) VALUES (?, ?)", (key, str(value)))

    def recover(self) -> None:
        """Called at startup: requeue interrupted jobs and close interrupted scans."""
        with self.db.tx() as conn:
            conn.execute("UPDATE jobs SET status='queued' WHERE status='running'")
            conn.execute("UPDATE scans SET status='interrupted', finished_at=? WHERE status='running'",
                         (self.clock(),))

    def enqueue(self, conn, kind: str, file_id: int, delay: float = 0.0) -> None:
        now = self.clock()
        conn.execute(
            "INSERT INTO jobs(kind, file_id, status, attempts, not_before, created_at, updated_at)"
            " VALUES (?, ?, 'queued', 0, ?, ?, ?) ON CONFLICT DO NOTHING",
            (kind, file_id, now + delay, now, now))

    # ----- discovery ----------------------------------------------------
    def run_discovery(self) -> discovery.ScanReport:
        cfg = self.settings.get()
        now = self.clock()
        with self.db.tx() as conn:
            scan_id = conn.execute("INSERT INTO scans(kind, started_at, status) VALUES ('discovery', ?, 'running')",
                                   (now,)).lastrowid
        try:
            report = discovery.discover(self.db, cfg, now=now)
        except Exception as exc:  # noqa: BLE001 - recorded and surfaced in the UI
            with self.db.tx() as conn:
                conn.execute("UPDATE scans SET status='failed', finished_at=?, summary=? WHERE id=?",
                             (self.clock(), self._redact(str(exc)), scan_id))
            raise
        with self.db.tx() as conn:
            for fid in report.changed_ids:
                self._handle_replacement(conn, fid)
                # A new file version supersedes any pending retry of the old version.
                conn.execute("UPDATE jobs SET not_before=?, attempts=0 WHERE kind='probe' AND file_id=?"
                             " AND status='queued'", (self.clock(), fid))
            for fid in report.ready:
                row = conn.execute("SELECT signature FROM files WHERE id=?", (fid,)).fetchone()
                done = conn.execute("SELECT 1 FROM probes WHERE file_id=? AND signature=? AND status != 'changed'",
                                    (fid, row["signature"])).fetchone()
                if not done:
                    self.enqueue(conn, "probe", fid)
            status = "ok" if all(r.status == "ok" for r in report.roots) and not report.errors else "partial"
            summary = {"new": report.new, "changed": report.changed, "missing": report.missing,
                       "returned": report.returned, "roots": [asdict(r) for r in report.roots],
                       "errors": report.errors[:50]}
            conn.execute("UPDATE scans SET status=?, finished_at=?, summary=? WHERE id=?",
                         (status, self.clock(), json.dumps(summary), scan_id))
        self._kv_set("last_discovery", self.clock())
        return report

    def _handle_replacement(self, conn, fid: int) -> None:
        """A file's signature changed: stale approvals/overrides no longer apply unless preserved."""
        sig = conn.execute("SELECT signature FROM files WHERE id=?", (fid,)).fetchone()["signature"]
        for a in conn.execute("SELECT * FROM approvals WHERE file_id=? AND revoked_at IS NULL AND signature != ?",
                              (fid, sig)).fetchall():
            if a["preserve_on_replace"]:
                conn.execute("UPDATE approvals SET signature=? WHERE id=?", (sig, a["id"]))
                self.db.event(conn, fid, "approval_carried_forward", {"approval_id": a["id"]})
            else:
                conn.execute("UPDATE approvals SET revoked_at=?, revoked_by='system', revoke_reason=? WHERE id=?",
                             (self.clock(), "File was replaced (size or modification time changed).", a["id"]))
                self.db.event(conn, fid, "approval_invalidated", {"approval_id": a["id"]})
        for o in conn.execute("SELECT id FROM overrides WHERE file_id=? AND active=1 AND signature != ?",
                              (fid, sig)).fetchall():
            conn.execute("UPDATE overrides SET active=0 WHERE id=?", (o["id"],))
            self.db.event(conn, fid, "override_invalidated", {"override_id": o["id"]})

    # ----- probing ------------------------------------------------------
    def process_probes(self) -> int:
        cfg = self.settings.get()
        now = self.clock()
        limit = max(1, int(cfg["probe_concurrency"]))
        jobs = self.db.query("SELECT j.id, j.file_id, j.attempts, f.path, f.signature FROM jobs j"
                             " JOIN files f ON f.id = j.file_id WHERE j.kind='probe' AND j.status='queued'"
                             " AND j.not_before <= ? AND f.state='present' ORDER BY j.not_before LIMIT ?",
                             (now, limit))
        if not jobs:
            return 0
        with self.db.tx() as conn:
            for j in jobs:
                conn.execute("UPDATE jobs SET status='running', attempts=attempts+1, updated_at=? WHERE id=?",
                             (now, j["id"]))
        timeout = float(cfg["probe_timeout_seconds"])
        roots = cfg["media_roots"]

        def work(j):
            safe = resolve_within(j["path"], roots)
            if safe is None:
                return j, probe.ProbeResult(status="failed", diagnostics="Path resolves outside the media roots.")
            try:
                result = self.ffprobe_runner(safe, timeout)
            except Exception as exc:  # noqa: BLE001 - one bad file must not stop the queue
                return j, probe.ProbeResult(status="failed", diagnostics=f"Unexpected error: {exc}")
            try:
                st = os.stat(safe)
                after = discovery.signature_of(st.st_size, st.st_mtime_ns)
            except OSError:
                after = None
            if after != j["signature"]:
                result = probe.ProbeResult(status="changed",
                                           diagnostics="File changed or disappeared during inspection.")
            return j, result

        with ThreadPoolExecutor(max_workers=limit) as pool:
            results = list(pool.map(work, jobs))
        with self.db.tx() as conn:
            for j, r in results:
                current = conn.execute("SELECT signature FROM files WHERE id=?", (j["file_id"],)).fetchone()
                if current is None:
                    continue
                if current["signature"] != j["signature"]:
                    r = probe.ProbeResult(status="changed", diagnostics="File changed after the probe was queued.")
                conn.execute(
                    "INSERT INTO probes(file_id, signature, status, duration_seconds, duration_source,"
                    " container_duration, video_codec, width, height, video_stream_count, audio_count,"
                    " audio_languages, subtitle_count, subtitle_languages, warnings, diagnostics, probed_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (j["file_id"], j["signature"], r.status, r.duration_seconds, r.duration_source,
                     r.container_duration, r.video_codec, r.width, r.height, r.video_stream_count,
                     r.audio_count, json.dumps(r.audio_languages), r.subtitle_count,
                     json.dumps(r.subtitle_languages), json.dumps(r.warnings),
                     self._redact(r.diagnostics), self.clock()))
                self.db.event(conn, j["file_id"], "probed", {"status": r.status,
                                                             "duration": r.duration_seconds})
                retry = r.status in ("changed", "failed", "timeout") and j["attempts"] + 1 < PROBE_MAX_ATTEMPTS
                if r.status == "changed":
                    conn.execute("UPDATE files SET stable_count=0, ready=0 WHERE id=?", (j["file_id"],))
                if retry:
                    conn.execute("UPDATE jobs SET status='queued', not_before=?, last_error=?, updated_at=?"
                                 " WHERE id=?", (self.clock() + 300 * (j["attempts"] + 1), r.status,
                                                  self.clock(), j["id"]))
                else:
                    conn.execute("UPDATE jobs SET status=?, last_error=?, updated_at=? WHERE id=?",
                                 ("done" if r.status in ("ok", "ok_with_warnings") else "failed",
                                  None if r.status.startswith("ok") else r.status, self.clock(), j["id"]))
        return len(results)

    # ----- Plex ---------------------------------------------------------
    def plex_configured(self) -> bool:
        p = self.settings.get()["plex"]
        return bool(p["url"] and p["token"])

    def plex_sync(self) -> dict:
        cfg = self.settings.get()
        pc = cfg["plex"]
        now = self.clock()
        summary = {"versions": 0, "mapped": 0, "unmapped": 0, "matched_files": 0, "unmapped_examples": []}
        with self.db.tx() as conn:
            scan_id = conn.execute("INSERT INTO scans(kind, started_at, status) VALUES ('plex_sync', ?, 'running')",
                                   (now,)).lastrowid
        self._kv_set("last_plex_sync", now)
        try:
            client = plex.PlexClient(pc["url"], pc["token"], verify_tls=pc.get("verify_tls", True),
                                     transport=self.plex_transport)
            try:
                sections = [s for s in client.sections() if s["type"] == "movie"
                            and (not pc["library_keys"] or s["key"] in [str(k) for k in pc["library_keys"]])]
                versions: list[plex.PlexVersion] = []
                for s in sections:
                    versions.extend(client.movies(s["key"], s["title"]))
            finally:
                client.close()
        except plex.PlexError as exc:
            msg = self._redact(str(exc))
            self.set_status("plex", False, msg)
            with self.db.tx() as conn:
                conn.execute("UPDATE scans SET status='failed', finished_at=?, summary=? WHERE id=?",
                             (self.clock(), json.dumps({"error": msg}), scan_id))
            return {"error": msg}

        by_path: dict[str, tuple[plex.PlexVersion, plex.PlexPart]] = {}
        for v in versions:
            summary["versions"] += 1
            for part in v.parts:
                mapped = plex.map_plex_path(part.file, pc["path_mappings"])
                if mapped is None:
                    summary["unmapped"] += 1
                    if len(summary["unmapped_examples"]) < 5:
                        summary["unmapped_examples"].append(part.file)
                    continue
                summary["mapped"] += 1
                by_path[os.path.normpath(mapped)] = (v, part)

        with self.db.tx() as conn:
            for f in conn.execute("SELECT id, path FROM files WHERE state='present'").fetchall():
                existing = conn.execute("SELECT * FROM identities WHERE file_id=?", (f["id"],)).fetchone()
                hit = by_path.get(f["path"])
                if hit is None:
                    if existing is None:
                        conn.execute("INSERT INTO identities(file_id, state, updated_at) VALUES (?, 'pending', ?)",
                                     (f["id"], now))
                    continue
                summary["matched_files"] += 1
                v, part = hit
                if existing is not None and existing["state"] == "manual":
                    conn.execute(
                        "UPDATE identities SET plex_rating_key=?, plex_media_id=?, plex_part_id=?, part_index=?,"
                        " part_count=?, plex_title=?, plex_year=?, plex_edition=?, plex_library=?, guids=?,"
                        " updated_at=? WHERE file_id=?",
                        (v.rating_key, v.media_id, part.part_id, part.index, part.count, v.title, v.year,
                         v.edition, v.library, json.dumps(v.guids), now, f["id"]))
                    continue
                if v.tmdb_id:
                    state, method, tmdb_id = "matched", "plex_tmdb", v.tmdb_id
                elif v.imdb_id:
                    state, method, tmdb_id = "matched", "plex_imdb", None
                else:
                    state, method, tmdb_id = "unmatched", None, None
                if existing is not None and existing["tmdb_id"] and tmdb_id and existing["tmdb_id"] != tmdb_id:
                    self.db.event(conn, f["id"], "identity_changed",
                                  {"old_tmdb": existing["tmdb_id"], "new_tmdb": tmdb_id})
                if existing is not None and existing["plex_rating_key"] == v.rating_key and \
                        existing["state"] in ("ambiguous",) and state == "unmatched":
                    state = "ambiguous"  # keep candidates awaiting manual selection
                if state == "matched" and method == "plex_imdb" and existing is not None \
                        and existing["imdb_id"] == v.imdb_id and existing["tmdb_id"]:
                    tmdb_id = existing["tmdb_id"]
                values = (state, v.rating_key, v.media_id, part.part_id, part.index, part.count, v.title,
                          v.year, v.edition, v.library, json.dumps(v.guids), tmdb_id, v.imdb_id, method, now)
                if existing is None:
                    conn.execute(
                        "INSERT INTO identities(state, plex_rating_key, plex_media_id, plex_part_id, part_index,"
                        " part_count, plex_title, plex_year, plex_edition, plex_library, guids, tmdb_id, imdb_id,"
                        " match_method, updated_at, file_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        values + (f["id"],))
                    self.db.event(conn, f["id"], "plex_identified", {"title": v.title, "year": v.year,
                                                                     "tmdb": tmdb_id, "imdb": v.imdb_id})
                else:
                    conn.execute(
                        "UPDATE identities SET state=?, plex_rating_key=?, plex_media_id=?, plex_part_id=?,"
                        " part_index=?, part_count=?, plex_title=?, plex_year=?, plex_edition=?, plex_library=?,"
                        " guids=?, tmdb_id=?, imdb_id=?, match_method=?, updated_at=? WHERE file_id=?",
                        values + (f["id"],))
                    if existing["state"] == "pending":
                        self.db.event(conn, f["id"], "plex_identified", {"title": v.title, "year": v.year,
                                                                         "tmdb": tmdb_id, "imdb": v.imdb_id})
            conn.execute("UPDATE scans SET status='ok', finished_at=?, summary=? WHERE id=?",
                         (self.clock(), json.dumps(summary), scan_id))
        msg = f"{summary['versions']} movie versions read; {summary['mapped']} file paths mapped."
        if summary["unmapped"]:
            msg += f" {summary['unmapped']} Plex paths had no matching path mapping."
        self.set_status("plex", True, msg)
        return summary

    # ----- TMDB ---------------------------------------------------------
    def resolve_metadata(self, max_items: int = 25) -> int:
        cfg = self.settings.get()
        tc = cfg["tmdb"]
        if not tc["credential"]:
            return 0
        now = self.clock()
        if self._kv_float("tmdb_backoff_until") > now:
            return 0
        stale_before = now - float(tc["cache_days"]) * 86400
        rows = self.db.query(
            "SELECT i.* FROM identities i JOIN files f ON f.id=i.file_id WHERE f.state='present' AND ("
            " (i.state IN ('matched','manual') AND i.tmdb_id IS NULL AND i.imdb_id IS NOT NULL)"
            " OR (i.state IN ('matched','manual') AND i.tmdb_id IS NOT NULL AND NOT EXISTS ("
            "     SELECT 1 FROM tmdb_cache c WHERE c.tmdb_id=i.tmdb_id AND c.fetched_at >= ?))"
            " OR (i.state='unmatched' AND ? AND i.plex_title IS NOT NULL AND i.candidates IS NULL))"
            " LIMIT ?", (stale_before, 1 if tc["title_search_fallback"] else 0, max_items))
        if not rows:
            return 0
        done = 0
        try:
            client = tmdb.TmdbClient(tc["credential"], transport=self.tmdb_transport)
        except tmdb.TmdbError as exc:
            self.set_status("tmdb", False, str(exc))
            return 0
        try:
            for r in rows:
                fid = r["file_id"]
                try:
                    if r["state"] == "unmatched":
                        cands = client.search(r["plex_title"], r["plex_year"])
                        with self.db.tx() as conn:
                            conn.execute("UPDATE identities SET state='ambiguous', candidates=?, match_method=NULL,"
                                         " note=? WHERE file_id=?",
                                         (json.dumps(cands), "Title search results need manual selection.", fid))
                            self.db.event(conn, fid, "title_search", {"candidates": len(cands)})
                        done += 1
                        continue
                    tmdb_id = r["tmdb_id"]
                    if tmdb_id is None:
                        ids = client.find_imdb(r["imdb_id"])
                        if len(ids) != 1:
                            state = "unmatched" if not ids else "ambiguous"
                            cands = [{"tmdb_id": i, "title": None, "year": None} for i in ids]
                            with self.db.tx() as conn:
                                conn.execute("UPDATE identities SET state=?, candidates=?, note=? WHERE file_id=?",
                                             (state, json.dumps(cands) if ids else None,
                                              f"IMDb {r['imdb_id']} matched {len(ids)} TMDB movies.", fid))
                            continue
                        tmdb_id = ids[0]
                        with self.db.tx() as conn:
                            conn.execute("UPDATE identities SET tmdb_id=? WHERE file_id=?", (tmdb_id, fid))
                    m = client.movie(tmdb_id)
                    with self.db.tx() as conn:
                        conn.execute("INSERT OR REPLACE INTO tmdb_cache(tmdb_id, title, year, runtime_minutes,"
                                     " imdb_id, fetched_at) VALUES (?,?,?,?,?,?)",
                                     (m.tmdb_id, m.title, m.year, m.runtime_minutes, m.imdb_id, self.clock()))
                    done += 1
                except tmdb.RateLimited as exc:
                    self._kv_set("tmdb_backoff_until", self.clock() + min(max(exc.retry_after, 1.0), 600.0))
                    self.set_status("tmdb", False, str(exc))
                    break
                except tmdb.NotFound:
                    with self.db.tx() as conn:
                        conn.execute("UPDATE identities SET state='unmatched', note=? WHERE file_id=?",
                                     ("TMDB has no record for the identifier Plex supplied.", fid))
                except tmdb.TmdbError as exc:
                    self._kv_set("tmdb_backoff_until", self.clock() + 300)
                    self.set_status("tmdb", False, str(exc))
                    break
            else:
                self.set_status("tmdb", True, f"Metadata lookups succeeded ({done} this round).")
        finally:
            client.close()
        return done

    # ----- classification ----------------------------------------------
    def _inputs_for(self, f, cfg) -> tuple[cls.Inputs, float | None, float | None, str | None]:
        now = self.clock()
        inp = cls.Inputs(file_state=f["state"], ready=bool(f["ready"]))
        pr = self.db.one("SELECT * FROM probes WHERE file_id=? AND signature=? ORDER BY probed_at DESC, id DESC"
                         " LIMIT 1", (f["id"], f["signature"]))
        if pr is not None:
            inp.probe_status = pr["status"]
            inp.actual_seconds = pr["duration_seconds"]
        ident = self.db.one("SELECT * FROM identities WHERE file_id=?", (f["id"],))
        grace = float(cfg["plex"]["pending_grace_hours"]) * 3600
        if ident is None:
            inp.identity_state = "pending"
        else:
            inp.identity_state = ident["state"]
            inp.plex_edition = ident["plex_edition"]
        inp.identity_grace_expired = (now - f["signature_changed_at"]) > grace or not self.plex_configured()

        group_files = [f]
        if ident is not None and ident["plex_media_id"] and (ident["part_count"] or 1) > 1:
            inp.parts_expected = int(ident["part_count"])
            group_files = self.db.query(
                "SELECT f.* FROM files f JOIN identities i ON i.file_id=f.id WHERE i.plex_media_id=?"
                " AND i.plex_rating_key=? AND f.state='present' ORDER BY i.part_index",
                (ident["plex_media_id"], ident["plex_rating_key"]))
            total, probed = 0.0, 0
            for gf in group_files:
                gp = self.db.one("SELECT * FROM probes WHERE file_id=? AND signature=? ORDER BY probed_at DESC,"
                                 " id DESC LIMIT 1", (gf["id"], gf["signature"]))
                if gp is not None and gp["status"].startswith("ok") and gp["duration_seconds"]:
                    total += gp["duration_seconds"]
                    probed += 1
            inp.parts_probed = probed
            if probed:
                inp.actual_seconds = total

        ov = None
        for gf in group_files:
            ov = self.db.one("SELECT * FROM overrides WHERE file_id=? AND signature=? AND active=1"
                             " ORDER BY created_at DESC LIMIT 1", (gf["id"], gf["signature"]))
            if ov:
                break
        if ov is not None:
            inp.expected_seconds, inp.expected_source = ov["expected_seconds"], "user_override"
        elif ident is not None and ident["tmdb_id"]:
            c = self.db.one("SELECT * FROM tmdb_cache WHERE tmdb_id=?", (ident["tmdb_id"],))
            if c is not None and c["runtime_minutes"]:
                inp.expected_seconds, inp.expected_source = c["runtime_minutes"] * 60.0, "tmdb"
        ap = self.db.one("SELECT * FROM approvals WHERE file_id=? AND signature=? AND revoked_at IS NULL"
                         " ORDER BY created_at DESC LIMIT 1", (f["id"], f["signature"]))
        if ap is not None:
            inp.approved, inp.approval_reason = True, ap["reason"]
        return inp, inp.actual_seconds, inp.expected_seconds, inp.expected_source

    _RESULT_FIELDS = ("signature", "outcome", "severity", "probe_state", "identity_state", "runtime_state",
                      "health_state", "actual_seconds", "expected_seconds", "difference_seconds",
                      "expected_source", "reasons")

    def classify_all(self) -> int:
        """Recompute every result; write only rows whose content changed."""
        cfg = self.settings.get()
        files = self.db.query("SELECT * FROM files")
        changed = 0
        for f in files:
            inp, actual, expected, source = self._inputs_for(f, cfg)
            v = cls.classify(inp, cfg["thresholds"])
            v.health_state = tdarr.STATUS
            values = (f["signature"], v.outcome, v.severity, v.probe_state, v.identity_state, v.runtime_state,
                      v.health_state, actual, expected, v.difference_seconds, source, json.dumps(v.reasons))
            prev = self.db.one("SELECT * FROM results WHERE file_id=?", (f["id"],))
            if prev is not None and tuple(prev[k] for k in self._RESULT_FIELDS) == values:
                continue
            with self.db.tx() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO results(file_id, signature, outcome, severity, probe_state,"
                    " identity_state, runtime_state, health_state, actual_seconds, expected_seconds,"
                    " difference_seconds, expected_source, reasons, computed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f["id"], *values, self.clock()))
                if prev is None or prev["outcome"] != v.outcome or prev["severity"] != v.severity:
                    changed += 1
                    self.db.event(conn, f["id"], "classified", {"outcome": v.outcome, "severity": v.severity})
                    self._queue_issue_notification(conn, f, v, cfg)
        self._last_classify = self.clock()
        return changed

    def _display_path(self, path: str, cfg: dict) -> str:
        return path if cfg["notifications"]["include_full_paths"] else os.path.basename(path)

    def _queue_issue_notification(self, conn, f, v: cls.Verdict, cfg: dict) -> None:
        nc = cfg["notifications"]
        if nc["kind"] == "none" or v.outcome not in cls.EXCEPTION_OUTCOMES:
            return
        if v.severity == "high" and not nc["notify_high"]:
            return
        if v.severity != "high" and not nc["notify_review"]:
            return
        key = f"issue:{f['id']}:{f['signature']}:{v.outcome}:{v.severity}"
        title = f"RipAudit: {cls.OUTCOMES[v.outcome]}"
        body = f"{self._display_path(f['path'], cfg)}\n{v.reasons[0] if v.reasons else ''}"
        self._queue(conn, key, f["id"], title, body, v.severity)

    def _queue(self, conn, key: str, file_id: int | None, title: str, body: str, priority: str) -> bool:
        cur = conn.execute(
            "INSERT INTO notifications(dedupe_key, file_id, title, body, priority, status, attempts, next_attempt,"
            " created_at) VALUES (?,?,?,?,?, 'pending', 0, ?, ?) ON CONFLICT(dedupe_key) DO NOTHING",
            (key, file_id, title, body, priority, self.clock(), self.clock()))
        return cur.rowcount == 1

    def queue_daily_digest(self) -> bool:
        cfg = self.settings.get()
        nc = cfg["notifications"]
        if nc["kind"] == "none" or not nc["daily_digest"]:
            return False
        lt = time.localtime(self.clock())
        if lt.tm_hour < int(nc["digest_hour"]):
            return False
        key = f"digest:{time.strftime('%Y-%m-%d', lt)}"
        if self.db.scalar("SELECT 1 FROM notifications WHERE dedupe_key=?", (key,)):
            return False
        counts = {r["outcome"]: r["n"] for r in self.db.query(
            "SELECT r.outcome, COUNT(*) n FROM results r JOIN files f ON f.id=r.file_id"
            " WHERE f.state='present' GROUP BY r.outcome")}
        open_issues = sum(counts.get(o, 0) for o in cls.EXCEPTION_OUTCOMES)
        if not open_issues:
            return False
        body = "\n".join(f"{cls.OUTCOMES[o]}: {counts[o]}" for o in cls.EXCEPTION_OUTCOMES if counts.get(o))
        with self.db.tx() as conn:
            return self._queue(conn, key, None,
                               f"RipAudit daily digest: {open_issues} files need review", body, "review")

    def deliver_notifications(self) -> int:
        cfg = self.settings.get()
        nc = cfg["notifications"]
        if nc["kind"] == "none":
            return 0
        sent = 0
        for n in self.db.query("SELECT * FROM notifications WHERE status='pending' AND next_attempt <= ?"
                               " ORDER BY created_at LIMIT 20", (self.clock(),)):
            try:
                notify.send(nc, n["title"], n["body"], n["priority"], transport=self.notify_transport)
            except notify.NotifyError as exc:
                attempts = n["attempts"] + 1
                status = "failed" if attempts >= notify.MAX_ATTEMPTS else "pending"
                with self.db.tx() as conn:
                    conn.execute("UPDATE notifications SET attempts=?, status=?, next_attempt=?, last_error=?"
                                 " WHERE id=?", (attempts, status, self.clock() + notify.backoff_seconds(attempts),
                                                 self._redact(str(exc)), n["id"]))
                self.set_status("notifications", False, str(exc))
                continue
            with self.db.tx() as conn:
                conn.execute("UPDATE notifications SET status='sent', attempts=attempts+1, sent_at=? WHERE id=?",
                             (self.clock(), n["id"]))
            sent += 1
        if sent:
            self.set_status("notifications", True, f"Delivered {sent} notification(s).")
        return sent

    def send_test_notification(self) -> str:
        cfg = self.settings.get()
        try:
            notify.send(cfg["notifications"], "RipAudit test notification",
                        "Notifications from RipAudit are working.", "review", transport=self.notify_transport)
        except notify.NotifyError as exc:
            msg = self._redact(str(exc))
            self.set_status("notifications", False, msg)
            return msg
        self.set_status("notifications", True, "Test notification delivered.")
        return "Test notification delivered."

    # ----- scheduling ---------------------------------------------------
    def tick(self) -> None:
        cfg = self.settings.get()
        now = self.clock()
        did_work = False
        if self.scan_requested.is_set() or now - self._kv_float("last_discovery") >= cfg["scan_interval_minutes"] * 60:
            self.scan_requested.clear()
            self.run_discovery()
            did_work = True
        while self.process_probes():
            did_work = True
        if self.plex_configured():
            last = self._kv_float("last_plex_sync")
            awaiting = self.db.scalar(
                "SELECT COUNT(*) FROM files f LEFT JOIN identities i ON i.file_id=f.id"
                " WHERE f.state='present' AND (i.file_id IS NULL OR i.state='pending')")
            due = now - last >= cfg["plex"]["sync_interval_minutes"] * 60
            if self.plex_requested.is_set() or due or (awaiting and now - last >= PLEX_MIN_GAP_SECONDS):
                self.plex_requested.clear()
                self.plex_sync()
                did_work = True
        if self.resolve_metadata():
            did_work = True
        # Classification is cheap but touches every row; run it after new work, or every few minutes so
        # time-based states (such as the Plex waiting period) still advance.
        if did_work or now - self._last_classify >= CLASSIFY_IDLE_SECONDS:
            self.classify_all()
        self.queue_daily_digest()
        self.deliver_notifications()

    # ----- user actions -------------------------------------------------
    def action_rescan(self, file_id: int, actor: str) -> None:
        with self.db.tx() as conn:
            conn.execute("UPDATE jobs SET status='done' WHERE kind='probe' AND file_id=? AND status='queued'",
                         (file_id,))
            conn.execute("UPDATE files SET ready=1 WHERE id=? AND state='present'", (file_id,))
            conn.execute("DELETE FROM probes WHERE file_id=? AND signature=(SELECT signature FROM files WHERE id=?)",
                         (file_id, file_id))
            self.enqueue(conn, "probe", file_id)
            self.db.event(conn, file_id, "rescan_requested", None, actor)

    def action_retry_metadata(self, file_id: int, actor: str) -> None:
        with self.db.tx() as conn:
            ident = conn.execute("SELECT * FROM identities WHERE file_id=?", (file_id,)).fetchone()
            if ident is not None and ident["tmdb_id"]:
                conn.execute("DELETE FROM tmdb_cache WHERE tmdb_id=?", (ident["tmdb_id"],))
            if ident is not None and ident["state"] in ("unmatched", "ambiguous"):
                conn.execute("UPDATE identities SET state='pending', candidates=NULL, note=NULL WHERE file_id=?",
                             (file_id,))
            conn.execute("DELETE FROM kv WHERE key='tmdb_backoff_until'")
            self.db.event(conn, file_id, "metadata_retry_requested", None, actor)
        self.plex_requested.set()

    def action_select_match(self, file_id: int, tmdb_id: int, actor: str) -> None:
        with self.db.tx() as conn:
            now = self.clock()
            if conn.execute("SELECT 1 FROM identities WHERE file_id=?", (file_id,)).fetchone() is None:
                conn.execute("INSERT INTO identities(file_id, state, updated_at) VALUES (?, 'pending', ?)",
                             (file_id, now))
            conn.execute("UPDATE identities SET state='manual', tmdb_id=?, match_method='manual', note=?,"
                         " updated_at=? WHERE file_id=?",
                         (int(tmdb_id), f"Match selected manually by {actor}.", now, file_id))
            self.db.event(conn, file_id, "match_selected", {"tmdb_id": int(tmdb_id)}, actor)

    def action_set_override(self, file_id: int, seconds: float, note: str, actor: str) -> None:
        with self.db.tx() as conn:
            sig = conn.execute("SELECT signature FROM files WHERE id=?", (file_id,)).fetchone()["signature"]
            conn.execute("UPDATE overrides SET active=0 WHERE file_id=? AND active=1", (file_id,))
            conn.execute("INSERT INTO overrides(file_id, signature, expected_seconds, note, actor, created_at, active)"
                         " VALUES (?,?,?,?,?,?,1)", (file_id, sig, seconds, note, actor, self.clock()))
            self.db.event(conn, file_id, "override_set", {"expected_seconds": seconds, "note": note}, actor)

    def action_clear_override(self, file_id: int, actor: str) -> None:
        with self.db.tx() as conn:
            conn.execute("UPDATE overrides SET active=0 WHERE file_id=? AND active=1", (file_id,))
            self.db.event(conn, file_id, "override_cleared", None, actor)

    def action_approve(self, file_id: int, reason: str, preserve: bool, actor: str) -> None:
        if not reason.strip():
            raise ValueError("An approval reason is required.")
        with self.db.tx() as conn:
            sig = conn.execute("SELECT signature FROM files WHERE id=?", (file_id,)).fetchone()["signature"]
            conn.execute("INSERT INTO approvals(file_id, signature, reason, preserve_on_replace, actor, created_at)"
                         " VALUES (?,?,?,?,?,?)", (file_id, sig, reason.strip(), int(preserve), actor, self.clock()))
            self.db.event(conn, file_id, "approved", {"reason": reason.strip(), "preserve": preserve}, actor)

    def action_reopen(self, file_id: int, reason: str, actor: str) -> None:
        with self.db.tx() as conn:
            conn.execute("UPDATE approvals SET revoked_at=?, revoked_by=?, revoke_reason=? WHERE file_id=?"
                         " AND revoked_at IS NULL", (self.clock(), actor, reason or "Reopened", file_id))
            self.db.event(conn, file_id, "reopened", {"reason": reason}, actor)


class Worker(threading.Thread):
    """Background loop. Stops cleanly between units of work."""

    def __init__(self, engine: Engine, interval: float = 5.0):
        super().__init__(name="ripaudit-worker", daemon=True)
        self.engine = engine
        self.interval = interval
        self.stop_event = threading.Event()

    def run(self) -> None:
        self.engine.recover()
        while not self.stop_event.is_set():
            try:
                self.engine.tick()
            except Exception:  # noqa: BLE001 - keep the loop alive; error is logged
                log.exception("Worker tick failed")
            self.stop_event.wait(self.interval)

    def stop(self, timeout: float = 30.0) -> None:
        self.stop_event.set()
        self.join(timeout)

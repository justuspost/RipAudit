"""Media discovery.

Discovery only reads directory entries and ``stat`` results; it never reads
file contents. A file is identified by its normalized container path, and its
signature is ``size:mtime_ns``. A changed signature triggers a re-audit.

Readiness: a file is ready for probing when its signature has been observed
unchanged for ``stability_observations`` consecutive scans *and* its mtime is
at least ``min_file_age_seconds`` old. Stability is a heuristic only: a paused
rip also looks stable. The recommended workflow is to rip into a staging folder
outside the monitored roots and move completed files in.

Unavailable-mount guard: a root that is missing, unreadable, or suddenly empty
(while files were previously known) is reported as unavailable and its known
files are left untouched. If more than ``missing_guard_percent`` of a root's
known files vanish in one scan, missing-marking is held back for that root.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from .db import Database


def signature_of(size: int, mtime_ns: int) -> str:
    return f"{size}:{mtime_ns}"


@dataclass
class RootReport:
    root: str
    status: str = "ok"           # ok | unavailable | guarded
    found: int = 0
    message: str = ""


@dataclass
class ScanReport:
    roots: list[RootReport] = field(default_factory=list)
    new: int = 0
    changed: int = 0
    missing: int = 0
    returned: int = 0
    ready: list[int] = field(default_factory=list)
    changed_ids: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _ignored(rel: str, patterns: list[str]) -> bool:
    rel = "/" + os.path.normpath(rel).replace(os.sep, "/").lstrip("/")
    if rel == "/.":
        return False
    return any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(os.path.basename(rel), p) for p in patterns)


def walk_root(root: str, extensions: list[str], patterns: list[str], errors: list[str]) -> dict[str, os.stat_result]:
    """Return {normalized_path: stat} for candidate media files under root."""
    found: dict[str, os.stat_result] = {}
    real_root = os.path.realpath(root)
    exts = {e.lower() for e in extensions}

    def onerror(err: OSError) -> None:
        errors.append(f"{err.filename}: {err.strerror}")

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=onerror):
        rel_dir = os.path.relpath(dirpath, root)
        dirnames[:] = [d for d in dirnames
                       if not _ignored(os.path.join(rel_dir, d, "x"), patterns)
                       and not os.path.islink(os.path.join(dirpath, d))]
        for name in filenames:
            full = os.path.join(dirpath, name)
            if os.path.splitext(name)[1].lower() not in exts:
                continue
            rel = os.path.relpath(full, root)
            if _ignored(rel, patterns):
                continue
            try:
                if os.path.islink(full):
                    target = os.path.realpath(full)
                    if not (target == real_root or target.startswith(real_root + os.sep)):
                        errors.append(f"{full}: symlink points outside the media root; skipped")
                        continue
                st = os.stat(full)
            except OSError as exc:
                errors.append(f"{full}: {exc.strerror}")
                continue
            found[os.path.normpath(full)] = st
    return found


def discover(db: Database, settings: dict, now: float | None = None) -> ScanReport:
    now = time.time() if now is None else now
    report = ScanReport()
    need = max(0, int(settings["stability_observations"]))
    min_age = max(0, float(settings["min_file_age_seconds"]))
    guard_pct = float(settings.get("missing_guard_percent", 50))

    for root in settings["media_roots"]:
        rr = RootReport(root=root)
        report.roots.append(rr)
        root_path = Path(root)
        known = {r["path"]: r for r in db.query(
            "SELECT * FROM files WHERE root = ? AND state = 'present'", (root,))}
        if not root_path.is_dir() or not os.access(root, os.R_OK | os.X_OK):
            rr.status, rr.message = "unavailable", "Root is missing or unreadable; known files left unchanged."
            continue
        errors: list[str] = []
        found = walk_root(root, settings["extensions"], settings["ignore_patterns"], errors)
        report.errors.extend(errors)
        rr.found = len(found)
        if not found and known:
            rr.status = "unavailable"
            rr.message = (f"Root returned no media but {len(known)} files were known; "
                          "treated as an unavailable mount, not deletions.")
            continue
        vanished = [p for p in known if p not in found]
        hold_missing = bool(known) and len(vanished) * 100.0 / len(known) > guard_pct
        guard_key = f"missing_guard:{root}"
        if hold_missing:
            fingerprint = hashlib.sha256("\n".join(sorted(vanished)).encode()).hexdigest()
            previous = db.scalar("SELECT value FROM kv WHERE key = ?", (guard_key,))
            if previous == fingerprint:
                hold_missing = False  # the same files were absent on two consecutive scans
            else:
                with db.tx() as conn:
                    conn.execute("INSERT OR REPLACE INTO kv(key, value) VALUES (?, ?)", (guard_key, fingerprint))
        if not hold_missing:
            with db.tx() as conn:
                conn.execute("DELETE FROM kv WHERE key = ?", (guard_key,))
        if hold_missing:
            rr.status = "guarded"
            rr.message = (f"{len(vanished)} of {len(known)} known files vanished in one scan; "
                          "missing-marking held until the next scan confirms it.")

        with db.tx() as conn:
            for path, st in found.items():
                sig = signature_of(st.st_size, st.st_mtime_ns)
                row = conn.execute("SELECT * FROM files WHERE path = ?", (path,)).fetchone()
                aged = (now - st.st_mtime) >= min_age
                if row is None:
                    ready = int(need == 0 and aged)
                    cur = conn.execute(
                        "INSERT INTO files(path, root, size, mtime_ns, signature, state, stable_count, ready,"
                        " first_seen, last_seen, signature_changed_at) VALUES (?,?,?,?,?, 'present', 0, ?, ?,?,?)",
                        (path, root, st.st_size, st.st_mtime_ns, sig, ready, now, now, now))
                    fid = cur.lastrowid
                    db.event(conn, fid, "discovered", {"size": st.st_size})
                    report.new += 1
                    if ready:
                        report.ready.append(fid)
                    continue
                fid = row["id"]
                if row["state"] != "present":
                    db.event(conn, fid, "returned", None)
                    report.returned += 1
                if row["signature"] != sig:
                    conn.execute(
                        "UPDATE files SET size=?, mtime_ns=?, signature=?, state='present', stable_count=0,"
                        " ready=?, last_seen=?, signature_changed_at=? WHERE id=?",
                        (st.st_size, st.st_mtime_ns, sig, int(need == 0 and aged), now, now, fid))
                    db.event(conn, fid, "signature_changed",
                             {"old": row["signature"], "new": sig})
                    report.changed += 1
                    report.changed_ids.append(fid)
                    if need == 0 and aged:
                        report.ready.append(fid)
                    continue
                stable = row["stable_count"] + 1
                ready = int(stable >= need and aged)
                conn.execute("UPDATE files SET state='present', stable_count=?, ready=?, last_seen=? WHERE id=?",
                             (stable, ready, now, fid))
                if ready and not row["ready"]:
                    db.event(conn, fid, "ready", {"observations": stable})
                if ready:
                    report.ready.append(fid)
            if not hold_missing:
                for path in vanished:
                    fid = known[path]["id"]
                    conn.execute("UPDATE files SET state='missing', ready=0 WHERE id=?", (fid,))
                    db.event(conn, fid, "missing", None)
                    report.missing += 1
    return report

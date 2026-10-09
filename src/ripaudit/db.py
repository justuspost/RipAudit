"""SQLite access and schema migrations."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Any


class Database:
    """Small wrapper: one connection per thread, WAL mode, serialized writes."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self.migrate()

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            try:
                os.chmod(self.path, 0o600)  # contains password hashes and audit history
            except OSError:
                pass
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        conn = self._conn()
        with self._write_lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return self._conn().execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return self._conn().execute(sql, params).fetchone()

    def scalar(self, sql: str, params: tuple | dict = ()) -> Any:
        row = self.one(sql, params)
        return row[0] if row else None

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ----- migrations ---------------------------------------------------
    def migrate(self) -> None:
        conn = self._conn()
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at REAL NOT NULL)"
        )
        applied = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
        files = sorted(
            (p for p in resources.files("ripaudit.migrations").iterdir() if p.name.endswith(".sql")),
            key=lambda p: p.name,
        )
        for f in files:
            version = f.name.split("_", 1)[0]
            if version in applied:
                continue
            sql = f.read_text(encoding="utf-8")
            with self._write_lock:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for statement in _split_sql(sql):
                        conn.execute(statement)
                    conn.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)", (version, time.time())
                    )
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise

    # ----- helpers ------------------------------------------------------
    def event(self, conn: sqlite3.Connection, file_id: int | None, kind: str, detail: Any = None,
              actor: str = "system") -> None:
        text = detail if isinstance(detail, str) or detail is None else json.dumps(detail, sort_keys=True)
        conn.execute(
            "INSERT INTO events(file_id, ts, kind, detail, actor) VALUES (?, ?, ?, ?, ?)",
            (file_id, time.time(), kind, text, actor),
        )


def _split_sql(sql: str) -> list[str]:
    lines = [ln for ln in sql.splitlines() if not ln.strip().startswith("--")]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]

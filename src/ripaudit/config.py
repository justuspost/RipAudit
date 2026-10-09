"""Settings storage.

Settings live in ``<config_dir>/settings.json`` and are written atomically
(temporary file + fsync + rename). Secrets may also be supplied through
environment variables, which take precedence and are never written to disk.
"""

from __future__ import annotations

import copy
import json
import os
import secrets
import tempfile
import threading
from pathlib import Path
from typing import Any

SECRET_FIELDS: tuple[tuple[str, str], ...] = (
    ("plex", "token"),
    ("tmdb", "credential"),
    ("notifications", "token"),
)

ENV_SECRETS = {
    ("plex", "token"): "RIPAUDIT_PLEX_TOKEN",
    ("tmdb", "credential"): "RIPAUDIT_TMDB_CREDENTIAL",
    ("notifications", "token"): "RIPAUDIT_NOTIFY_TOKEN",
}

DEFAULTS: dict[str, Any] = {
    "media_roots": ["/media"],
    "extensions": [".mkv", ".mp4"],
    "ignore_patterns": ["*.part", "*.partial", "*.tmp", "*/.*", "*/@eaDir/*", "*/#recycle/*"],
    "scan_interval_minutes": 15,
    "stability_observations": 1,
    "min_file_age_seconds": 120,
    "probe_concurrency": 1,
    "probe_timeout_seconds": 120,
    "missing_guard_percent": 50,
    "plex": {
        "url": "",
        "token": "",
        "library_keys": [],
        "path_mappings": [],
        "sync_interval_minutes": 30,
        "pending_grace_hours": 48,
        "verify_tls": True,
    },
    "tmdb": {
        "credential": "",
        "title_search_fallback": False,
        "cache_days": 30,
    },
    "thresholds": {
        "short_review_seconds": 120,
        "short_high_seconds": 300,
        "short_review_percent": 3.0,
        "short_high_percent": 10.0,
        "long_review_seconds": 120,
        "long_review_percent": 3.0,
    },
    "notifications": {
        "kind": "none",
        "url": "",
        "token": "",
        "include_full_paths": False,
        "notify_high": True,
        "notify_review": False,
        "daily_digest": False,
        "digest_hour": 8,
    },
    "session_cookie_secure": False,
}


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _merge(out[key], value)
        elif key in out:
            out[key] = value
    return out


class Settings:
    """Thread-safe settings store backed by an atomically written JSON file."""

    def __init__(self, config_dir: Path, environ: dict[str, str] | None = None):
        self.config_dir = Path(config_dir)
        self.path = self.config_dir / "settings.json"
        self.environ = dict(os.environ if environ is None else environ)
        self._lock = threading.RLock()
        self._data = self._load()

    def _load(self) -> dict[str, Any]:
        data = copy.deepcopy(DEFAULTS)
        roots_env = self.environ.get("RIPAUDIT_MEDIA_ROOTS")
        if roots_env:
            data["media_roots"] = [r.strip() for r in roots_env.split(",") if r.strip()]
        if self.path.exists():
            stored = json.loads(self.path.read_text(encoding="utf-8"))
            data = _merge(data, stored)
        return data

    # ----- access -------------------------------------------------------
    def get(self) -> dict[str, Any]:
        """Return a copy with environment-provided secrets applied."""
        with self._lock:
            data = copy.deepcopy(self._data)
        for (section, field), env_name in ENV_SECRETS.items():
            if self.environ.get(env_name):
                data[section][field] = self.environ[env_name]
        return data

    def secret_source(self, section: str, field: str) -> str:
        if self.environ.get(ENV_SECRETS[(section, field)]):
            return "environment"
        with self._lock:
            return "settings" if self._data[section][field] else "unset"

    def secret_values(self) -> list[str]:
        data = self.get()
        return [data[s][f] for s, f in SECRET_FIELDS if data[s][f]]

    def masked(self) -> dict[str, Any]:
        """Settings safe to render or export: secrets replaced by a marker."""
        data = self.get()
        for section, field in SECRET_FIELDS:
            data[section][field] = "********" if data[section][field] else ""
        return data

    # ----- update -------------------------------------------------------
    def update(self, new: dict[str, Any]) -> None:
        """Merge and persist. Empty secret values keep the stored secret."""
        with self._lock:
            merged = _merge(self._data, new)
            for section, field in SECRET_FIELDS:
                incoming = new.get(section, {}).get(field) if isinstance(new.get(section), dict) else None
                if incoming in (None, "", "********"):
                    merged[section][field] = self._data[section][field]
            self._write(merged)
            self._data = merged

    def clear_secret(self, section: str, field: str) -> None:
        with self._lock:
            data = copy.deepcopy(self._data)
            data[section][field] = ""
            self._write(data)
            self._data = data

    def _write(self, data: dict[str, Any]) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.config_dir, prefix=".settings-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise


def load_or_create_secret(config_dir: Path, name: str, nbytes: int = 32) -> str:
    """Return a persistent random secret stored with 0600 permissions."""
    path = Path(config_dir) / name
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    Path(config_dir).mkdir(parents=True, exist_ok=True)
    value = secrets.token_urlsafe(nbytes)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(value)
    return value

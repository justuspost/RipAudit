"""Shared fixtures: temporary config/media dirs, fake clock, mocked Plex and TMDB."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from ripaudit.config import Settings
from ripaudit.db import Database
from ripaudit.engine import Engine
from ripaudit.probe import ProbeResult

PLEX_URL = "http://plex.test:32400"
PLEX_TOKEN = "plex-secret-token-123"
TMDB_KEY = "tmdb-secret-key-456"


class Clock:
    def __init__(self):
        self.now = time.time() + 10_000

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakePlex:
    """In-memory Plex server: movies are dicts in the JSON shape Plex returns."""

    def __init__(self):
        self.movies: list[dict] = []
        self.down = False
        self.requests: list[httpx.Request] = []

    def add(self, title, year, files, tmdb=None, imdb=None, edition=None, rating_key=None, media_id=None):
        guids = []
        if tmdb:
            guids.append({"id": f"tmdb://{tmdb}"})
        if imdb:
            guids.append({"id": f"imdb://{imdb}"})
        rk = rating_key or str(1000 + len(self.movies))
        item = {"ratingKey": rk, "type": "movie", "title": title, "year": year, "Guid": guids,
                "Media": [{"id": media_id or f"m{rk}", "Part": [{"id": f"p{rk}{i}", "file": f}
                                                                for i, f in enumerate(files)]}]}
        if edition:
            item["editionTitle"] = edition
        self.movies.append(item)
        return item

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        if request.headers.get("X-Plex-Token") != PLEX_TOKEN:
            return httpx.Response(401)
        if request.url.path == "/library/sections":
            return httpx.Response(200, json={"MediaContainer": {"Directory": [
                {"key": "1", "title": "Movies", "type": "movie"}, {"key": "2", "title": "TV", "type": "show"}]}})
        if request.url.path == "/library/sections/1/all":
            start = int(request.headers.get("X-Plex-Container-Start", "0"))
            size = int(request.headers.get("X-Plex-Container-Size", "200"))
            page = self.movies[start:start + size]
            return httpx.Response(200, json={"MediaContainer": {"totalSize": len(self.movies), "Metadata": page}})
        return httpx.Response(404)


class FakeTmdb:
    def __init__(self):
        self.movies: dict[int, dict] = {}
        self.imdb: dict[str, list[int]] = {}
        self.search_results: list[dict] = []
        self.mode = "ok"  # ok | ratelimit | error
        self.calls = 0

    def add(self, tmdb_id, title, year, runtime, imdb=None):
        self.movies[tmdb_id] = {"id": tmdb_id, "title": title, "release_date": f"{year}-01-01",
                                "runtime": runtime, "imdb_id": imdb}
        if imdb:
            self.imdb.setdefault(imdb, []).append(tmdb_id)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.mode == "ratelimit":
            return httpx.Response(429, headers={"Retry-After": "7"})
        if self.mode == "error":
            return httpx.Response(503)
        if request.url.params.get("api_key") != TMDB_KEY:
            return httpx.Response(401)
        parts = request.url.path.strip("/").split("/")
        if parts[:2] == ["3", "movie"]:
            m = self.movies.get(int(parts[2]))
            return httpx.Response(200, json=m) if m else httpx.Response(404)
        if parts[:2] == ["3", "find"]:
            ids = self.imdb.get(parts[2], [])
            return httpx.Response(200, json={"movie_results": [{"id": i} for i in ids]})
        if parts[:2] == ["3", "search"]:
            return httpx.Response(200, json={"results": self.search_results})
        return httpx.Response(404)


class FakeProbe:
    """Stands in for ffprobe: durations keyed by file name; default 7200s."""

    def __init__(self):
        self.durations: dict[str, float | None] = {}
        self.status: dict[str, str] = {}
        self.calls: list[str] = []
        self.side_effect = None

    def __call__(self, path: Path, timeout: float) -> ProbeResult:
        self.calls.append(path.name)
        if self.side_effect:
            self.side_effect(path)
        status = self.status.get(path.name, "ok")
        if status != "ok":
            return ProbeResult(status=status, diagnostics=f"simulated {status}")
        d = self.durations.get(path.name, 7200.0)
        if d is None:
            return ProbeResult(status="invalid_duration")
        return ProbeResult(status="ok", duration_seconds=d, duration_source="container", container_duration=d,
                           video_codec="hevc", width=3840, height=2160, video_stream_count=1, audio_count=1,
                           audio_languages=["eng"])


@pytest.fixture
def env(tmp_path):
    config = tmp_path / "config"
    media = tmp_path / "media"
    (media / "Movies").mkdir(parents=True)
    settings = Settings(config, environ={})
    settings.update({
        "media_roots": [str(media)], "stability_observations": 0, "min_file_age_seconds": 0,
        "plex": {"url": PLEX_URL, "token": PLEX_TOKEN, "path_mappings": [{"plex": "/data", "container": str(media)}]},
        "tmdb": {"credential": TMDB_KEY},
    })
    db = Database(config / "ripaudit.db")
    fplex, ftmdb, fprobe, clock = FakePlex(), FakeTmdb(), FakeProbe(), Clock()
    engine = Engine(db, settings, clock=clock, plex_transport=httpx.MockTransport(fplex.handler),
                    tmdb_transport=httpx.MockTransport(ftmdb.handler), ffprobe_runner=fprobe)

    class Env:
        pass

    e = Env()
    e.tmp, e.config, e.media, e.settings, e.db, e.engine = tmp_path, config, media, settings, db, engine
    e.plex, e.tmdb, e.probe, e.clock = fplex, ftmdb, fprobe, clock

    def write(rel, data=b"x" * 64):
        p = media / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def result(rel):
        return db.one("SELECT r.* FROM results r JOIN files f ON f.id=r.file_id WHERE f.path=?",
                      (os.path.normpath(str(media / rel)),))

    def file_id(rel):
        return db.scalar("SELECT id FROM files WHERE path=?", (os.path.normpath(str(media / rel)),))

    def run():
        engine.run_discovery()
        while engine.process_probes():
            pass
        engine.plex_sync()
        engine.resolve_metadata()
        engine.classify_all()

    e.write, e.result, e.file_id, e.run = write, result, file_id, run
    yield e
    db.close()


def make_media(path: Path, seconds: float) -> Path:
    """Create a tiny synthetic Matroska file with ffmpeg's test pattern."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg not installed")
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size=64x36:rate=5:duration={seconds}",
                    "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:v", "mpeg4", "-c:a", "aac",
                    "-metadata:s:a:0", "language=eng", "-f", "matroska", str(path)], check=True)
    return path


def dumps(o):
    return json.dumps(o)

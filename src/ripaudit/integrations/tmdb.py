"""TMDB adapter (read-only).

Endpoints used (TMDB API v3; see docs/integrations.md):
* ``GET /3/movie/{movie_id}`` — ``runtime`` (minutes), ``title``, ``release_date``, ``imdb_id``.
* ``GET /3/find/{external_id}?external_source=imdb_id`` — ``movie_results``.
* ``GET /3/search/movie?query=&year=`` — only when title-search fallback is enabled;
  results are always presented for manual selection, never auto-accepted.

Authentication: an API Read Access Token is sent as ``Authorization: Bearer``; a
v3 API key is sent as the ``api_key`` query parameter. HTTP 429 responses honor
``Retry-After`` via :class:`RateLimited`.

TMDB runtime is a whole-minute general figure for the title, not an
authoritative runtime for a specific disc edition.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx

BASE_URL = os.environ.get("RIPAUDIT_TMDB_BASE_URL", "https://api.themoviedb.org")  # override for local testing


class TmdbError(Exception):
    pass


class RateLimited(TmdbError):
    def __init__(self, retry_after: float):
        super().__init__(f"TMDB rate limit; retry after {retry_after:.0f}s")
        self.retry_after = retry_after


class NotFound(TmdbError):
    pass


@dataclass
class TmdbMovie:
    tmdb_id: int
    title: str
    year: int | None
    runtime_minutes: int | None
    imdb_id: str | None


def _year(date: str | None) -> int | None:
    return int(date[:4]) if date and date[:4].isdigit() else None


class TmdbClient:
    def __init__(self, credential: str, timeout: float = 20.0, transport: httpx.BaseTransport | None = None,
                 base_url: str = BASE_URL):
        if not credential:
            raise TmdbError("TMDB credential is not configured.")
        headers = {"Accept": "application/json"}
        self._params: dict[str, str] = {}
        if credential.startswith("eyJ"):  # JWT-style API Read Access Token
            headers["Authorization"] = f"Bearer {credential}"
        else:
            self._params["api_key"] = credential
        self._client = httpx.Client(base_url=base_url, timeout=timeout, headers=headers, transport=transport)

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str, params: dict | None = None) -> dict:
        try:
            resp = self._client.get(path, params={**self._params, **(params or {})})
        except httpx.HTTPError as exc:
            raise TmdbError(f"TMDB request failed: {type(exc).__name__}") from None
        if resp.status_code == 429:
            try:
                retry = float(resp.headers.get("Retry-After", "10"))
            except ValueError:
                retry = 10.0
            raise RateLimited(retry)
        if resp.status_code == 404:
            raise NotFound("TMDB has no record for that ID.")
        if resp.status_code == 401:
            raise TmdbError("TMDB rejected the credential (401).")
        if resp.status_code >= 400:
            raise TmdbError(f"TMDB returned HTTP {resp.status_code}.")
        try:
            return resp.json()
        except ValueError:
            raise TmdbError("TMDB returned a non-JSON response.") from None

    def movie(self, tmdb_id: int) -> TmdbMovie:
        d = self._get(f"/3/movie/{int(tmdb_id)}")
        runtime = d.get("runtime")
        return TmdbMovie(tmdb_id=int(d["id"]), title=d.get("title") or "", year=_year(d.get("release_date")),
                         runtime_minutes=int(runtime) if isinstance(runtime, int) and runtime > 0 else None,
                         imdb_id=d.get("imdb_id") or None)

    def find_imdb(self, imdb_id: str) -> list[int]:
        if not imdb_id.startswith("tt") or not imdb_id[2:].isdigit():
            raise TmdbError("Invalid IMDb ID.")
        d = self._get(f"/3/find/{imdb_id}", {"external_source": "imdb_id"})
        return [int(m["id"]) for m in d.get("movie_results") or [] if m.get("id")]

    def search(self, title: str, year: int | None) -> list[dict]:
        params: dict = {"query": title, "include_adult": "false"}
        if year:
            params["year"] = year
        d = self._get("/3/search/movie", params)
        return [{"tmdb_id": int(r["id"]), "title": r.get("title"), "year": _year(r.get("release_date"))}
                for r in (d.get("results") or [])[:10] if r.get("id")]

"""Plex Media Server adapter (read-only).

Endpoints used (see docs/integrations.md for references):
* ``GET /library/sections`` — list libraries.
* ``GET /library/sections/{key}/all?includeGuids=1`` — movies with ``Guid`` external IDs,
  ``editionTitle``, and ``Media`` -> ``Part`` file paths. Paged with
  ``X-Plex-Container-Start`` / ``X-Plex-Container-Size``.

The token is sent in the ``X-Plex-Token`` header, never in the URL, so it does
not appear in access logs. RipAudit never writes to Plex.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import httpx

PAGE_SIZE = 200
_LEGACY_IMDB = re.compile(r"com\.plexapp\.agents\.imdb://(tt\d+)")
_LEGACY_TMDB = re.compile(r"com\.plexapp\.agents\.themoviedb://(\d+)")


class PlexError(Exception):
    pass


@dataclass
class PlexPart:
    part_id: str
    file: str
    index: int
    count: int
    duration_ms: int | None = None


@dataclass
class PlexVersion:
    rating_key: str
    media_id: str
    title: str
    year: int | None
    edition: str | None
    library: str
    guids: list[str] = field(default_factory=list)
    tmdb_id: int | None = None
    imdb_id: str | None = None
    parts: list[PlexPart] = field(default_factory=list)


def parse_guids(item: dict) -> tuple[list[str], int | None, str | None]:
    guids = [g.get("id", "") for g in item.get("Guid") or [] if g.get("id")]
    tmdb_id = imdb_id = None
    for g in guids:
        if g.startswith("tmdb://") and g[7:].isdigit():
            tmdb_id = int(g[7:])
        elif g.startswith("imdb://tt"):
            imdb_id = g[7:]
    legacy = item.get("guid") or ""
    if tmdb_id is None and (m := _LEGACY_TMDB.search(legacy)):
        tmdb_id = int(m.group(1))
    if imdb_id is None and (m := _LEGACY_IMDB.search(legacy)):
        imdb_id = m.group(1)
    return guids, tmdb_id, imdb_id


def parse_movies(container: dict, library: str) -> list[PlexVersion]:
    """Turn a MediaContainer into one PlexVersion per Media element.

    Each Media element is a separate version of the movie. Each Part of a
    Media element is one file of a multipart version.
    """
    out: list[PlexVersion] = []
    for item in (container.get("MediaContainer") or {}).get("Metadata") or []:
        if item.get("type") not in (None, "movie"):
            continue
        guids, tmdb_id, imdb_id = parse_guids(item)
        for media in item.get("Media") or []:
            parts = media.get("Part") or []
            version = PlexVersion(
                rating_key=str(item.get("ratingKey")),
                media_id=str(media.get("id")),
                title=item.get("title") or "",
                year=item.get("year"),
                edition=item.get("editionTitle") or None,
                library=library,
                guids=guids, tmdb_id=tmdb_id, imdb_id=imdb_id,
            )
            for idx, part in enumerate(parts, start=1):
                if part.get("file"):
                    version.parts.append(PlexPart(part_id=str(part.get("id")), file=part["file"], index=idx,
                                                  count=len(parts), duration_ms=part.get("duration")))
            out.append(version)
    return out


class PlexClient:
    def __init__(self, url: str, token: str, verify_tls: bool = True, timeout: float = 30.0,
                 transport: httpx.BaseTransport | None = None):
        if not url or not token:
            raise PlexError("Plex URL and token must be configured.")
        self._client = httpx.Client(
            base_url=url.rstrip("/"), timeout=timeout, verify=verify_tls, transport=transport,
            headers={"X-Plex-Token": token, "Accept": "application/json",
                     "X-Plex-Product": "RipAudit", "X-Plex-Client-Identifier": "ripaudit"},
        )

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str, params: dict | None = None, headers: dict | None = None) -> dict:
        try:
            resp = self._client.get(path, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise PlexError(f"Plex request failed: {type(exc).__name__}") from None
        if resp.status_code == 401:
            raise PlexError("Plex rejected the token (401).")
        if resp.status_code >= 400:
            raise PlexError(f"Plex returned HTTP {resp.status_code}.")
        try:
            return resp.json()
        except ValueError:
            raise PlexError("Plex returned a non-JSON response.") from None

    def sections(self) -> list[dict]:
        data = self._get("/library/sections")
        return [{"key": str(d.get("key")), "title": d.get("title"), "type": d.get("type")}
                for d in (data.get("MediaContainer") or {}).get("Directory") or []]

    def movies(self, section_key: str, section_title: str) -> list[PlexVersion]:
        versions: list[PlexVersion] = []
        start = 0
        while True:
            data = self._get(f"/library/sections/{section_key}/all", params={"includeGuids": 1, "type": 1},
                             headers={"X-Plex-Container-Start": str(start),
                                      "X-Plex-Container-Size": str(PAGE_SIZE)})
            mc = data.get("MediaContainer") or {}
            batch = mc.get("Metadata") or []
            versions.extend(parse_movies(data, section_title))
            total = mc.get("totalSize")
            start += len(batch)
            if not batch or len(batch) < PAGE_SIZE or (total is not None and start >= int(total)):
                break
        return versions


# ----- path mapping -----------------------------------------------------
def _norm(path: str) -> str:
    p = path.replace("\\", "/")
    while "//" in p:
        p = p.replace("//", "/")
    return p.rstrip("/") if len(p) > 1 else p


def map_plex_path(plex_path: str, mappings: list[dict]) -> str | None:
    """Translate a Plex-visible path to a container path by longest-prefix match."""
    path = _norm(plex_path)
    best = None
    for m in mappings:
        prefix = _norm(m.get("plex", ""))
        if not prefix:
            continue
        if (path == prefix or path.startswith(prefix + "/")) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, _norm(m.get("container", "")))
    if best is None:
        return None
    prefix, target = best
    rest = path[len(prefix):]
    if ".." in rest.split("/"):
        return None
    return target + rest


def validate_mappings(mappings: list[dict], media_roots: list[str]) -> list[str]:
    problems = []
    seen = set()
    roots = [_norm(r) for r in media_roots]
    for m in mappings:
        plex, cont = _norm(m.get("plex", "")), _norm(m.get("container", ""))
        if not plex or not cont:
            problems.append("Each mapping needs both a Plex path and a container path.")
            continue
        if plex in seen:
            problems.append(f"Duplicate Plex prefix: {plex}")
        seen.add(plex)
        if ".." in cont.split("/") or ".." in plex.split("/"):
            problems.append(f"Mapping may not contain '..': {plex} -> {cont}")
        if not any(cont == r or cont.startswith(r + "/") or r.startswith(cont + "/") for r in roots):
            problems.append(f"Container path {cont} is not inside a configured media root.")
    return problems

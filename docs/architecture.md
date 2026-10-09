# Architecture and roadmap

## Architecture

A single container running one Python process:

- **FastAPI web app** (`web.py`): server-rendered Jinja2 pages, form actions with CSRF protection, exports, diagnostics, `/healthz`.
- **Background worker** (`engine.Worker`): one thread calling `Engine.tick()` every 5 seconds. Each tick runs a bounded amount of work: discovery when due, probe jobs, Plex sync when due, TMDB lookups, classification, and notification delivery.
- **SQLite** (`db.py`, WAL mode) with versioned SQL migrations. All job and scan state is durable; at startup, interrupted jobs are requeued and interrupted scans are marked.
- **Adapters** (`integrations/`): Plex, TMDB, notifications, and the deferred Tdarr boundary.
- **Pure classification** (`classify.py`): results are recomputed from stored probe and metadata data, so threshold changes never require re-probing.

No Redis, Celery, or external database is required. FFprobe runs as a subprocess in a small thread pool.

```
src/ripaudit/
  __main__.py      entry point (uvicorn)
  web.py           routes, auth, settings, exports
  engine.py        pipeline orchestration and user actions
  discovery.py     directory walk, signatures, stability, unavailable-root guard
  probe.py         FFprobe invocation and parsing
  classify.py      thresholds and outcomes
  config.py        settings file and secrets
  db.py            SQLite and migrations
  security.py      hashing, redaction, path confinement, URL checks
  integrations/    plex.py, tmdb.py, notify.py, tdarr.py
  migrations/      0001_initial.sql
  templates/       Jinja2 pages
  static/          CSS, icons, TMDB logo
tests/             pytest suite with synthetic media and mocked services
unraid/            Unraid template (development artifact)
docs/              guides, screenshots, icon
scripts/           development mock services and demo fixture
```

## Assumptions

- Plex is the authority for which movie a file is; RipAudit does not guess from file names unless title search is enabled, and even then a person confirms.
- TMDB's runtime is a general figure for the title and can describe a different cut than a particular disc.
- Size plus modification time is a sufficient change signal for media files that are written once.
- One administrator account is enough for a home server.

## Unresolved integration details

- Behavior against a real Plex server (pagination limits, very large libraries, Plex editions created as separate items) has not been exercised outside mocks.
- Some Plex agents expose only legacy GUIDs; both forms are parsed, but coverage across agents is unverified.
- No supported Tdarr per-file results interface was verified, so decode-health import is deferred.

## Phases

**MVP (this release):** discovery, stability, FFprobe, Plex identity with path mappings, versions, and multipart movies, TMDB runtime with caching and back-off, classification, review queue, inventory, file pages, overrides, approvals, exports, ntfy/webhook notifications, first-run authentication, Docker image, CI, and the Unraid template.

**Next:**
1. Live validation against real Plex, TMDB, and ntfy, and on full-size UHD remuxes.
2. Tdarr decode-health import, if a stable, documented results interface is confirmed.
3. A drop-folder or API endpoint for recording the source runtime at rip time.
4. Bulk actions in the review queue.
5. Additional notification targets (for example Discord or Apprise).
6. Optional multiple accounts.

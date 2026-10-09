# Changelog

All notable changes to this project are documented here. The format follows Keep a Changelog, and versions follow Semantic Versioning.

## [Unreleased]

### Added
- Media discovery with stability checks, ignore patterns, and unavailable-share protection.
- FFprobe inspection of duration, codec, resolution, and track inventory, with timeouts and retries.
- Plex identity via configurable path mappings, including multiple versions, editions, and multipart movies.
- TMDB runtime lookup by TMDB or IMDb ID with caching and rate-limit back-off; optional title search with manual selection.
- Runtime classification with combined absolute and percentage thresholds and separate probe, identity, runtime, and decode-health states.
- Review queue, inventory with search, filters, and sorting, per-file details and history, CSV/JSON exports, sanitized diagnostics.
- Source-runtime overrides and approvals scoped to the exact file version.
- Deduplicated ntfy and webhook notifications with retry and an optional daily digest.
- First-run setup token, scrypt password hashing, CSRF protection, sign-in lockout, and secret redaction.
- Dockerfile running as a non-root user, Compose example, CI with tests, dependency audit, container smoke test, and image scan, plus a gated release workflow.
- Unraid template and Community Applications profile (development artifacts).
- MIT License.

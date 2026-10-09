# Integrations

All integrations are read-only and isolated in `src/ripaudit/integrations/`. Automated tests use mocked services only; see "Verification status" below.

## Plex Media Server

| Use | Request |
|---|---|
| List libraries | `GET /library/sections` |
| List movies with external IDs, editions, and file paths | `GET /library/sections/{key}/all?includeGuids=1&type=1`, paged with `X-Plex-Container-Start` / `X-Plex-Container-Size` headers |

- Authentication: `X-Plex-Token` header (never placed in the URL). `Accept: application/json`.
- Fields read: `ratingKey`, `title`, `year`, `editionTitle`, `Guid[].id` (`tmdb://`, `imdb://`), legacy `guid` (`com.plexapp.agents.imdb://`, `com.plexapp.agents.themoviedb://`), `Media[].id`, `Media[].Part[].id`, `Media[].Part[].file`.
- RipAudit never writes to Plex.

References: [Plex Media Server developer documentation](https://developer.plex.tv/pms/index.html), [Plexopedia: Get All Movies](https://www.plexopedia.com/plex-media-server/api/library/movies/), [python-plexapi media model](https://python-plexapi.readthedocs.io/en/latest/modules/media.html).

## TMDB

| Use | Request |
|---|---|
| Runtime by TMDB ID | `GET /3/movie/{movie_id}` → `runtime` (minutes), `title`, `release_date`, `imdb_id` |
| TMDB ID from IMDb ID | `GET /3/find/{imdb_id}?external_source=imdb_id` → `movie_results` |
| Optional title search | `GET /3/search/movie?query=&year=` |

- Authentication: an API Read Access Token is sent as `Authorization: Bearer`; a v3 API key is sent as the `api_key` query parameter (HTTP library request logging is suppressed and all logs are redacted).
- HTTP 429 honors `Retry-After` (capped at 10 minutes); other errors back off for 5 minutes.
- Results are cached per TMDB ID for the configured number of days.
- Attribution: the About page shows TMDB's logo and the required notice, "This product uses the TMDB API but is not endorsed or certified by TMDB." The bundled logo is TMDB's unmodified short blue logo from themoviedb.org.

References: [TMDB API FAQ and attribution requirements](https://developer.themoviedb.org/docs/faq), [Find by ID](https://developer.themoviedb.org/reference/find-by-id).

## Notifications

- **ntfy**: `POST` the message body to the topic URL with `Title`, `Priority` (`high` for high-priority issues), and `Tags` headers, plus `Authorization: Bearer` when a token is set.
- **Generic webhook**: `POST` JSON `{"source", "title", "body", "priority", "sent_at"}` with an optional bearer token.

## Tdarr

Tdarr remains responsible for full-file decode checks ([Tdarr health check documentation](https://docs.tdarr.io/docs/library-setup/healthcheck/)). The only Tdarr endpoint confirmed in official documentation during development is the server status endpoint, `/api/v2/status` ([Tdarr troubleshooting](https://docs.tdarr.io/docs/troubleshooting/)). A supported, documented interface for per-file health-check results, with distinct success, failure, cancelled, and configuration-error meanings, was not verified. The integration is therefore **deferred**: the decode-health dimension is reported as `not_integrated`, and RipAudit does not read Tdarr's internal database. `src/ripaudit/integrations/tdarr.py` is the adapter boundary for a future implementation.

## Verification status

| Integration | Automated tests | Live verification |
|---|---|---|
| Plex | Mocked responses in the documented shape | Not yet tested against a real Plex server |
| TMDB | Mocked responses in the documented shape | Not yet tested against the real API |
| ntfy / webhook | Mocked endpoint | Not yet tested against a real ntfy server |
| FFprobe | Real FFprobe on synthetic MKV files | Not yet tested on full-size UHD remuxes |
| Tdarr | — | Deferred |

# Installation and configuration (any Docker host)

## Requirements

- Linux Docker host (amd64; arm64 builds are configured in the release workflow but not yet tested on hardware).
- Plex Media Server reachable from the container.
- A TMDB API Read Access Token.
- Media readable by the container user (default UID 99, GID 100).

## Build and run

```bash
git clone https://github.com/justuspost/RipAudit.git && cd RipAudit
docker build -t ripaudit:local --build-arg REVISION="$(git rev-parse HEAD)" .
mkdir -p appdata && sudo chown 99:100 appdata
docker run -d --name ripaudit --user 99:100 --read-only --tmpfs /tmp --cap-drop ALL \
  --security-opt no-new-privileges:true -p 8080:8080 \
  -v "$PWD/appdata:/config" -v /srv/media/Movies:/media/Movies:ro ripaudit:local
```

Use `--user <uid>:<gid>` to match whatever owns your appdata and can read your media. See `docker-compose.example.yml` for a Compose equivalent.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `RIPAUDIT_CONFIG_DIR` | `/config` | Appdata location |
| `RIPAUDIT_PORT` | `8080` | WebUI port inside the container |
| `RIPAUDIT_MEDIA_ROOTS` | `/media` | Initial comma-separated media roots (editable in Settings) |
| `RIPAUDIT_PLEX_TOKEN` | — | Plex token; overrides the WebUI value and is never written to disk |
| `RIPAUDIT_TMDB_CREDENTIAL` | — | TMDB Read Access Token or API key; same behavior |
| `RIPAUDIT_NOTIFY_TOKEN` | — | Bearer token for ntfy/webhook; same behavior |
| `RIPAUDIT_LOG_LEVEL` | `INFO` | Log level |
| `TZ` | UTC | Time zone for timestamps and the daily digest |

`RIPAUDIT_TMDB_BASE_URL` and `RIPAUDIT_FFPROBE` exist for development and testing only.

## Settings reference

| Setting | Default | Notes |
|---|---|---|
| Media roots | `/media` | Container paths. Mount them read-only. |
| File extensions | `.mkv, .mp4` | Case-insensitive. |
| Ignore patterns | `*.part`, `*.partial`, `*.tmp`, `*/.*`, `*/@eaDir/*`, `*/#recycle/*` | Shell-style, matched against the path under each root and against file names. |
| Scan interval | 15 min | Discovery reads only directory entries and file metadata. |
| Unchanged scans required | 1 | Consecutive scans with the same size and modification time before inspection. 0 = inspect on first sight (use only with a staging-folder workflow). |
| Minimum file age | 120 s | Time since last modification before inspection. |
| Concurrent inspections | 1 | Maximum simultaneous FFprobe processes (1–4). |
| FFprobe timeout | 120 s | Per file. |
| Plex sync interval | 30 min | New files awaiting Plex trigger an extra sync at most every 5 minutes. |
| Wait for Plex | 48 h | After this, unmatched files show as Unverified (they are still retried). |
| TMDB cache | 30 days | Cached runtimes are refreshed after this. |
| Thresholds | 120 s / 3 % review; 300 s / 10 % high; 120 s / 3 % longer | See [How it works](how-it-works.md). |

Saving settings recalculates every result immediately without re-inspecting files.

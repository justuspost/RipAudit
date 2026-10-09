# Privacy

RipAudit runs entirely on your own server. It has no telemetry, analytics, or update checks, and its maintainers receive no data from your installation.

## What leaves your server

| Destination | When | What is sent |
|---|---|---|
| Your Plex server | Plex sync and connection tests | Your Plex token (in a request header) and library requests |
| TMDB (`api.themoviedb.org`) | Metadata lookups | Your TMDB credential, TMDB or IMDb IDs, and, only if you enable title search, movie titles and years. TMDB's own privacy policy applies. |
| Your ntfy server or webhook | Only if configured | Notification title and text: file name (or full path if you enable it) and the reason it was flagged |

File names and paths are never sent to TMDB. Media contents are never uploaded anywhere.

## What is stored locally

In the appdata folder: file paths, sizes, modification times, FFprobe results, Plex and TMDB metadata, your overrides and approval notes, an audit history of actions, your administrator account (with a hashed password), and any secrets you entered in the WebUI. Delete the appdata folder to remove everything.

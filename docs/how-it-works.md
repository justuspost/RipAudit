# How RipAudit works

```
Discovery ──► Stability check ──► FFprobe ──► Plex identity ──► TMDB runtime ──► Classification ──► Review / notify
 (stat only)   (size + mtime)     (duration,   (path mapping,    (cached by        (thresholds,
                                   tracks)      versions, parts)  TMDB ID)          approvals)
```

## File identity

Each file is tracked by its normalized container path. Its **signature** is `size:mtime_ns`. When the signature changes, the file is treated as a new version: it is re-inspected, and approvals and source-runtime overrides recorded for the previous version stop applying (an approval can be marked to carry forward). A renamed or moved file is a new path and never inherits another file's approval.

Discovery never reads file contents. Routine scans therefore cost the same for a 90 GB UHD remux as for a small file.

## Readiness

A file is inspected only after its signature is unchanged for the configured number of consecutive scans and its modification time is older than the minimum age. This is a heuristic: a paused rip looks stable. The reliable workflow is to rip into an unwatched staging folder and move finished files into the library.

## Unavailable shares

A media root that is missing, unreadable, or suddenly empty while files were previously known is reported as **unavailable**, and nothing is marked missing. If more than half of a root's known files disappear in one scan, missing-marking is held until the next scan sees the same set missing.

## Measured duration

RipAudit runs `ffprobe -show_format -show_streams` with an argument list (never a shell) and a `file:` prefix so file names cannot be interpreted as options. The comparison duration is:

1. The container duration, when present.
2. Otherwise the longest video-stream duration (including the Matroska `DURATION` tag).
3. Otherwise the result is a **probe error** (invalid duration).

If the container and video-stream durations differ by more than 5 seconds, a warning is shown. Track counts and languages are displayed for reference only; RipAudit cannot know which tracks were on the disc.

If the file changes during inspection, the result is discarded and the file is retried. Failures and timeouts are retried up to three times with increasing delays; one bad file never blocks the queue.

## Identity

Plex is the source of identity. RipAudit reads each movie library with external IDs and maps every `Media` (version) and `Part` (file) to a container path using the longest matching path mapping. For multipart versions, the durations of all parts are summed, and the movie is not compared until every part has been inspected. Each version and edition is compared separately.

Expected-runtime sources, in order:

1. Your source runtime for this file version (for example, the DVDFab title length).
2. TMDB runtime for the TMDB ID Plex supplied.
3. TMDB runtime found from the IMDb ID Plex supplied (only when exactly one TMDB movie matches).
4. Optional title/year search, which only lists candidates for you to select.

Plex's own duration is never used as the reference, because it is measured from the same file.

## Thresholds

For expected runtime E and measured runtime A, the shortfall is E − A.

| Rule | Threshold | Default |
|---|---|---|
| Possible incomplete (review) | shortfall > min(review seconds, review % × E) | min(120 s, 3 %) |
| Possible incomplete (high) | shortfall > min(high seconds, high % × E) | min(300 s, 10 %) |
| Edition review (longer) | A − E > min(longer seconds, longer % × E) | min(120 s, 3 %) |

Taking the smaller value means a fixed two-minute tolerance cannot hide a large proportional loss in a short film. Because TMDB runtimes are whole minutes, no threshold falls below 60 seconds when the reference comes from TMDB.

When Plex reports an edition (for example "Extended") and the reference is TMDB, a moderate shortfall is shown as **Edition review** instead of **Possible incomplete**; a high-priority shortfall stays high priority.

## Result dimensions

Every result records four independent states: file/probe state, identity state, runtime comparison state, and decode-health state. Decode health is `not_integrated` in this release (see [Integrations](integrations.md#tdarr)).

## Notifications

New issues are queued with a unique key built from the file, its version, the outcome, and the severity, so the same issue is announced once. Delivery is retried with exponential backoff (1, 2, 4 … minutes, capped at an hour) for up to six attempts.

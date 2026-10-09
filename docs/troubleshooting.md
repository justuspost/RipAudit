# Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| WebUI asks for a setup token | No account exists yet | Read `appdata/setup-token` (Unraid: `cat /mnt/user/appdata/ripaudit/setup-token`). |
| Forgot the password | Single local account | Stop the container, back up appdata, then run `sqlite3 ripaudit.db "DELETE FROM users;"` and start it again; a new setup token is generated. Results and approvals are kept. |
| Container exits with a permission error on `/config` | Appdata not writable by UID 99 / GID 100 | `chown -R 99:100 /mnt/user/appdata/ripaudit`, or run with `--user` matching the folder owner. |
| Root shows **unavailable** | Share missing, unreadable, or empty | Confirm the path mapping in the container settings and that the array is started. Files are not marked missing while a root is unavailable. |
| Files stay **Pending** — "Waiting for the file to be stable" | Stability settings | Wait for the next scans, or lower **Unchanged scans required** if you use a staging-folder workflow. |
| Files stay **Pending** — "Waiting for Plex" | Plex has not scanned the file, or the path mapping does not match | Check the dashboard Plex status for "no matching path mapping", then compare paths as described in the Unraid guide. After the waiting period the file shows as Unverified and is still retried. |
| Many files **Unverified** — "No external movie identifier" | Plex item has no TMDB/IMDb IDs (unmatched, or a "none" agent) | Fix the match in Plex, or enter a TMDB ID or source runtime on the file page. |
| **Probe error** | Unreadable or damaged file, or timeout | Open the file page for FFprobe diagnostics. Raise the timeout for very slow storage. Run Tdarr's thorough health check. |
| **Possible incomplete rip** on a correct file | Different cut than TMDB's runtime, or TMDB data is inaccurate | Enter the disc title runtime as the source runtime, or approve the exception with a reason. |
| TMDB status shows a rate limit | Too many requests | RipAudit backs off automatically and resumes. |
| Test notification fails | Wrong URL or token | Check the URL and token. ntfy topics are URLs such as `https://ntfy.sh/your-topic`. |
| Behind HTTPS, sign-in loops | Secure-cookie setting mismatch | Enable **Secure cookies only** only when the browser reaches RipAudit over HTTPS, then restart. |

Use **Settings → Download sanitized diagnostics** when asking for help; it excludes secrets. Review it before sharing, because it contains your media root paths.

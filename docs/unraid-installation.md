# Installing RipAudit on Unraid

> **Development artifact.** The template at `unraid/ripaudit.xml` points at `ghcr.io/justuspost/ripaudit:latest`, which has **not been published yet**, and RipAudit is **not listed** in Community Applications. Until a release exists, build the image yourself (below) or wait for a published release.

## 1. Get the image

Until a release is published, build on any machine with Docker and push to a registry you control, or build directly on the Unraid server from a terminal:

```bash
cd /mnt/user/appdata && git clone https://github.com/justuspost/RipAudit.git ripaudit-src
cd ripaudit-src && docker build -t ripaudit:local .
```

## 2. Add the container

1. In **Docker → Add Container**, choose **Template: none**, or copy `unraid/ripaudit.xml` to `/boot/config/plugins/dockerMan/templates-user/my-RipAudit.xml` and select it.
2. Set **Repository** to `ripaudit:local` (or the published image once it exists).
3. Keep **Extra Parameters** as `--user 99:100 --security-opt no-new-privileges:true`. RipAudit runs as `nobody:users`, the Unraid appdata default, and never needs privileged mode, GPU access, or the Docker socket.
4. Paths:
   - **Appdata** → `/config` (read/write): `/mnt/user/appdata/ripaudit`
   - **Movies** → `/media/Movies` with access mode **Read Only**: your movie share, for example `/mnt/user/Movies`. Add more read-only paths for other libraries (for example a 4K share at `/media/Movies4K`).
5. **WebUI Port**: 8080 by default.
6. Apply.

## 3. First run

1. Open the WebUI. You will be asked for a **setup token**. Find it in `/mnt/user/appdata/ripaudit/setup-token` (for example `cat /mnt/user/appdata/ripaudit/setup-token` from the Unraid terminal). It is deleted after the account is created.
2. Create an administrator account with a password of at least 12 characters.

## 4. Connect Plex

1. **Plex server URL**: for example `http://192.168.1.10:32400`.
2. **Plex token**: follow Plex's support article *Finding an authentication token / X-Plex-Token*. Treat it like a password. Alternatively set `RIPAUDIT_PLEX_TOKEN` in the template (masked), which keeps it out of appdata.
3. Choose **Test Plex connection** to list your movie libraries, and optionally restrict to specific library keys.
4. **Path mappings** translate the path Plex sees to the path RipAudit sees. In the Plex container, a movie may be at `/data/Movies/Heat (1995)/Heat.mkv`; in RipAudit the same file is at `/media/Movies/Heat (1995)/Heat.mkv`. The mapping is:

   ```
   /data/Movies => /media/Movies
   ```

   If the dashboard says Plex paths had no matching path mapping, compare a file path on a RipAudit file page with the path shown in Plex (**Get Info → View XML**, `file=` attribute).

## 5. Connect TMDB

Create a free TMDB account, open **Settings → API**, and copy the **API Read Access Token** (preferred) or the v3 API key. Paste it in RipAudit's TMDB settings or set `RIPAUDIT_TMDB_CREDENTIAL`. Review TMDB's API terms; personal non-commercial use is free with attribution, which RipAudit displays on its About page.

## 6. Recommended ripping workflow

Have DVDFab write into a **staging share that RipAudit does not watch**, then move finished files into the movie share. Moving within the same Unraid share is instant. If you must rip directly into a watched folder, raise **Unchanged scans required** and **Minimum file age**; remember a paused rip can look finished.

For best accuracy, note the runtime of the title you selected in DVDFab and enter it on the file's page as the **source runtime**.

## 7. Notifications (optional)

Choose **ntfy** and enter a topic URL such as `https://ntfy.sh/<long-random-topic>` (or your own ntfy server), or **Generic JSON webhook**. Use **Send test notification** to confirm delivery. By default notifications include only the file name, not the full path.

## Backup and restore

Everything RipAudit stores lives in the appdata folder:

| File | Purpose |
|---|---|
| `ripaudit.db` (+ `-wal`, `-shm`) | Results, approvals, overrides, history |
| `settings.json` | Settings, including any secrets entered in the WebUI (mode 0600) |
| `secret.key` | Session-signing key |

To back up, stop the container and copy the folder (the Unraid **Appdata Backup** plugin works). To restore, stop the container, replace the folder, and start it. Copying the database while the container is running can capture an inconsistent snapshot; if you must, use `sqlite3 ripaudit.db ".backup backup.db"`.

## Upgrading

Pull the new image and restart. Database migrations run automatically at startup; back up appdata first.

## Uninstalling

Remove the container and delete `/mnt/user/appdata/ripaudit`. RipAudit never writes to your media shares.

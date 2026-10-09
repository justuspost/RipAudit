# Release checklist

Each item that publishes or announces something requires the maintainer's explicit approval.

## Before the first public release

- [x] Choose and add a `LICENSE` (MIT; see [license-options.md](license-options.md)).
- [ ] Validate against a real Plex server, the real TMDB API, and a real ntfy server; record results in `docs/integrations.md`.
- [ ] Run on full-size UHD remuxes and confirm FFprobe time and memory stay modest.
- [ ] Install from the template on an Unraid test system; confirm permissions, the read-only media mount, first-run setup, and restart persistence.
- [ ] Re-check TMDB attribution terms and logo against TMDB's current logos and attribution page.
- [ ] Re-check the current Community Applications requirements at ca.unraid.net/submit/help.
- [ ] Review the naming and trademark search for "RipAudit".
- [ ] Configure the GitHub `release` environment with required reviewers.

## For each release

- [ ] CI is green on `main` (lint, tests, dependency audit, container build, smoke test, image scan).
- [ ] Update `CHANGELOG.md` and the version in `pyproject.toml` and `src/ripaudit/__init__.py`.
- [ ] **Approval:** tag `vX.Y.Z` (or `vX.Y.Z-rc.N` for a pre-release, which never receives `latest`).
- [ ] **Approval:** approve the `release` environment deployment, which publishes `ghcr.io/justuspost/ripaudit` and creates the GitHub release with the source commit.
- [ ] Make the GHCR package public and confirm `docker pull` works anonymously.
- [ ] Remove the development notices from `unraid/ripaudit.xml`, `ca_profile.xml`, `README.md`, and `docs/unraid-installation.md`, and set `<Beta>` appropriately.

## Community Applications submission

- [ ] Repository public, active, licensed, with `ca_profile.xml` at the root and a valid template.
- [ ] Support thread created on the Unraid forums, and `<Support>` updated.
- [ ] Run **Validate** and **Scan** at ca.unraid.net/submit.
- [ ] **Approval:** submit. Do not describe RipAudit as listed until the submission is accepted and visible.

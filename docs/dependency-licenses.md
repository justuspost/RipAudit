# Dependency and license review

Reviewed for 0.1.0.dev0 from installed package metadata.

## Python runtime dependencies

| Package | Version | License |
|---|---|---|
| fastapi | 0.143.0 | MIT |
| starlette | 1.7.0 | BSD-3-Clause |
| pydantic / pydantic-core | 2.14.0 / 2.50.0 | MIT |
| uvicorn | 0.54.0 | BSD-3-Clause |
| jinja2 | 3.1.6 | BSD-3-Clause |
| markupsafe | 3.0.4 | BSD-3-Clause |
| httpx / httpcore | 0.28.1 / 1.0.9 | BSD-3-Clause |
| itsdangerous | 2.2.0 | BSD-3-Clause |
| python-multipart | 0.0.32 | Apache-2.0 |
| anyio | 4.15.1 | MIT |
| h11 | 0.16.0 | MIT |
| idna | 3.20 | BSD-3-Clause |
| certifi | 2026.7.22 | MPL-2.0 |
| click | 8.5.0 | BSD-3-Clause |
| typing-extensions | 4.16.0 | PSF-2.0 |
| annotated-types, typing-inspection | 0.8.0, 0.4.4 | MIT |

Transitive versions are resolved at build time; CI runs `pip-audit`.

## Container image components

| Component | Source | License notes |
|---|---|---|
| Python 3.13 | `python:3.13-slim-trixie` | PSF-2.0 |
| FFmpeg / FFprobe | Debian `ffmpeg` package | Debian's build enables GPL components, so the binaries are distributed under GPL. Publishing the image redistributes them; Debian's source packages satisfy source availability, and release notes should point to them. |
| tini | Debian `tini` package | MIT |
| Debian base packages | Debian trixie | Various free licenses |

## Assets

| Asset | License |
|---|---|
| RipAudit icon (`docs/assets/ripaudit-icon.png`, `static/icon.svg`) | Original artwork created for this project; MIT, as part of this project |
| TMDB logo (`static/tmdb-logo.svg`) | TMDB trademark, used unmodified under TMDB's attribution requirements; not covered by the project license |

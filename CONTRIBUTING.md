# Contributing to RipAudit

Thanks for helping. RipAudit's promise is narrow on purpose: it flags files for review and never claims a rip is complete. Contributions should preserve that.

## Ground rules

- Never add features that modify, delete, rename, transcode, download, or redistribute media, or that circumvent copy protection.
- Never label a result as "verified" or "complete".
- Integrations must use documented interfaces. If an interface cannot be verified, isolate it behind an adapter, document the uncertainty, and test with mocks.
- No real media, real credentials, or personal paths in tests, fixtures, screenshots, or issues.

## Development setup

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
ruff check src tests
pytest -q
```

FFmpeg is required for tests that run real FFprobe against tiny synthetic files.

### Local demo

```bash
python scripts/dev_mock_services.py --port 9999 --fixture scripts/demo_fixture.json &
RIPAUDIT_CONFIG_DIR=./config RIPAUDIT_MEDIA_ROOTS=$PWD/demo-media \
RIPAUDIT_TMDB_BASE_URL=http://127.0.0.1:9999 python -m ripaudit
```

Use Plex URL `http://127.0.0.1:9999`, token `dev-token`, TMDB credential `dev-key`, and a mapping such as `/data/Movies => <your demo media root>`. Create synthetic files with FFmpeg's `testsrc` source. The mock is for demos only and does not replace testing against real services.

## Pull requests

- One logical change per pull request, with tests.
- Database changes need a new numbered file in `src/ripaudit/migrations/`; never edit an applied migration.
- Update documentation and `CHANGELOG.md` for user-visible changes.

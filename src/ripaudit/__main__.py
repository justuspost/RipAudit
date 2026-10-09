"""Entry point: ``python -m ripaudit``."""

from __future__ import annotations

import logging
import os

import uvicorn


def main() -> None:
    logging.basicConfig(level=os.environ.get("RIPAUDIT_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from .web import create_app

    app = create_app()
    uvicorn.run(app, host=os.environ.get("RIPAUDIT_HOST", "0.0.0.0"),  # noqa: S104 - container listens on all
                port=int(os.environ.get("RIPAUDIT_PORT", "8080")), proxy_headers=False,
                timeout_graceful_shutdown=30, access_log=False, log_config=None)


if __name__ == "__main__":
    main()

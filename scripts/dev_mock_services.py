"""Development-only mock of the Plex and TMDB endpoints RipAudit reads.

Used for local demos, screenshots, and manual testing with synthetic media.
It is NOT a substitute for testing against a real Plex server and the real TMDB API.

    python scripts/dev_mock_services.py --port 9999 --fixture scripts/demo_fixture.json

Then point RipAudit at http://127.0.0.1:9999 (Plex URL, token "dev-token") and set
RIPAUDIT_TMDB_BASE_URL=http://127.0.0.1:9999 with TMDB credential "dev-key".
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


def make_handler(fixture: dict):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: dict | None = None):
            data = json.dumps(body or {}).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

        def do_GET(self):  # noqa: N802
            url = urlsplit(self.path)
            q = parse_qs(url.query)
            if url.path.startswith("/library"):
                if self.headers.get("X-Plex-Token") != "dev-token":
                    return self._send(401)
                if url.path == "/library/sections":
                    return self._send(200, {"MediaContainer": {"Directory": [
                        {"key": "1", "title": "Movies", "type": "movie"}]}})
                if url.path == "/library/sections/1/all":
                    return self._send(200, {"MediaContainer": {"totalSize": len(fixture["plex"]),
                                                               "Metadata": fixture["plex"]}})
                return self._send(404)
            if url.path.startswith("/3/"):
                if q.get("api_key", [""])[0] != "dev-key":
                    return self._send(401)
                parts = url.path.strip("/").split("/")
                if parts[1] == "movie":
                    m = fixture["tmdb"].get(parts[2])
                    return self._send(200, m) if m else self._send(404)
                if parts[1] == "find":
                    return self._send(200, {"movie_results": []})
                if parts[1] == "search":
                    return self._send(200, {"results": []})
            return self._send(404)

    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9999)
    ap.add_argument("--fixture", required=True)
    args = ap.parse_args()
    with open(args.fixture, encoding="utf-8") as fh:
        fixture = json.load(fh)
    ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(fixture)).serve_forever()


if __name__ == "__main__":
    main()

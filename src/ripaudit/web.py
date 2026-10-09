"""FastAPI web application: authentication, dashboard, actions, settings, exports."""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import __version__, classify
from .config import Settings, load_or_create_secret
from .db import Database
from .engine import Engine, Worker
from .integrations import plex, tdarr
from .security import (
    csv_safe,
    hash_password,
    new_csrf_token,
    password_problems,
    redact,
    tokens_match,
    validate_service_url,
    verify_password,
)

log = logging.getLogger("ripaudit")
HERE = Path(__file__).parent
LOGIN_WINDOW, LOGIN_MAX_FAILURES = 900, 5
SORTABLE = {"path": "f.path", "outcome": "r.outcome", "severity": "r.severity", "difference": "r.difference_seconds",
            "actual": "r.actual_seconds", "expected": "r.expected_seconds", "seen": "f.first_seen",
            "title": "i.plex_title"}


class RedactingFilter(logging.Filter):
    def __init__(self, settings: Settings):
        super().__init__()
        self.settings = settings

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage(), self.settings.secret_values())
        record.args = ()
        return True


def install_log_redaction(settings: Settings) -> None:
    """Redact secrets from every log record, including third-party libraries.

    httpx logs full request URLs at INFO, which would include a TMDB ``api_key``
    query parameter, so it is limited to WARNING and still passes through the filter.
    """
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    flt = RedactingFilter(settings)
    logging.getLogger("ripaudit").addFilter(flt)
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(flt)


def fmt_duration(seconds) -> str:
    if seconds is None:
        return "—"
    s = int(round(abs(float(seconds))))
    sign = "-" if float(seconds) < 0 else ""
    return f"{sign}{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def fmt_diff(seconds) -> str:
    if seconds is None:
        return "—"
    return ("+" if float(seconds) >= 0 else "-") + fmt_duration(abs(float(seconds)))


def fmt_time(ts) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "—"


def create_app(config_dir: str | Path | None = None, engine_kwargs: dict | None = None,
               start_worker: bool = True) -> FastAPI:
    config_dir = Path(config_dir or os.environ.get("RIPAUDIT_CONFIG_DIR", "/config"))
    config_dir.mkdir(parents=True, exist_ok=True)
    settings = Settings(config_dir)
    db = Database(config_dir / "ripaudit.db")
    engine = Engine(db, settings, **(engine_kwargs or {}))
    session_secret = load_or_create_secret(config_dir, "secret.key")
    install_log_redaction(settings)

    def setup_token() -> str | None:
        if db.scalar("SELECT COUNT(*) FROM users"):
            return None
        return load_or_create_secret(config_dir, "setup-token", 18)

    worker = Worker(engine)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        token = setup_token()
        if token:
            log.warning("First-run setup: open the WebUI and enter the setup token stored in %s",
                        config_dir / "setup-token")
        if start_worker:
            worker.start()
        yield
        if start_worker:
            worker.stop()
        db.close()

    app = FastAPI(title="RipAudit", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.add_middleware(SessionMiddleware, secret_key=session_secret, session_cookie="ripaudit_session",
                       max_age=12 * 3600, same_site="strict",
                       https_only=bool(settings.get().get("session_cookie_secure")))
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals.update(OUTCOMES=classify.OUTCOMES, fmt_duration=fmt_duration, fmt_diff=fmt_diff,
                                 fmt_time=fmt_time, version=__version__, basename=os.path.basename)
    app.state.engine, app.state.db, app.state.settings = engine, db, settings
    failures: dict[str, list[float]] = defaultdict(list)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Content-Security-Policy",
                                    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self';"
                                    " frame-ancestors 'none'; form-action 'self'")
        return response

    # ----- helpers ------------------------------------------------------
    def csrf(request: Request) -> str:
        tok = request.session.get("csrf")
        if not tok:
            tok = new_csrf_token()
            request.session["csrf"] = tok
        return tok

    def render(request: Request, name: str, ctx: dict | None = None, status: int = 200) -> HTMLResponse:
        ctx = dict(ctx or {})
        ctx.update(request=request, csrf_token=csrf(request), user=request.session.get("user"),
                   flash=request.session.pop("flash", None))
        return templates.TemplateResponse(request, name, ctx, status_code=status)

    class LoginRequired(Exception):
        pass

    @app.exception_handler(LoginRequired)
    async def _login_redirect(request: Request, _exc):
        target = "/setup" if setup_token() else "/login"
        return RedirectResponse(target, status_code=303)

    def require_user(request: Request) -> str:
        user = request.session.get("user")
        if not user or not db.scalar("SELECT 1 FROM users WHERE username = ?", (user,)):
            raise LoginRequired()
        return user

    async def check_csrf(request: Request) -> None:
        form = await request.form()
        if not tokens_match(form.get("csrf_token"), request.session.get("csrf")):
            raise HTTPException(status_code=403, detail="Invalid or missing CSRF token.")

    def flash(request: Request, message: str, kind: str = "info") -> None:
        request.session["flash"] = {"message": message, "kind": kind}

    def back(path: str) -> RedirectResponse:
        return RedirectResponse(path, status_code=303)

    def file_or_404(file_id: int):
        row = db.one("SELECT * FROM files WHERE id = ?", (file_id,))
        if row is None:
            raise HTTPException(status_code=404)
        return row

    # ----- health -------------------------------------------------------
    @app.get("/healthz")
    def healthz():
        try:
            db.scalar("SELECT 1")
        except Exception:  # noqa: BLE001
            return JSONResponse({"status": "error"}, status_code=503)
        alive = worker.is_alive() if start_worker else True
        return JSONResponse({"status": "ok" if alive else "degraded"}, status_code=200 if alive else 503)

    # ----- setup & auth -------------------------------------------------
    @app.get("/setup", response_class=HTMLResponse)
    def setup_form(request: Request):
        if not setup_token():
            return back("/login")
        return render(request, "setup.html", {"token_path": str(config_dir / "setup-token")})

    @app.post("/setup", dependencies=[Depends(check_csrf)])
    def setup_submit(request: Request, setup_token_value: str = Form(..., alias="setup_token"),
                     username: str = Form(...), password: str = Form(...), confirm: str = Form(...)):
        token = setup_token()
        if not token:
            return back("/login")
        errors = []
        if not tokens_match(setup_token_value.strip(), token):
            errors.append("Setup token is incorrect.")
        if not username.strip() or len(username) > 64:
            errors.append("Enter a username (up to 64 characters).")
        if password != confirm:
            errors.append("Passwords do not match.")
        errors += password_problems(password)
        if errors:
            return render(request, "setup.html", {"errors": errors, "token_path": str(config_dir / "setup-token")},
                          400)
        with db.tx() as conn:
            conn.execute("INSERT INTO users(username, password_hash, created_at) VALUES (?, ?, ?)",
                         (username.strip(), hash_password(password), time.time()))
            db.event(conn, None, "user_created", {"username": username.strip()}, username.strip())
        try:
            (config_dir / "setup-token").unlink()
        except FileNotFoundError:
            pass
        request.session.clear()
        request.session["user"] = username.strip()
        flash(request, "Account created. Configure your media roots, Plex, and TMDB in Settings.")
        return back("/settings")

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        if setup_token():
            return back("/setup")
        return render(request, "login.html")

    @app.post("/login", dependencies=[Depends(check_csrf)])
    def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
        ip = request.client.host if request.client else "unknown"
        now = time.time()
        failures[ip] = [t for t in failures[ip] if now - t < LOGIN_WINDOW]
        if len(failures[ip]) >= LOGIN_MAX_FAILURES:
            return render(request, "login.html", {"errors": ["Too many failed attempts. Try again later."]}, 429)
        row = db.one("SELECT * FROM users WHERE username = ?", (username.strip(),))
        if row is None or not verify_password(password, row["password_hash"]):
            failures[ip].append(now)
            return render(request, "login.html", {"errors": ["Incorrect username or password."]}, 401)
        failures.pop(ip, None)
        request.session.clear()
        request.session["user"] = row["username"]
        return back("/")

    @app.post("/logout", dependencies=[Depends(check_csrf)])
    def logout(request: Request):
        request.session.clear()
        return back("/login")

    # ----- dashboard ----------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, user: str = Depends(require_user)):
        counts = {r["outcome"]: r["n"] for r in db.query(
            "SELECT r.outcome, COUNT(*) n FROM results r JOIN files f ON f.id=r.file_id GROUP BY r.outcome")}
        queue = db.query(
            "SELECT f.id, f.path, f.first_seen, r.outcome, r.severity, r.actual_seconds, r.expected_seconds,"
            " r.difference_seconds, r.reasons, i.plex_title, i.plex_year, i.plex_edition FROM files f"
            " JOIN results r ON r.file_id=f.id LEFT JOIN identities i ON i.file_id=f.id"
            " WHERE f.state='present' AND r.outcome IN ('possible_incomplete','edition_review','unverified',"
            "'probe_error') ORDER BY CASE r.severity WHEN 'high' THEN 0 WHEN 'review' THEN 1 ELSE 2 END,"
            " CASE r.outcome WHEN 'possible_incomplete' THEN 0 WHEN 'probe_error' THEN 1 WHEN 'edition_review'"
            " THEN 2 ELSE 3 END, r.difference_seconds LIMIT 200")
        scans = {k: db.one("SELECT * FROM scans WHERE kind=? ORDER BY id DESC LIMIT 1", (k,))
                 for k in ("discovery", "plex_sync")}
        last_ok = {k: db.scalar("SELECT MAX(finished_at) FROM scans WHERE kind=? AND status IN ('ok','partial')",
                                (k,)) for k in ("discovery", "plex_sync")}
        roots = []
        if scans["discovery"] and scans["discovery"]["summary"]:
            roots = json.loads(scans["discovery"]["summary"]).get("roots", [])
        statuses = {k: engine.get_status(k) for k in ("plex", "tmdb", "notifications")}
        queued = db.scalar("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')")
        return render(request, "dashboard.html", {
            "counts": counts, "queue": [dict(q) | {"reasons": json.loads(q["reasons"])} for q in queue],
            "scans": scans, "last_ok": last_ok, "roots": roots, "statuses": statuses, "queued": queued,
            "tdarr": tdarr.EXPLANATION, "total": sum(counts.values()),
        })

    @app.post("/scan-now", dependencies=[Depends(check_csrf)])
    def scan_now(request: Request, user: str = Depends(require_user)):
        engine.scan_requested.set()
        engine.plex_requested.set()
        flash(request, "Scan requested. Results update as files are processed.")
        return back("/")

    # ----- inventory ----------------------------------------------------
    def _filtered(params: dict, limit: int | None = None, offset: int = 0):
        where, args = ["1=1"], []
        if params.get("q"):
            where.append("(f.path LIKE ? OR i.plex_title LIKE ?)")
            args += [f"%{params['q']}%", f"%{params['q']}%"]
        if params.get("outcome"):
            where.append("r.outcome = ?")
            args.append(params["outcome"])
        if params.get("severity"):
            where.append("r.severity = ?")
            args.append(params["severity"])
        if params.get("library"):
            where.append("i.plex_library = ?")
            args.append(params["library"])
        if params.get("source"):
            if params["source"] == "none":
                where.append("r.expected_source IS NULL")
            else:
                where.append("r.expected_source = ?")
                args.append(params["source"])
        if params.get("since"):
            try:
                ts = time.mktime(time.strptime(params["since"], "%Y-%m-%d"))
                where.append("f.first_seen >= ?")
                args.append(ts)
            except ValueError:
                pass
        if not params.get("include_missing"):
            where.append("f.state = 'present'")
        sort = SORTABLE.get(params.get("sort") or "path", "f.path")
        direction = "DESC" if params.get("dir") == "desc" else "ASC"
        sql = ("SELECT f.*, r.outcome, r.severity, r.actual_seconds, r.expected_seconds, r.difference_seconds,"
               " r.expected_source, i.plex_title, i.plex_year, i.plex_edition, i.plex_library, i.tmdb_id"
               " FROM files f LEFT JOIN results r ON r.file_id=f.id LEFT JOIN identities i ON i.file_id=f.id"
               f" WHERE {' AND '.join(where)} ORDER BY {sort} {direction}, f.id")
        total = db.scalar(f"SELECT COUNT(*) FROM ({sql})", tuple(args))
        if limit:
            sql += " LIMIT ? OFFSET ?"
            args += [limit, offset]
        return db.query(sql, tuple(args)), total

    @app.get("/files", response_class=HTMLResponse)
    def files(request: Request, user: str = Depends(require_user)):
        params = dict(request.query_params)
        page = max(1, int(params.get("page", "1") or 1)) if str(params.get("page", "1")).isdigit() else 1
        per = 100
        rows, total = _filtered(params, per, (page - 1) * per)
        libraries = [r[0] for r in db.query("SELECT DISTINCT plex_library FROM identities WHERE plex_library"
                                            " IS NOT NULL ORDER BY 1")]
        return render(request, "files.html", {"rows": rows, "total": total, "page": page, "per": per,
                                              "params": params, "libraries": libraries})

    @app.get("/files/{file_id}", response_class=HTMLResponse)
    def file_detail(request: Request, file_id: int, user: str = Depends(require_user)):
        f = file_or_404(file_id)
        result = db.one("SELECT * FROM results WHERE file_id=?", (file_id,))
        ident = db.one("SELECT * FROM identities WHERE file_id=?", (file_id,))
        probe_row = db.one("SELECT * FROM probes WHERE file_id=? ORDER BY probed_at DESC, id DESC LIMIT 1",
                           (file_id,))
        cache = db.one("SELECT * FROM tmdb_cache WHERE tmdb_id=?", (ident["tmdb_id"],)) \
            if ident is not None and ident["tmdb_id"] else None
        siblings = []
        if ident is not None and ident["plex_rating_key"]:
            siblings = db.query("SELECT f.id, f.path, i.part_index, i.part_count, i.plex_media_id, r.outcome"
                                " FROM identities i JOIN files f ON f.id=i.file_id LEFT JOIN results r"
                                " ON r.file_id=f.id WHERE i.plex_rating_key=? AND f.id != ?",
                                (ident["plex_rating_key"], file_id))
        return render(request, "file.html", {
            "f": f, "result": result, "reasons": json.loads(result["reasons"]) if result else [],
            "ident": ident, "probe": probe_row, "cache": cache, "siblings": siblings,
            "candidates": json.loads(ident["candidates"]) if ident is not None and ident["candidates"] else [],
            "guids": json.loads(ident["guids"]) if ident is not None and ident["guids"] else [],
            "warnings": json.loads(probe_row["warnings"]) if probe_row is not None and probe_row["warnings"] else [],
            "audio_langs": json.loads(probe_row["audio_languages"] or "[]") if probe_row is not None else [],
            "sub_langs": json.loads(probe_row["subtitle_languages"] or "[]") if probe_row is not None else [],
            "override": db.one("SELECT * FROM overrides WHERE file_id=? ORDER BY created_at DESC LIMIT 1",
                               (file_id,)),
            "approvals": db.query("SELECT * FROM approvals WHERE file_id=? ORDER BY created_at DESC", (file_id,)),
            "events": db.query("SELECT * FROM events WHERE file_id=? ORDER BY ts DESC, id DESC LIMIT 100",
                               (file_id,)),
            "tdarr": tdarr.EXPLANATION,
        })

    def _after_action(request: Request, file_id: int, message: str):
        engine.classify_all()
        flash(request, message)
        return back(f"/files/{file_id}")

    @app.post("/files/{file_id}/rescan", dependencies=[Depends(check_csrf)])
    def rescan(request: Request, file_id: int, user: str = Depends(require_user)):
        file_or_404(file_id)
        engine.action_rescan(file_id, user)
        return _after_action(request, file_id, "Rescan queued.")

    @app.post("/files/{file_id}/retry-metadata", dependencies=[Depends(check_csrf)])
    def retry_metadata(request: Request, file_id: int, user: str = Depends(require_user)):
        file_or_404(file_id)
        engine.action_retry_metadata(file_id, user)
        return _after_action(request, file_id, "Metadata lookup will be retried.")

    @app.post("/files/{file_id}/match", dependencies=[Depends(check_csrf)])
    def select_match(request: Request, file_id: int, tmdb_id: str = Form(...), user: str = Depends(require_user)):
        file_or_404(file_id)
        if not tmdb_id.strip().isdigit():
            flash(request, "Enter a numeric TMDB movie ID.", "error")
            return back(f"/files/{file_id}")
        engine.action_select_match(file_id, int(tmdb_id.strip()), user)
        return _after_action(request, file_id, "Match saved; the runtime will be fetched from TMDB.")

    @app.post("/files/{file_id}/override", dependencies=[Depends(check_csrf)])
    def set_override(request: Request, file_id: int, runtime: str = Form(...), note: str = Form(""),
                     user: str = Depends(require_user)):
        file_or_404(file_id)
        seconds = parse_runtime(runtime)
        if seconds is None or seconds <= 0 or seconds > 86400:
            flash(request, "Enter the runtime as H:MM:SS (for example 2:14:07) or as minutes.", "error")
            return back(f"/files/{file_id}")
        engine.action_set_override(file_id, seconds, note.strip()[:500], user)
        return _after_action(request, file_id, "Source runtime saved for this file version.")

    @app.post("/files/{file_id}/clear-override", dependencies=[Depends(check_csrf)])
    def clear_override(request: Request, file_id: int, user: str = Depends(require_user)):
        file_or_404(file_id)
        engine.action_clear_override(file_id, user)
        return _after_action(request, file_id, "Source runtime removed.")

    @app.post("/files/{file_id}/approve", dependencies=[Depends(check_csrf)])
    def approve(request: Request, file_id: int, reason: str = Form(""), preserve: str = Form(""),
                user: str = Depends(require_user)):
        file_or_404(file_id)
        try:
            engine.action_approve(file_id, reason[:1000], preserve == "on", user)
        except ValueError as exc:
            flash(request, str(exc), "error")
            return back(f"/files/{file_id}")
        return _after_action(request, file_id, "Exception approved for this file version.")

    @app.post("/files/{file_id}/reopen", dependencies=[Depends(check_csrf)])
    def reopen(request: Request, file_id: int, reason: str = Form(""), user: str = Depends(require_user)):
        file_or_404(file_id)
        engine.action_reopen(file_id, reason[:1000], user)
        return _after_action(request, file_id, "Approval revoked; the file is back in review.")

    # ----- exports ------------------------------------------------------
    EXPORT_FIELDS = ["id", "path", "state", "size", "outcome", "severity", "actual_seconds", "expected_seconds",
                     "difference_seconds", "expected_source", "plex_title", "plex_year", "plex_edition",
                     "plex_library", "tmdb_id", "first_seen"]

    @app.get("/export.csv")
    def export_csv(request: Request, user: str = Depends(require_user)):
        rows, _ = _filtered(dict(request.query_params))
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(EXPORT_FIELDS)
        for r in rows:
            w.writerow([csv_safe(r[k]) for k in EXPORT_FIELDS])
        return Response(buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": "attachment; filename=ripaudit-export.csv"})

    @app.get("/export.json")
    def export_json(request: Request, user: str = Depends(require_user)):
        rows, _ = _filtered(dict(request.query_params))
        return JSONResponse({"generated_at": time.time(), "version": __version__,
                             "note": "'no_runtime_issue' means within tolerance of the reference runtime; "
                                     "it does not certify a complete rip.",
                             "files": [{k: r[k] for k in EXPORT_FIELDS} for r in rows]},
                            headers={"Content-Disposition": "attachment; filename=ripaudit-export.json"})

    @app.get("/diagnostics.json")
    def diagnostics(request: Request, user: str = Depends(require_user)):
        secrets_list = settings.secret_values()
        data = {
            "version": __version__,
            "settings": settings.masked(),
            "secret_sources": {s: settings.secret_source(s, f)
                               for s, f in (("plex", "token"), ("tmdb", "credential"), ("notifications", "token"))},
            "statuses": {k: engine.get_status(k) for k in ("plex", "tmdb", "notifications")},
            "counts": {r["outcome"]: r["n"] for r in db.query("SELECT outcome, COUNT(*) n FROM results"
                                                              " GROUP BY outcome")},
            "recent_scans": [dict(s) for s in db.query("SELECT * FROM scans ORDER BY id DESC LIMIT 10")],
            "jobs": {r["status"]: r["n"] for r in db.query("SELECT status, COUNT(*) n FROM jobs GROUP BY status")},
            "notifications": {r["status"]: r["n"] for r in db.query("SELECT status, COUNT(*) n FROM notifications"
                                                                    " GROUP BY status")},
        }
        text = redact(json.dumps(data, indent=2, default=str), secrets_list)
        return PlainTextResponse(text, media_type="application/json",
                                 headers={"Content-Disposition": "attachment; filename=ripaudit-diagnostics.json"})

    # ----- settings -----------------------------------------------------
    @app.get("/settings", response_class=HTMLResponse)
    def settings_form(request: Request, user: str = Depends(require_user)):
        return render(request, "settings.html", _settings_ctx())

    def _settings_ctx(errors: list[str] | None = None) -> dict:
        m = settings.masked()
        return {"s": m, "errors": errors or [],
                "mappings_text": "\n".join(f"{x['plex']} => {x['container']}" for x in m["plex"]["path_mappings"]),
                "sources": {f"{a}.{b}": settings.secret_source(a, b)
                            for a, b in (("plex", "token"), ("tmdb", "credential"), ("notifications", "token"))},
                "plex_sections": app.state.__dict__.get("plex_sections")}

    @app.post("/settings", dependencies=[Depends(check_csrf)])
    async def settings_submit(request: Request, user: str = Depends(require_user)):
        form = await request.form()
        errors: list[str] = []

        def lines(name):
            return [ln.strip() for ln in str(form.get(name, "")).splitlines() if ln.strip()]

        def num(name, lo, hi, cast=int):
            try:
                v = cast(str(form.get(name, "")).strip())
            except ValueError:
                errors.append(f"{name.replace('_', ' ')} must be a number.")
                return lo
            if not lo <= v <= hi:
                errors.append(f"{name.replace('_', ' ')} must be between {lo} and {hi}.")
            return v

        roots = lines("media_roots")
        for r in roots:
            if not r.startswith("/") or ".." in r.split("/"):
                errors.append(f"Media root must be an absolute path without '..': {r}")
        mappings = []
        for ln in lines("path_mappings"):
            if "=>" not in ln:
                errors.append(f"Path mapping must look like '/plex/path => /container/path': {ln}")
                continue
            a, b = (x.strip() for x in ln.split("=>", 1))
            mappings.append({"plex": a, "container": b})
        errors += plex.validate_mappings(mappings, roots)
        exts = [e if e.startswith(".") else "." + e for e in
                (x.strip().lower() for x in str(form.get("extensions", "")).split(",")) if e]
        plex_url = str(form.get("plex_url", "")).strip()
        notify_url = str(form.get("notify_url", "")).strip()
        errors += [f"Plex URL: {p}" for p in validate_service_url(plex_url)]
        errors += [f"Notification URL: {p}" for p in validate_service_url(notify_url)]
        kind = str(form.get("notify_kind", "none"))
        if kind not in ("none", "ntfy", "webhook"):
            errors.append("Unknown notification type.")
        new = {
            "media_roots": roots, "extensions": exts or [".mkv", ".mp4"],
            "ignore_patterns": lines("ignore_patterns"),
            "scan_interval_minutes": num("scan_interval_minutes", 1, 1440),
            "stability_observations": num("stability_observations", 0, 10),
            "min_file_age_seconds": num("min_file_age_seconds", 0, 86400),
            "probe_concurrency": num("probe_concurrency", 1, 4),
            "probe_timeout_seconds": num("probe_timeout_seconds", 10, 1800),
            "plex": {"url": plex_url, "token": str(form.get("plex_token", "")),
                     "library_keys": [k.strip() for k in str(form.get("plex_library_keys", "")).split(",")
                                      if k.strip()],
                     "path_mappings": mappings,
                     "sync_interval_minutes": num("plex_sync_interval_minutes", 5, 1440),
                     "pending_grace_hours": num("pending_grace_hours", 1, 720),
                     "verify_tls": form.get("plex_verify_tls") == "on"},
            "tmdb": {"credential": str(form.get("tmdb_credential", "")),
                     "title_search_fallback": form.get("title_search_fallback") == "on",
                     "cache_days": num("tmdb_cache_days", 1, 365)},
            "thresholds": {
                "short_review_seconds": num("short_review_seconds", 0, 3600),
                "short_high_seconds": num("short_high_seconds", 0, 7200),
                "short_review_percent": num("short_review_percent", 0, 100, float),
                "short_high_percent": num("short_high_percent", 0, 100, float),
                "long_review_seconds": num("long_review_seconds", 0, 7200),
                "long_review_percent": num("long_review_percent", 0, 100, float)},
            "notifications": {"kind": kind, "url": notify_url, "token": str(form.get("notify_token", "")),
                              "include_full_paths": form.get("include_full_paths") == "on",
                              "notify_high": form.get("notify_high") == "on",
                              "notify_review": form.get("notify_review") == "on",
                              "daily_digest": form.get("daily_digest") == "on",
                              "digest_hour": num("digest_hour", 0, 23)},
            "session_cookie_secure": form.get("session_cookie_secure") == "on",
        }
        if not roots:
            errors.append("Configure at least one media root.")
        if errors:
            return render(request, "settings.html", _settings_ctx(errors), 400)
        settings.update(new)
        with db.tx() as conn:
            db.event(conn, None, "settings_updated", None, user)
        engine.classify_all()  # thresholds may have changed: recalculate without reprobing
        flash(request, "Settings saved. Results were recalculated with the current thresholds.")
        return back("/settings")

    @app.post("/settings/clear-secret", dependencies=[Depends(check_csrf)])
    def clear_secret(request: Request, field: str = Form(...), user: str = Depends(require_user)):
        allowed = {"plex.token": ("plex", "token"), "tmdb.credential": ("tmdb", "credential"),
                   "notifications.token": ("notifications", "token")}
        if field not in allowed:
            raise HTTPException(status_code=400)
        settings.clear_secret(*allowed[field])
        flash(request, "Stored secret removed.")
        return back("/settings")

    @app.post("/settings/test-plex", dependencies=[Depends(check_csrf)])
    def test_plex(request: Request, user: str = Depends(require_user)):
        pc = settings.get()["plex"]
        try:
            client = plex.PlexClient(pc["url"], pc["token"], verify_tls=pc.get("verify_tls", True),
                                     transport=engine.plex_transport)
            try:
                sections = client.sections()
            finally:
                client.close()
        except plex.PlexError as exc:
            msg = redact(str(exc), settings.secret_values())
            engine.set_status("plex", False, msg)
            flash(request, f"Plex connection failed: {msg}", "error")
            return back("/settings")
        app.state.plex_sections = [s for s in sections if s["type"] == "movie"]
        engine.set_status("plex", True, f"Connected; {len(app.state.plex_sections)} movie libraries found.")
        flash(request, "Plex connection succeeded. Movie libraries are listed below.")
        return back("/settings")

    @app.post("/settings/test-notification", dependencies=[Depends(check_csrf)])
    def test_notification(request: Request, user: str = Depends(require_user)):
        flash(request, engine.send_test_notification())
        return back("/settings")

    @app.get("/about", response_class=HTMLResponse)
    def about(request: Request, user: str = Depends(require_user)):
        return render(request, "about.html")

    return app


def parse_runtime(text: str) -> float | None:
    text = text.strip()
    try:
        if ":" in text:
            parts = [float(p) for p in text.split(":")]
            if len(parts) == 3:
                return parts[0] * 3600 + parts[1] * 60 + parts[2]
            if len(parts) == 2:
                return parts[0] * 3600 + parts[1] * 60
            return None
        return float(text) * 60
    except ValueError:
        return None

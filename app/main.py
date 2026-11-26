from __future__ import annotations

import base64
import binascii
import fcntl
import gzip
import json
import logging
import os
import secrets
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .bot import Bot
from .config import BASELINE_SPEC, ENGINE_VERSION, PRESETS, PresetName, TournamentSpec
from .exchange import ExchangeError
from .research import run_tournament
from .store import Store

logger = logging.getLogger(__name__)
ROOT = Path(os.getenv("SUPERTRADE_DATA", "data"))
RAILWAY = bool(os.getenv("RAILWAY_SERVICE_ID"))
PUBLIC_DOMAIN = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").lower()
DASHBOARD_PASSWORD = os.getenv("SUPERTRADE_DASHBOARD_PASSWORD", "")


@asynccontextmanager
async def lifespan(app):
    ROOT.mkdir(parents=True, exist_ok=True)
    with (ROOT / "process.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        app.state.store = Store(ROOT / "state.sqlite3")
        release_path = Path(__file__).with_name("edge-baseline.json.gz")
        if release_path.is_file() and not app.state.store.get("edge_release"):
            with gzip.open(release_path, "rt") as source:
                report = json.load(source)
            if report["kind"] != f"tournament-{ENGINE_VERSION}" or report["spec"] != BASELINE_SPEC.evidence_spec():
                raise RuntimeError("The benchmark release does not match this engine version")
            app.state.store.complete_tournament(f"baseline-{ENGINE_VERSION}", report)
        app.state.bot = Bot(app.state.store)
        app.state.runs_lock = threading.Lock()
        app.state.research = ThreadPoolExecutor(max_workers=1, thread_name_prefix="research")
        app.state.bot.worker.start()
        logger.info("Supertrade started with persistent SQLite; paper-only=%s", app.state.bot.paper_only)
        try:
            yield
        finally:
            app.state.bot.stopping.set()
            app.state.bot.worker.join(timeout=30)
            app.state.research.shutdown(wait=False, cancel_futures=True)


app = FastAPI(title="Supertrade", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
allowed_hosts = ["localhost", "127.0.0.1", "healthcheck.railway.app"]
if PUBLIC_DOMAIN:
    allowed_hosts.append(PUBLIC_DOMAIN)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)


def dashboard_authorized(header):
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        username, separator, password = base64.b64decode(token, validate=True).partition(b":")
    except (ValueError, binascii.Error):
        return False
    return bool(username == b"supertrade" and separator and
                secrets.compare_digest(password, DASHBOARD_PASSWORD.encode()))


@app.middleware("http")
async def dashboard_access(request, call_next):
    path = request.url.path
    if path in {"/health", "/api/health"} and request.method in {"GET", "HEAD"}:
        return await call_next(request)
    if RAILWAY:
        if not DASHBOARD_PASSWORD or len(DASHBOARD_PASSWORD) < 24:
            if path == "/" and request.method == "GET":
                return HTMLResponse("<h1>Supertrade</h1><p>Dashboard access awaits Railway configuration.</p>",
                                    headers={"Cache-Control": "no-store"})
            return JSONResponse({"detail": "Dashboard access is disabled until configured"}, status_code=503)
        if not dashboard_authorized(request.headers.get("authorization", "")):
            return JSONResponse({"detail": "Authentication required"}, status_code=401,
                                headers={"WWW-Authenticate": 'Basic realm="Supertrade", charset="UTF-8"',
                                         "Cache-Control": "no-store"})
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        expected = f"https://{PUBLIC_DOMAIN}" if RAILWAY else None
        if origin and (origin != expected if RAILWAY else urlsplit(origin).hostname not in {"localhost", "127.0.0.1"}):
            return JSONResponse({"detail": "Same-origin actions only"}, status_code=403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "Same-origin actions only"}, status_code=403)
    response = await call_next(request)
    if RAILWAY or path.startswith("/api"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
    return response


class Start(BaseModel):
    mode: Literal["paper", "live"]


class PresetChange(BaseModel):
    preset: PresetName


def action(handler, *args):
    try:
        return handler(*args)
    except ExchangeError as error:
        raise HTTPException(409, str(error)) from error
    except Exception as error:
        logger.error("Bot action failed (%s)", type(error).__name__)
        raise HTTPException(502, f"Bot action failed ({type(error).__name__})") from error


@app.get("/health")
@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/status")
def status():
    return app.state.bot.snapshot()


@app.get("/api/config")
def settings():
    return {"schema": TournamentSpec.model_json_schema(), "backtest": BASELINE_SPEC.model_dump(mode="json"),
            "presets": {name: {"risk": preset.risk, "daily_loss": preset.daily_loss} for name, preset in PRESETS.items()}}


@app.put("/api/strategy")
def strategy(settings: PresetChange):
    return action(app.state.bot.configure, settings.preset)


@app.get("/api/tournament")
def tournament():
    release = app.state.store.get("edge_release", {})
    return app.state.store.runs(release["run_id"]) if release else None


@app.post("/api/bot/start")
def start(request: Start):
    return action(app.state.bot.start, request.mode)


@app.post("/api/bot/stop")
def stop():
    return action(app.state.bot.stop)


@app.post("/api/bot/kill")
def kill():
    return action(app.state.bot.kill)


@app.post("/api/bot/reset")
def reset():
    return action(app.state.bot.reset)


@app.get("/api/logs")
def logs():
    return app.state.store.logs()


@app.get("/api/paper/evidence")
def paper_evidence():
    return action(app.state.bot.paper_evidence)


def execute(run_id, spec):
    store = app.state.store
    try:
        store.update_run(run_id, "running", "Preparing Binance archive")
        report = run_tournament(spec, ROOT / "archive", lambda progress: store.update_run(run_id, "running", progress))
        store.complete_tournament(run_id, report)
    except Exception as error:
        logger.exception("Research run %s failed", run_id)
        store.update_run(run_id, "failed", str(error)[:600])


@app.get("/api/backtests")
def backtests():
    return app.state.store.runs()


@app.post("/api/backtests", status_code=202)
def submit(spec: TournamentSpec):
    with app.state.runs_lock:
        if any(row["status"] in {"queued", "running"} for row in app.state.store.runs()):
            raise HTTPException(409, "A backtest is already running")
        run_id = uuid.uuid4().hex
        app.state.store.new_run(run_id, spec.model_dump(mode="json"))
        app.state.research.submit(execute, run_id, spec)
        return {"id": run_id}


@app.get("/api/backtests/{run_id}")
def result(run_id: str):
    record = app.state.store.runs(run_id)
    if not record:
        raise HTTPException(404, "Backtest unavailable")
    return record


frontend = Path(os.getenv("SUPERTRADE_WEB", "web/dist"))
if frontend.is_dir():
    app.mount("/", StaticFiles(directory=frontend, html=True), name="web")

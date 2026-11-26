from __future__ import annotations

import fcntl
import gzip
import json
import logging
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
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
            if report["kind"] != f"tournament-{ENGINE_VERSION}" or report["spec"] != BASELINE_SPEC.model_dump(mode="json"):
                raise RuntimeError("The benchmark release does not match this engine version")
            app.state.store.complete_tournament(f"baseline-{ENGINE_VERSION}", report)
        app.state.bot = Bot(app.state.store)
        app.state.runs_lock = threading.Lock()
        app.state.research = ThreadPoolExecutor(max_workers=1, thread_name_prefix="research")
        app.state.bot.worker.start()
        try:
            yield
        finally:
            app.state.bot.stopping.set()
            app.state.bot.worker.join(timeout=30)
            app.state.research.shutdown(wait=False, cancel_futures=True)


app = FastAPI(title="Supertrade", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1"])


@app.middleware("http")
async def local_actions(request, call_next):
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        if (origin and urlparse(origin).hostname not in {"localhost", "127.0.0.1"}) or request.headers.get(
            "sec-fetch-site"
        ) == "cross-site":
            return JSONResponse({"detail": "Local-origin actions only"}, status_code=403)
    response = await call_next(request)
    if request.url.path.startswith("/api"):
        response.headers["Cache-Control"] = "no-store"
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

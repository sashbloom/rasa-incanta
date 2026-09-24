"""Rasa Incanta: one process serving the API and the built React app.

Every route answers both at `/` and under `/reports/rasa-incanta/` (see `api/prefix.py`).
`/health` must keep working at the root. The board is open: no route asks who the caller is.
`api/auth.py` keeps the login and portal identity code, unwired, for when that changes.
"""
import logging
import os
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from api import __version__
from api.config import REPORT_PREFIX, get_settings
from api.db import get_session, get_sessionmaker
from api.domain.weeks import week_start
from api.engine.run import run_week
from api.models import Run
from api.prefix import MountUnderPrefix
from api.sources.zoho import fetch_deals
from api.views import deal_view, deals_view, run_payload, week_view

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("rasa_incanta")


@asynccontextmanager
async def lifespan(_: FastAPI):
    for warning in get_settings().startup_warnings():
        logger.warning(warning)
    mark_interrupted_runs()
    yield


fastapi_app = FastAPI(title="Rasa Incanta", version=__version__, lifespan=lifespan)

if get_settings().is_local:
    # Vite's dev server (npm run dev) calls http://localhost:8000 directly.
    fastapi_app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173"],
                               allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


# ---------------------------------------------------------------- health

@fastapi_app.get("/health")
def health(response: Response, session: Session = Depends(get_session)) -> dict:
    try:
        session.execute(text("SELECT 1"))
        database = "ok"
    except Exception:  # report, never crash the health check
        logger.exception("Database check failed")
        database = "error"
        response.status_code = 503
    return {
        "status": "ok" if database == "ok" else "degraded",
        "database": database,
        "environment": get_settings().environment,
        "version": __version__,
    }


# ---------------------------------------------------------------- board

@fastapi_app.get("/api/week")
def week(session: Session = Depends(get_session)) -> dict:
    return week_view(session, get_settings())


@fastapi_app.get("/api/deals/{deal_id}")
def deal(deal_id: uuid.UUID, session: Session = Depends(get_session)) -> dict:
    view = deal_view(session, deal_id)
    if view is None:
        raise HTTPException(404, "Deal not found.")
    return view


@fastapi_app.get("/api/deals")
def deals(session: Session = Depends(get_session)) -> list[dict]:
    """Every deal, active or not, each with its latest NBAs."""
    return deals_view(session)


# ---------------------------------------------------------------- runs

# One run at a time in this process: a second POST while one is going gets a 409, so repeated
# clicks cannot stack up Zoho reads and Claude calls.
_run_lock = threading.Lock()


def run_sources() -> dict:
    """What a run reads from and drafts with. Tests override this dependency with fakes."""
    return {"fetch": fetch_deals, "llm_client": None}  # None = the real Claude client from settings


def mark_interrupted_runs() -> None:
    """A run still marked running at startup was cut off by a restart; say so instead of spinning forever."""
    try:
        with get_sessionmaker()() as session:
            session.execute(update(Run).where(Run.status == "running").values(
                status="failed", error="Interrupted by a restart.", finished_at=datetime.now(timezone.utc)))
            session.commit()
    except Exception:  # never block startup on this; migrations may not have run in a bare dev setup
        logger.exception("Could not check for interrupted runs")


def execute_run(run_id: uuid.UUID, sources: dict) -> None:
    try:
        with get_sessionmaker()() as session:
            try:
                run_week(session, get_settings(), fetch=sources["fetch"], llm_client=sources["llm_client"],
                         nba_limit=None, run=session.get(Run, run_id))
            except Exception as exc:
                logger.exception("Run %s crashed", run_id)  # the detail stays in the server log
                session.rollback()
                run = session.get(Run, run_id)
                run.status, run.finished_at = "failed", datetime.now(timezone.utc)
                run.error = f"The run stopped unexpectedly ({type(exc).__name__}). Details are in the server log."
                session.commit()
    finally:
        _run_lock.release()


@fastapi_app.post("/api/run", status_code=202)
def start_run(background: BackgroundTasks, session: Session = Depends(get_session),
              sources: dict = Depends(run_sources)):
    """Start a full run (Zoho pull, context cards, an NBA for every deal) and return it as `running`.
    Poll GET /api/run for progress and the final status."""
    if not _run_lock.acquire(blocking=False):
        current = session.scalar(select(Run).where(Run.status == "running").order_by(Run.started_at.desc()).limit(1))
        return JSONResponse({"detail": "A run is already in progress.", "run": run_payload(current) if current else None},
                            status_code=409)
    try:
        now = datetime.now(timezone.utc)
        run = Run(week_start=week_start(now, get_settings().timezone), kind="manual", status="running",
                  started_at=now, stats={})
        session.add(run)
        session.commit()
    except Exception:
        _run_lock.release()
        raise
    background.add_task(execute_run, run.id, sources)
    return run_payload(run)


@fastapi_app.get("/api/run")
def latest_run(session: Session = Depends(get_session)) -> dict:
    """The most recent run: status, progress counts, and the error if it failed."""
    run = session.scalar(select(Run).order_by(Run.started_at.desc()).limit(1))
    if run is None:
        raise HTTPException(404, "No runs yet.")
    return run_payload(run)


@fastapi_app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
def api_not_found(rest: str) -> JSONResponse:
    # An unknown endpoint must 404 as JSON, never fall through to index.html with a 200.
    return JSONResponse({"detail": "Not found."}, status_code=404)


# ---------------------------------------------------------------- the React app

def resolve_static(static_dir: str, full_path: str) -> str | None:
    """The real file for `full_path` inside `static_dir`, or None. Contained by realpath:
    anything that resolves outside the build directory (../, absolute paths, symlinks) is refused."""
    root = os.path.realpath(static_dir)
    candidate = os.path.realpath(os.path.join(root, full_path))
    try:
        if os.path.commonpath([root, candidate]) != root:
            return None
    except ValueError:  # different drives on Windows
        return None
    return candidate if os.path.isfile(candidate) else None


@fastapi_app.get("/", include_in_schema=False)
@fastapi_app.get("/{full_path:path}", include_in_schema=False)
def spa(full_path: str = "") -> Response:
    static_dir = get_settings().static_dir
    if full_path:
        found = resolve_static(static_dir, full_path)
        if found:
            immutable = full_path.startswith("assets/")
            return FileResponse(found, headers={"Cache-Control": "public, max-age=31536000, immutable"
                                                if immutable else "no-cache"})
        if full_path.startswith("assets/") or "." in full_path.rsplit("/", 1)[-1]:
            return JSONResponse({"detail": "Not found."}, status_code=404)  # a missing file, not a page
    index = resolve_static(static_dir, "index.html")
    if index is None:
        return JSONResponse({"detail": "The frontend has not been built. Run npm run build in frontend/."},
                            status_code=503)
    return FileResponse(index, headers={"Cache-Control": "no-cache"})


app = MountUnderPrefix(fastapi_app, REPORT_PREFIX)

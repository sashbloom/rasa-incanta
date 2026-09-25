"""Rasa Incanta: one process serving the API and the built React app.

Every route answers both at `/` and under `/reports/rasa-incanta/` (see `api/prefix.py`).
`/health` must keep working at the root. The board is open: no route asks who the caller is.
`api/auth.py` keeps the login and portal identity code, unwired, for when that changes.
"""
import logging
import os
import secrets
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from api import __version__
from api.config import REPORT_PREFIX, get_settings
from api.db import get_session, get_sessionmaker
from api.domain.weeks import week_start
from api.engine.run import run_week
from api.models import Meeting, Run
from api.prefix import MountUnderPrefix
from api.sources import outlook, readai
from api.sources.setu import fetch_case_studies
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
    """What a run reads from and drafts with. Tests override this dependency with fakes.
    None means the real source (or, for llm_client, the Claude client from settings)."""
    return {"fetch": fetch_deals, "fetch_setu": fetch_case_studies, "fetch_mail": None,
            "capability_fn": None, "score_company_fn": None, "llm_client": None}


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
                         fetch_setu=sources.get("fetch_setu", fetch_case_studies),
                         fetch_mail=sources.get("fetch_mail"), capability_fn=sources.get("capability_fn"),
                         score_company_fn=sources.get("score_company_fn"),
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


# ---------------------------------------------------------------- Read.ai webhook

@fastapi_app.post("/api/webhooks/readai", include_in_schema=False)
async def readai_webhook(request: Request, session: Session = Depends(get_session)) -> Response:
    """Read.ai's signed workspace webhook. 401 for anything unverifiable (never 2xx, so a genuine
    retry is not mistaken for delivered), 500 on a processing error so Read.ai retries, 204 for a
    duplicate or a meeting_start, 202 when a meeting is stored."""
    body = await request.body()
    if len(body) > readai.MAX_BODY_BYTES:
        return Response(status_code=413)
    try:
        outcome = readai.handle_delivery(session, body, request.headers.get("x-read-signature"),
                                         get_settings().readai_webhook_secret)
    except readai.WebhookRejected as exc:
        logger.warning("Read.ai webhook rejected: %s", exc)
        return Response(status_code=401)
    except Exception:
        logger.exception("Read.ai webhook delivery could not be processed")
        session.rollback()
        return Response(status_code=500)
    return Response(status_code=202 if outcome == "stored" else 204)


# ---------------------------------------------------------------- sources and the Outlook sign-in

# One-time sign-in states: state -> (expires, redirect URI the sign-in started with). Microsoft
# requires the code exchange to use exactly that redirect URI, so it travels with the state.
_OUTLOOK_STATES: dict[str, tuple[datetime, str]] = {}
_STATE_TTL = timedelta(minutes=15)
_states_lock = threading.Lock()


def _take_state(state: str) -> str | None:
    """The redirect URI for a live, unused state (consuming it), or None."""
    now = datetime.now(timezone.utc)
    with _states_lock:
        for key, (expires, _) in list(_OUTLOOK_STATES.items()):
            if expires < now:
                del _OUTLOOK_STATES[key]
        entry = _OUTLOOK_STATES.pop(state, None) if state else None
    return entry[1] if entry else None


def _origin(request: Request) -> str:
    """This app's public origin. Railway (and the portal gateway) terminate TLS and forward the
    original scheme and host; trusting them here is safe because Microsoft only redirects to URIs
    registered in Azure, so a forged host can only ever produce a sign-in Microsoft refuses."""
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme).split(",")[0].strip()
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc).split(",")[0].strip()
    return f"{proto}://{host}"


def _back_to_sources(outcome: str) -> RedirectResponse:
    # A fixed status word only: nothing from Microsoft's query string is ever reflected back.
    return RedirectResponse(f"{REPORT_PREFIX}/sources?outlook={outcome}", status_code=303)


@fastapi_app.get("/api/sources")
def sources_status(request: Request, session: Session = Depends(get_session)) -> dict:
    """Where each source stands: the latest run's status per source, the Outlook connection (with
    the redirect URI to register in Azure), and how many Read.ai meetings have arrived."""
    settings = get_settings()
    latest = session.scalar(select(Run).order_by(Run.started_at.desc()).limit(1))
    return {
        "last_run": run_payload(latest) if latest else None,
        "outlook": {**outlook.status(session, settings),
                    "redirect_uri": outlook.redirect_uri_for(settings, _origin(request))},
        "readai": {"configured": bool(settings.readai_webhook_secret),
                   "meetings": session.scalar(select(func.count()).select_from(Meeting)),
                   "webhook_path": "/api/webhooks/readai"},
        "exa": {"configured": bool(settings.exa_api_key)},
        "anthropic": {"configured": bool(settings.anthropic_api_key)},
    }


@fastapi_app.post("/api/outlook/connect/start")
def outlook_connect_start(request: Request) -> dict:
    """Begin Myrah's one-time sign-in: the page sends the browser to `authorize_url`, and
    Microsoft returns it to GET /api/outlook/callback."""
    settings = get_settings()
    state = secrets.token_urlsafe(24)
    redirect_uri = outlook.redirect_uri_for(settings, _origin(request))
    try:
        url = outlook.authorization_url(settings, state, redirect_uri)
    except outlook.OutlookError as exc:
        raise HTTPException(400, str(exc))
    with _states_lock:
        _OUTLOOK_STATES[state] = (datetime.now(timezone.utc) + _STATE_TTL, redirect_uri)
    return {"authorize_url": url, "redirect_uri": redirect_uri}


@fastapi_app.get("/api/outlook/callback", include_in_schema=False)
def outlook_callback(code: str | None = None, state: str | None = None, error: str | None = None,
                     session: Session = Depends(get_session)) -> RedirectResponse:
    """Microsoft's redirect after the sign-in. Checks the one-time state, exchanges the code, keeps
    the tokens only if Myrah signed in, then returns the browser to the Sources page."""
    redirect_uri = _take_state(state or "")
    if error:
        logger.info("Outlook sign-in declined or failed at Microsoft: %s", error[:100])
        return _back_to_sources("denied")
    if redirect_uri is None:
        return _back_to_sources("expired")
    if not code:
        return _back_to_sources("failed")
    try:
        with httpx.Client(timeout=30.0) as http:
            outlook.connect(session, get_settings(), code, http, redirect_uri)
    except outlook.WrongAccount as exc:
        logger.warning("Outlook sign-in refused: %s", exc)
        return _back_to_sources("wrong_account")
    except outlook.OutlookError as exc:
        logger.warning("Outlook sign-in failed: %s", exc)
        return _back_to_sources("failed")
    except httpx.HTTPError as exc:
        logger.warning("Outlook sign-in could not reach Microsoft: %s", exc)
        return _back_to_sources("failed")
    return _back_to_sources("connected")


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

"""Rasa Incanta: one process serving the API and the built React app.

Every route answers both at `/` and under `/reports/rasa-incanta/` (see `api/prefix.py`).
`/health` must keep working at the root. The board is open: no route asks who the caller is.
`api/auth.py` keeps the login and portal identity code, unwired, for when that changes.
"""
import hmac
import logging
import os
import secrets
import threading
import uuid
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from api import __version__
from api.config import REPORT_PREFIX, get_settings
from api.db import get_session, get_sessionmaker
from api.domain.matching import has_industry, label_matches, resolve_case_study_industry
from api.domain.stages import board_for
from api.domain.weeks import week_start
from api.engine import icp_signal
from api.engine.persona_signal import usable_name
from api.engine.run import run_week
from api.icp import setu_db, setu_embeddings
from api.models import Meeting, Run, SetuCaseStudyContext
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


# ---------------------------------------------------------------- debug endpoints

def require_debug_key(request: Request) -> None:
    """Gate for every debug endpoint (raw source dumps, internal tooling that isn't for the
    board): send `X-Debug-Key: <SESSION_SECRET>` or get a 404, indistinguishable from a route
    that does not exist at all — a wrong or missing key never reveals that the endpoint is even
    there. A header, not a query parameter, so the key never lands in a URL that gets logged by
    Railway, a proxy in front of it, or a browser's history. An unset SESSION_SECRET means every
    debug route refuses unconditionally, whatever the header holds: an empty secret is not a
    wildcard that matches an empty or missing header (CLAUDE.md rule 6, fail closed).
    `hmac.compare_digest` avoids a timing side-channel on the comparison.

    Add `dependencies=[Depends(require_debug_key)]` to any new debug route to gate it the same
    way; nothing else about the route needs to change."""
    key = request.headers.get("x-debug-key") or ""
    expected = get_settings().session_secret.strip()
    if not expected or not key or not hmac.compare_digest(key.encode(), expected.encode()):
        raise HTTPException(404)


def _fetch_case_studies_or_503() -> list[dict]:
    try:
        return setu_db.fetch_case_studies()
    except Exception as exc:
        raise HTTPException(503, f"Could not read the Setu database: {exc}") from exc


@fastapi_app.get("/api/setu/case-studies", dependencies=[Depends(require_debug_key)])
def setu_case_studies() -> list[dict]:
    """Every raw case study on file in Setu, unfiltered — a direct read, gated behind
    `X-Debug-Key: <SESSION_SECRET>` (require_debug_key), for checking what is actually in the
    corpus. Same source `find_case_study_matches_embedded()` scores."""
    return [{"entity_name": cs["entity_name"], "content": cs["content"], "industry": cs["industry"],
            "service_line": cs["service_line"]} for cs in _fetch_case_studies_or_503()]


@fastapi_app.get("/api/setu/enriched", dependencies=[Depends(require_debug_key)])
def setu_enriched(session: Session = Depends(get_session)) -> list[dict]:
    """Every case study plus our generated context (setu_case_study_contexts), and which case is
    which: `real_content` (its own text is >= setu_embeddings.MIN_CONTENT_CHARS, no enrichment
    needed), `enriched` (thin, and a generated context is cached under this exact source text), or
    `pending` (thin, not yet enriched — the next run that embeds case studies will enrich it)."""
    case_studies = _fetch_case_studies_or_503()
    hashes = [setu_embeddings.source_hash(cs["entity_name"], cs["industry"], cs["service_line"], cs["content"] or "")
             for cs in case_studies]
    contexts = {row.source_hash: row.generated_context for row in
               session.scalars(select(SetuCaseStudyContext).where(SetuCaseStudyContext.source_hash.in_(hashes)))}

    out = []
    for cs, h in zip(case_studies, hashes):
        thin = len((cs["content"] or "").strip()) < setu_embeddings.MIN_CONTENT_CHARS
        generated_context = contexts.get(h)
        status = "real_content" if not thin else ("enriched" if generated_context is not None else "pending")
        out.append({"entity_name": cs["entity_name"], "content": cs["content"], "industry": cs["industry"],
                    "service_line": cs["service_line"], "status": status, "generated_context": generated_context})
    return out


_RICH_FIELD_COUNT = 4  # problem statements, industry, contact_id, business area


def _most_frequent_contact(reachouts: list) -> str | None:
    """The contact `person` named most often in a deal's outreach log — plain frequency, not
    persona_signal.primary_contact()'s recency/role pick, so this stays a query over
    reachout_tracker with no ICP or persona logic involved. Junk (an email address, "NA", a blank
    field) is filtered with the same usable_name() persona_signal itself uses, so a deal isn't
    shown as having a contact when the only "name" on file is unusable."""
    names = [n for n in (usable_name(r.person) for r in reachouts) if n]
    return Counter(names).most_common(1)[0][0] if names else None


def _richness(z, reachouts: list, setu_industries: list[str]) -> dict:
    reachout_count = len(reachouts)
    contact_name = _most_frequent_contact(reachouts)
    filled = sum([
        bool(z.problem_statements),
        has_industry(z.industry),
        bool(z.raw.get("contact_id")),
        bool(z.business_area),
    ])
    industry_match = has_industry(z.industry) and label_matches(
        z.industry, setu_industries, resolve_alias=resolve_case_study_industry)
    board = board_for(z.stage)
    # Outreach counts for most, a Setu industry match is worth a couple of filled fields on its
    # own (a real capability signal, not just a filled box), and the field count rounds it out.
    score = reachout_count + (2 if industry_match else 0) + filled
    return {"deal": z, "board": board, "reachout_count": reachout_count, "contact_name": contact_name,
           "industry_match": industry_match, "filled_field_count": filled, "score": score}


class ImportedIcpScore(BaseModel):
    company: str = Field(min_length=1, max_length=300)
    verdict: str = Field(min_length=1, max_length=200)
    right_to_win: str | None = Field(default=None, max_length=1000)
    scored_at: datetime


@fastapi_app.post("/api/icp/import", dependencies=[Depends(require_debug_key)])
def import_icp_scores(scores: list[ImportedIcpScore], session: Session = Depends(get_session)) -> dict:
    """Load ICP scores made elsewhere into company_icp (body: a JSON list of {company, verdict,
    right_to_win, scored_at}). A company already scored within ICP_CACHE_DAYS is skipped, and so is a
    repeat within the same request. Append-only: nothing is overwritten. Gated like every debug route."""
    imported, skipped = icp_signal.import_scores(session, [s.model_dump() for s in scores],
                                                 datetime.now(timezone.utc), get_settings().icp_cache_days)
    return {"imported": imported, "skipped": skipped}


@fastapi_app.get("/api/debug/richest-deals", dependencies=[Depends(require_debug_key)])
def richest_deals() -> list[dict]:
    """The best-documented deals for a PILOT_COMPANIES shortlist, spread across all three boards
    so a pilot isn't accidentally all-pipeline or all-prospect. Richness = outreach log entries,
    a Setu industry match (the same check engine/capability.py uses for the real capability
    signal), and how many of four key Zoho fields are filled (problem statements, industry,
    contact_id, business area).

    Ranks within each board first, takes the top 5 from each (15 across 3 boards), then backfills
    any shortfall — a board with fewer than 5 in-scope deals doesn't shrink the total — from
    whatever is left, richest first."""
    settings = get_settings()
    zoho = fetch_deals(settings)
    if not zoho.ok:
        raise HTTPException(503, f"Could not read the Zoho database: {zoho.error}")
    setu = fetch_case_studies(settings)
    setu_industries = [cs.industry for cs in setu.case_studies if cs.industry] if setu.ok else []

    scored = [_richness(z, zoho.reachouts.get(z.zoho_id, []), setu_industries) for z in zoho.deals]
    by_board: dict = {}
    for s in scored:
        by_board.setdefault(s["board"], []).append(s)
    for rows in by_board.values():
        rows.sort(key=lambda s: s["score"], reverse=True)

    per_board = 5
    picked = [s for rows in by_board.values() for s in rows[:per_board]]
    leftover = sorted((s for rows in by_board.values() for s in rows[per_board:]),
                      key=lambda s: s["score"], reverse=True)
    picked += leftover[:max(0, 15 - len(picked))]
    picked.sort(key=lambda s: ((s["board"].value if s["board"] else ""), -s["score"]))

    return [{
        "company": s["deal"].account_name, "deal_name": s["deal"].name, "stage": s["deal"].stage,
        "board": s["board"].value if s["board"] else None, "outreach_count": s["reachout_count"],
        "contact_name": s["contact_name"], "outreach_has_contact": s["contact_name"] is not None,
        "setu_industry_match": s["industry_match"], "filled_field_count": s["filled_field_count"],
    } for s in picked[:15]]


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

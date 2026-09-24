"""Rasa Incanta: one process serving the API and the built React app.

Every route answers both at `/` and under `/reports/rasa-incanta/` (see `api/prefix.py`).
`/health` is public and must keep working at the root. Everything under `/api/` except
sign-in needs a user (`api/auth.py`), and every deal query is scoped by the user's SBUs.
"""
import logging
import os
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from api import __version__
from api.auth import (
    PUBLIC_ENDPOINTS,
    SESSION_COOKIE,
    allowed_sbus,
    boot_problems,
    current_user,
    gate,
    issue_session,
    login,
)
from api.config import REPORT_PREFIX, get_settings
from api.db import get_session
from api.models import User
from api.prefix import MountUnderPrefix
from api.views import deal_view, week_view

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("rasa_incanta")


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    problems = boot_problems(settings)
    if problems:  # refuse to boot; Railway's healthcheck then keeps the last good deploy
        for problem in problems:
            logger.error(problem)
        raise RuntimeError("Refusing to start: " + " ".join(problems))
    for warning in settings.startup_warnings():
        logger.warning(warning)
    yield


# Protect by default: every route needs a user unless it is listed in PUBLIC_ENDPOINTS below.
fastapi_app = FastAPI(title="Rasa Incanta", version=__version__, lifespan=lifespan, dependencies=[Depends(gate)])

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


# ---------------------------------------------------------------- sign-in

class LoginBody(BaseModel):
    username: str
    password: str


def me_payload(user: User, request: Request) -> dict:
    return {"username": user.username, "role": user.role, "sbus": allowed_sbus(user),
            "via_portal": bool(getattr(request.state, "via_portal", False))}


@fastapi_app.post("/api/auth/login")
def auth_login(body: LoginBody, request: Request, response: Response, session: Session = Depends(get_session)) -> dict:
    settings = get_settings()
    if not settings.session_secret:
        raise HTTPException(503, "Sign-in is not configured on this server.")
    user = login(session, body.username, body.password)
    if user is None:
        raise HTTPException(401, "That username and password don't match.")
    response.set_cookie(
        SESSION_COOKIE, issue_session(settings, user.id), max_age=settings.session_max_age_hours * 3600,
        httponly=True, secure=not settings.is_local, samesite="lax", path="/",
    )
    return me_payload(user, request)


@fastapi_app.post("/api/auth/logout")
def auth_logout(response: Response) -> dict:
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@fastapi_app.get("/api/me")
def me(request: Request, user: User = Depends(current_user)) -> dict:
    return me_payload(user, request)


# ---------------------------------------------------------------- board

@fastapi_app.get("/api/week")
def week(user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    return week_view(session, user, get_settings())


@fastapi_app.get("/api/deals/{deal_id}")
def deal(deal_id: uuid.UUID, user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    view = deal_view(session, user, deal_id)
    if view is None:
        raise HTTPException(404, "Deal not found.")
    return view


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


PUBLIC_ENDPOINTS.update({health, auth_login, auth_logout, api_not_found, spa})

app = MountUnderPrefix(fastapi_app, REPORT_PREFIX)

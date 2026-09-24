"""Who is asking: our own login, or the Practus Portal's identity handoff.

NOT WIRED IN. The board is open, so no route calls anything here. The module is kept, with its
tests, so identity can come back without being rebuilt. To gate routes again, call
`authenticate` from an app-wide dependency and scope `views.visible_deals` by `allowed_sbus`.

Two front doors, one rule each.

Portal (CGO reports standard, behind PORTAL_IDENTITY_ENABLED, off by default). The portal
proxies requests with `Authorization: Bearer <CGO_REPORTS_API_KEY>` and
`X-CGO-Portal-User-Email`. The email header is forgeable by anyone who finds our URL, so it
is never read until the key matches. Once the key matches we never fall back to the session:
a missing header, an unmapped email or an inactive account is a 401. An unset key matches
nothing, so a half-configured deploy cannot turn the header into a way in.

Our own login. Username and password, checked against a scrypt hash, then an HMAC-signed
session cookie. An unset SESSION_SECRET refuses every sign-in and every session. The user
row is re-read on every request, so deactivating someone locks them out at once.

Scoping, once wired: a user sees deals whose SBU is in their `user_allowed_sbus`. No rows, no deals.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
import uuid

from fastapi import HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.config import Settings
from api.models import User

PORTAL_EMAIL_HEADER = "X-CGO-Portal-User-Email"
SESSION_COOKIE = "ri_session"

_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


# ---------------------------------------------------------------- passwords

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **_SCRYPT)
    return "scrypt${n}${r}${p}${salt}${digest}".format(
        **_SCRYPT, salt=salt.hex(), digest=digest.hex(),
    )


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual.hex(), digest)


_DUMMY_HASH = hash_password(secrets.token_hex(8))  # evens out timing for unknown usernames


# ---------------------------------------------------------------- session tokens

def _sign(secret: str, payload: str) -> str:
    mac = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")


def issue_session(settings: Settings, user_id: uuid.UUID, now: float | None = None) -> str:
    if not settings.session_secret:
        raise HTTPException(503, "Sign-in is not configured on this server.")
    payload = f"{user_id}.{int(now if now is not None else time.time())}"
    return f"{payload}.{_sign(settings.session_secret, payload)}"


def read_session(settings: Settings, token: str | None, now: float | None = None) -> uuid.UUID | None:
    """The user id a valid, unexpired token was issued for; None for anything else."""
    if not token or not settings.session_secret:
        return None
    try:
        user_id, issued, signature = token.split(".")
        if not hmac.compare_digest(signature, _sign(settings.session_secret, f"{user_id}.{issued}")):
            return None
        age = (now if now is not None else time.time()) - int(issued)
        if age < 0 or age > settings.session_max_age_hours * 3600:
            return None
        return uuid.UUID(user_id)
    except ValueError:
        return None


# ---------------------------------------------------------------- front door

def _bearer(request: Request) -> str:
    """Bearer token from the Authorization header only, never from the query string."""
    auth = request.headers.get("authorization") or ""
    return auth[7:].strip() if auth.lower().startswith("bearer ") else ""


def is_portal_key(settings: Settings, token: str) -> bool:
    expected = settings.cgo_reports_api_key.strip()
    if not expected or not token:
        return False
    return hmac.compare_digest(token.encode(), expected.encode())


def authenticate(request: Request, session: Session, settings: Settings) -> User:
    if settings.portal_identity_enabled and is_portal_key(settings, _bearer(request)):
        email = (request.headers.get(PORTAL_EMAIL_HEADER) or "").strip().lower()
        if not email:
            raise HTTPException(401, "Portal request with no identity header.")
        # One query, is_active read with it, never cached: the two doors must agree.
        user = session.scalar(select(User).where(func.lower(User.ms_email) == email))
        if user is None or not user.is_active:
            raise HTTPException(401, "No active account for this Microsoft email.")
        request.state.via_portal = True
        return user  # never fall through to the session flow

    user_id = read_session(settings, request.cookies.get(SESSION_COOKIE))
    if user_id is None:
        raise HTTPException(401, "Not signed in.")
    user = session.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(401, "Account inactive or removed.")
    request.state.via_portal = False
    return user


def login(session: Session, username: str, password: str) -> User | None:
    user = session.scalar(select(User).where(User.username == username.strip().lower()))
    if user is None:
        verify_password(password, _DUMMY_HASH)
        return None
    if not verify_password(password, user.password_hash) or not user.is_active:
        return None
    return user


def allowed_sbus(user: User) -> list[str]:
    return sorted(a.sbu for a in user.allowed_sbus)

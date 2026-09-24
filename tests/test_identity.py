"""The identity code kept in api/auth.py, unwired while the board is open. Tested directly so it
still works when it is wired back in: the five portal rules, passwords, sessions, and the schema."""
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from fastapi import HTTPException
from starlette.requests import Request

from api.auth import authenticate, hash_password, issue_session, read_session, verify_password, SESSION_COOKIE
from api.db import get_sessionmaker
from api.models import User, UserAllowedSbu
from tests.conftest import TEST_PORTAL_KEY
from tests.fakes import settings

PORTAL_EMAIL = "sreejit.nair@roibypractus.com"
SECRET = "identity-test-secret"


def request(headers=(), cookie=None, query=b""):
    raw = [(k.lower().encode() if isinstance(k, str) else k, v.encode() if isinstance(v, str) else v)
           for k, v in headers]
    if cookie:
        raw.append((b"cookie", f"{SESSION_COOKIE}={cookie}".encode()))
    return Request({"type": "http", "method": "GET", "path": "/api/week", "headers": raw, "query_string": query})


def portal_on(**overrides):
    values = dict(portal_identity_enabled=True, cgo_reports_api_key=TEST_PORTAL_KEY, session_secret=SECRET)
    return settings(**{**values, **overrides})


def add_user(username, ms_email=None, active=True, sbus=()):
    with get_sessionmaker()() as s:
        user = User(username=username, ms_email=ms_email, is_active=active,
                    password_hash=hash_password("correct horse battery"))
        user.allowed_sbus = [UserAllowedSbu(sbu=x) for x in sbus]
        s.add(user)
        s.commit()
        return user.id


def resolve(req, cfg):
    with get_sessionmaker()() as s:
        return authenticate(req, s, cfg).username


def refused(req, cfg):
    with get_sessionmaker()() as s, pytest.raises(HTTPException) as err:
        authenticate(req, s, cfg)
    return err.value.status_code == 401


def key_and(email=PORTAL_EMAIL, key=TEST_PORTAL_KEY):
    headers = [("authorization", f"Bearer {key}")]
    return headers + ([("x-cgo-portal-user-email", email)] if email is not None else [])


# ---------------------------------------------------------------- the five portal rules

def test_key_plus_mapped_email_resolves_the_user_ignoring_case(migrated):
    add_user("sreejit", ms_email="Sreejit.Nair@RoiByPractus.com")
    assert resolve(request(key_and(PORTAL_EMAIL.upper())), portal_on()) == "sreejit"


def test_key_with_no_identity_header_is_refused(migrated):
    assert refused(request(key_and(email=None)), portal_on())


def test_key_with_unmapped_email_is_refused_even_with_a_valid_session(migrated):
    user_id = add_user("rao")
    cookie = issue_session(portal_on(), user_id)
    assert refused(request(key_and("stranger@roibypractus.com"), cookie=cookie), portal_on())


def test_key_with_inactive_account_is_refused(migrated):
    add_user("sreejit", ms_email=PORTAL_EMAIL, active=False)
    assert refused(request(key_and()), portal_on())


def test_header_without_the_key_falls_to_the_session(migrated):
    add_user("sreejit", ms_email=PORTAL_EMAIL)
    rao = add_user("rao")
    cookie = issue_session(portal_on(), rao)
    assert resolve(request([("x-cgo-portal-user-email", PORTAL_EMAIL)], cookie=cookie), portal_on()) == "rao"
    assert refused(request(key_and(key="guess")), portal_on())


def test_flag_off_ignores_the_portal(migrated):
    add_user("sreejit", ms_email=PORTAL_EMAIL)
    assert refused(request(key_and()), portal_on(portal_identity_enabled=False))


def test_unset_key_refuses_everything(migrated):
    add_user("sreejit", ms_email=PORTAL_EMAIL)
    assert refused(request(key_and(key="")), portal_on(cgo_reports_api_key=""))


def test_key_is_never_read_from_the_query_string(migrated):
    add_user("sreejit", ms_email=PORTAL_EMAIL)
    req = request([("x-cgo-portal-user-email", PORTAL_EMAIL)], query=f"token={TEST_PORTAL_KEY}".encode())
    assert refused(req, portal_on())


def test_non_ascii_bearer_is_refused_not_a_crash(migrated):
    add_user("sreejit", ms_email=PORTAL_EMAIL)
    req = request([(b"authorization", b"Bearer \xff\xfe-not-ascii"), (b"x-cgo-portal-user-email", PORTAL_EMAIL.encode())])
    assert refused(req, portal_on())


# ---------------------------------------------------------------- passwords and sessions

def test_passwords_hash_and_verify():
    stored = hash_password("correct horse battery")
    assert stored.startswith("scrypt$") and "correct horse" not in stored
    assert verify_password("correct horse battery", stored)
    assert not verify_password("wrong", stored)
    assert not verify_password("x", None) and not verify_password("x", "garbage") and not verify_password("", "!")


def test_session_tokens_are_signed_and_expire():
    s = settings(session_secret="k1")
    user_id = uuid.uuid4()
    token = issue_session(s, user_id, now=1000)
    assert read_session(s, token, now=1001) == user_id
    assert read_session(settings(session_secret="k2"), token, now=1001) is None
    assert read_session(s, token.replace(str(user_id), str(uuid.uuid4())), now=1001) is None
    assert read_session(s, token, now=1000 + 13 * 3600) is None
    assert read_session(settings(session_secret=""), token, now=1001) is None


# ---------------------------------------------------------------- schema kept for later

def test_ms_email_is_unique_ignoring_case_and_blank_is_null(migrated):
    with get_sessionmaker()() as s:
        s.add_all([User(username="a", ms_email="  "), User(username="b", ms_email="")])
        s.commit()  # two blanks are two NULLs, not a collision
        assert s.scalars(select(User.ms_email).order_by(User.username)).all() == [None, None]
        s.add(User(username="c", ms_email="Person@roibypractus.com"))
        s.commit()
        s.add(User(username="d", ms_email="person@ROIBYPRACTUS.com"))
        with pytest.raises(IntegrityError):
            s.commit()


def test_role_is_checked_by_the_schema(migrated):
    with get_sessionmaker()() as s:
        s.add(User(username="x", role="owner"))
        with pytest.raises(IntegrityError):
            s.commit()


def test_a_user_without_a_password_gets_an_unusable_one(migrated):
    with get_sessionmaker()() as s:
        user = User(username="portal")
        s.add(user)
        s.commit()
        assert user.password_hash == "!" and not verify_password("", user.password_hash)

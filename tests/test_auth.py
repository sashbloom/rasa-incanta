"""Our own login, the portal identity handoff (CGO reports standard) and SBU scoping."""
import time
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from api.auth import hash_password, issue_session, read_session, verify_password
from api.db import get_sessionmaker
from api.engine.run import run_week
from api.models import User, UserAllowedSbu
from tests.conftest import TEST_PORTAL_KEY
from tests.fakes import NOW, FakeClaude, claude_response, load, settings
from tests.test_run import zoho_fetch

PASSWORD = "correct horse battery"
PORTAL_EMAIL = "sreejit.nair@roibypractus.com"


def add_user(username, sbus=(), password=PASSWORD, ms_email=None, active=True, role="user"):
    with get_sessionmaker()() as s:
        user = User(username=username, password_hash=hash_password(password) if password else None,
                    ms_email=ms_email, is_active=active, role=role)
        user.allowed_sbus = [UserAllowedSbu(sbu=x) for x in sbus]
        s.add(user)
        s.commit()
        return user.id


def seed_deals():
    with get_sessionmaker()() as s:
        run_week(s, settings(), now=NOW, fetch=zoho_fetch(), llm_client=FakeClaude(claude_response(load("claude/nba_reply.json"))))


def sign_in(client, username="rao", password=PASSWORD):
    return client.post("/api/auth/login", json={"username": username, "password": password})


@pytest.fixture()
def portal(monkeypatch):
    from api.config import get_settings

    monkeypatch.setenv("PORTAL_IDENTITY_ENABLED", "true")
    monkeypatch.setenv("CGO_REPORTS_API_KEY", TEST_PORTAL_KEY)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def portal_headers(email=PORTAL_EMAIL, key=TEST_PORTAL_KEY):
    headers = {"Authorization": f"Bearer {key}"}
    if email is not None:
        headers["X-CGO-Portal-User-Email"] = email
    return headers


# ---------------------------------------------------------------- building blocks

def test_passwords_hash_and_verify():
    stored = hash_password(PASSWORD)
    assert stored.startswith("scrypt$") and PASSWORD not in stored
    assert verify_password(PASSWORD, stored)
    assert not verify_password("wrong", stored)
    assert not verify_password(PASSWORD, None)
    assert not verify_password(PASSWORD, "garbage")


def test_session_tokens_are_signed_and_expire():
    s = settings(session_secret="k1")
    user_id = uuid.uuid4()
    token = issue_session(s, user_id, now=1000)
    assert read_session(s, token, now=1001) == user_id
    assert read_session(settings(session_secret="k2"), token, now=1001) is None  # other secret
    assert read_session(s, token.replace(str(user_id), str(uuid.uuid4())), now=1001) is None  # tampered
    assert read_session(s, token, now=1000 + 13 * 3600) is None  # past 12 hours
    assert read_session(settings(session_secret=""), token, now=1001) is None  # unset secret refuses


# ---------------------------------------------------------------- our own login

def test_sign_in_sets_a_cookie_and_me_works(client):
    add_user("rao", sbus=["India"])
    response = sign_in(client, "Rao ")  # usernames are case- and space-insensitive
    assert response.status_code == 200
    assert response.json() == {"username": "rao", "role": "user", "sbus": ["India"], "via_portal": False}
    assert "httponly" in response.headers["set-cookie"].lower()
    assert client.get("/api/me").json()["username"] == "rao"
    assert client.get("/reports/rasa-incanta/api/me").json()["username"] == "rao"


def test_wrong_password_unknown_user_and_inactive_are_refused(client):
    add_user("rao")
    add_user("gone", active=False)
    assert sign_in(client, "rao", "wrong").status_code == 401
    assert sign_in(client, "nobody").status_code == 401
    assert sign_in(client, "gone").status_code == 401
    assert client.get("/api/me").status_code == 401


def test_portal_only_user_cannot_sign_in_with_a_password(client):
    add_user("portal-only", password=None)
    assert sign_in(client, "portal-only", "").status_code == 401


def test_no_session_secret_refuses_sign_in(client, monkeypatch):
    from api.config import get_settings

    add_user("rao")
    monkeypatch.delenv("SESSION_SECRET")
    get_settings.cache_clear()
    assert sign_in(client).status_code == 503


def test_deactivating_locks_out_an_existing_session(client):
    user_id = add_user("rao")
    sign_in(client)
    with get_sessionmaker()() as s:
        s.get(User, user_id).is_active = False
        s.commit()
    assert client.get("/api/me").status_code == 401


def test_sign_out_clears_the_cookie(client):
    add_user("rao")
    sign_in(client)
    client.post("/api/auth/logout")
    assert client.get("/api/me").status_code == 401


def test_tampered_cookie_is_refused(client):
    add_user("rao")
    sign_in(client)
    client.cookies.set("ri_session", client.cookies.get("ri_session")[:-2] + "xx")
    assert client.get("/api/me").status_code == 401


# ---------------------------------------------------------------- portal identity

def test_portal_request_maps_email_to_our_user(client, portal):
    add_user("sreejit", sbus=["India"], password=None, ms_email=PORTAL_EMAIL)
    response = client.get("/reports/rasa-incanta/api/me", headers=portal_headers(PORTAL_EMAIL.upper()))
    assert response.status_code == 200
    assert response.json()["username"] == "sreejit" and response.json()["via_portal"] is True


def test_portal_key_with_no_identity_header_is_401(client, portal):
    assert client.get("/api/me", headers=portal_headers(email=None)).status_code == 401


def test_portal_key_with_unmapped_email_is_401_even_with_a_session(client, portal):
    add_user("rao")
    sign_in(client)  # a valid session must not rescue a portal request naming nobody
    assert client.get("/api/me", headers=portal_headers("stranger@roibypractus.com")).status_code == 401


def test_portal_key_with_inactive_account_is_401(client, portal):
    add_user("sreejit", ms_email=PORTAL_EMAIL, active=False)
    assert client.get("/api/me", headers=portal_headers()).status_code == 401


def test_header_without_the_key_is_ignored(client, portal):
    add_user("sreejit", ms_email=PORTAL_EMAIL)
    assert client.get("/api/me", headers={"X-CGO-Portal-User-Email": PORTAL_EMAIL}).status_code == 401
    assert client.get("/api/me", headers=portal_headers(key="guess")).status_code == 401


def test_header_without_the_key_falls_to_our_normal_login(client, portal):
    add_user("rao")
    add_user("sreejit", ms_email=PORTAL_EMAIL)
    sign_in(client)
    response = client.get("/api/me", headers={"X-CGO-Portal-User-Email": PORTAL_EMAIL})
    assert response.json()["username"] == "rao"  # the session user, not the header


def test_portal_is_off_by_default(client, monkeypatch):
    from api.config import get_settings

    monkeypatch.setenv("CGO_REPORTS_API_KEY", TEST_PORTAL_KEY)
    get_settings.cache_clear()
    add_user("sreejit", ms_email=PORTAL_EMAIL)
    assert client.get("/api/me", headers=portal_headers()).status_code == 401


def test_enabled_but_unset_key_refuses_everything(client, monkeypatch):
    from api.config import get_settings

    monkeypatch.setenv("PORTAL_IDENTITY_ENABLED", "true")
    get_settings.cache_clear()
    add_user("sreejit", ms_email=PORTAL_EMAIL)
    assert client.get("/api/me", headers=portal_headers(key="")).status_code == 401
    assert client.get("/api/me", headers={"Authorization": "Bearer ", "X-CGO-Portal-User-Email": PORTAL_EMAIL}).status_code == 401


def test_portal_key_is_never_read_from_the_query_string(client, portal):
    add_user("sreejit", ms_email=PORTAL_EMAIL)
    response = client.get(f"/api/me?token={TEST_PORTAL_KEY}", headers={"X-CGO-Portal-User-Email": PORTAL_EMAIL})
    assert response.status_code == 401


def test_non_ascii_bearer_is_401_not_500(client, portal):
    from tests.test_web import raw_get

    status, _ = raw_get("/api/me", [(b"authorization", b"Bearer \xff\xfe-not-ascii"),
                                    (b"x-cgo-portal-user-email", PORTAL_EMAIL.encode())])
    assert status == 401


def test_portal_email_is_matched_ignoring_case(client, portal):
    add_user("sreejit", ms_email="Sreejit.Nair@RoiByPractus.com")
    assert client.get("/api/me", headers=portal_headers(PORTAL_EMAIL)).json()["username"] == "sreejit"


# ---------------------------------------------------------------- schema rules

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


def test_portal_only_user_has_an_unusable_password(migrated):
    with get_sessionmaker()() as s:
        user = User(username="portal")
        s.add(user)
        s.commit()
        assert user.password_hash == "!" and not verify_password("", user.password_hash)


# ---------------------------------------------------------------- protect by default, boot rules

def test_a_route_with_no_auth_of_its_own_is_still_protected(client):
    from api.main import fastapi_app

    @fastapi_app.get("/api/added-later")
    def added_later() -> dict:
        return {"secret": "data"}

    fastapi_app.router.routes.insert(0, fastapi_app.router.routes.pop())  # ahead of the catch-alls
    try:
        assert client.get("/api/added-later").status_code == 401
        assert client.get("/reports/rasa-incanta/api/added-later").status_code == 401
        add_user("rao")
        sign_in(client)
        assert client.get("/reports/rasa-incanta/api/added-later").json() == {"secret": "data"}
    finally:
        fastapi_app.router.routes.pop(0)


@pytest.mark.parametrize("env, problem", [
    ({"SESSION_SECRET": None}, "SESSION_SECRET"),
    ({"PORTAL_IDENTITY_ENABLED": "true"}, "CGO_REPORTS_API_KEY is not set"),
    ({"CGO_REPORTS_API_KEY": "same-value", "SESSION_SECRET": "same-value"}, "must not equal"),
])
def test_refuses_to_boot_when_misconfigured(migrated, monkeypatch, env, problem):
    from fastapi.testclient import TestClient

    from api.config import get_settings
    from api.main import app

    for name, value in env.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match=problem):
        with TestClient(app):
            pass


# ---------------------------------------------------------------- scoping and board

def test_board_needs_a_user(client):
    assert client.get("/api/week").status_code == 401


def test_empty_access_sees_nothing(client):
    seed_deals()
    add_user("rao", sbus=[])
    sign_in(client)
    week = client.get("/api/week").json()
    assert [b["count"] for b in week["boards"]] == [0, 0, 0]


def test_board_shows_only_allowed_sbus(client):
    seed_deals()  # Northwind is India / Proposal Sent, Blue Harbour is MEA / Qualified Prospect
    add_user("rao", sbus=["India"])
    sign_in(client)
    week = client.get("/reports/rasa-incanta/api/week").json()
    assert week["week_start"] <= time.strftime("%Y-%m-%d")
    pipeline = week["boards"][0]
    assert pipeline["label"] == "Pipeline" and pipeline["count"] == 1
    deal = pipeline["stages"][0]["deals"][0]
    assert deal["name"] == "Northwind Foods - Working capital"


def test_two_restricted_users_see_different_deals(client):
    seed_deals()
    add_user("india", sbus=["India"])
    add_user("mea", sbus=["MEA"])
    add_user("both", sbus=["India", "MEA"])

    def names(user):
        client.post("/api/auth/logout")
        sign_in(client, user)
        return {d["name"].split(" - ")[0] for b in client.get("/api/week").json()["boards"]
                for st in b["stages"] for d in st["deals"]}

    assert names("both") == {"Northwind Foods", "Blue Harbour Retail"}
    assert names("india") == {"Northwind Foods"}
    assert names("mea") == {"Blue Harbour Retail"}


def test_deal_detail_has_the_bar_and_the_action(client):
    seed_deals()
    add_user("rao", sbus=["India", "MEA"])
    sign_in(client)
    boards = client.get("/api/week").json()["boards"]
    northwind = next(d for st in boards[0]["stages"] for d in st["deals"] if d["name"].startswith("Northwind"))
    detail = client.get(f"/api/deals/{northwind['id']}").json()
    assert detail["contact_name"] == "Priya Shah" and detail["days_in_stage"] is not None
    segments = {s["key"]: s for s in detail["segments"]}
    assert segments["deal_state"]["present"] and segments["contact"]["present"]
    assert not segments["conversation"]["present"]
    assert segments["conversation"]["reason"] == "No call logged. No mail found."
    action = detail["actions"][0]
    assert action["objective"] == "advance" and action["evidence"][0]["source_label"] == "Zoho"


def test_deal_outside_your_sbus_is_a_404(client):
    seed_deals()
    add_user("admin", sbus=["India", "MEA"], role="admin")
    add_user("rao", sbus=["India"])
    sign_in(client, "admin")
    blue = next(d for b in client.get("/api/week").json()["boards"] for st in b["stages"] for d in st["deals"]
                if d["name"].startswith("Blue Harbour"))
    client.post("/api/auth/logout")
    sign_in(client, "rao")
    assert client.get(f"/api/deals/{blue['id']}").status_code == 404

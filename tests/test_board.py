"""The board is open: anyone reaches it at either root, with no sign-in and no gating."""
import pytest

from api.db import get_sessionmaker
from api.engine.run import run_week
from tests.conftest import TEST_PORTAL_KEY
from tests.fakes import NOW, FakeClaude, FakeConnection, claude_response, load, settings
from tests.test_run import zoho_fetch

PREFIX = "/reports/rasa-incanta"


def seed_deals(rows=None):
    conn = FakeConnection(deals=rows) if rows is not None else None
    with get_sessionmaker()() as s:
        run_week(s, settings(), now=NOW, fetch=zoho_fetch(conn),
                 llm_client=FakeClaude(claude_response(load("claude/nba_reply.json"))))


def deal_names(client, base=""):
    return {d["name"].split(" - ")[0] for b in client.get(base + "/api/week").json()["boards"]
            for st in b["stages"] for d in st["deals"]}


@pytest.mark.parametrize("base", ["", PREFIX])
def test_board_is_open_at_both_roots(client, base):
    seed_deals()
    response = client.get(base + "/api/week")
    assert response.status_code == 200
    pipeline = response.json()["boards"][0]
    assert pipeline["label"] == "Pipeline" and pipeline["count"] == 2
    assert deal_names(client, base) == {"Northwind Foods", "Blue Harbour Retail"}  # every SBU


def test_deals_with_no_sbu_are_shown_too(client):
    rows = [dict(r, sbu=None) for r in load("zoho/deals.json")]
    seed_deals(rows)
    assert deal_names(client) == {"Northwind Foods", "Blue Harbour Retail"}


def test_deal_detail_is_open_and_has_the_bar_and_the_action(client):
    seed_deals()
    boards = client.get("/api/week").json()["boards"]
    northwind = next(d for st in boards[0]["stages"] for d in st["deals"] if d["name"].startswith("Northwind"))
    detail = client.get(f"{PREFIX}/api/deals/{northwind['id']}").json()
    assert detail["contact_name"] == "Priya Shah" and detail["days_in_stage"] is not None
    segments = {s["key"]: s for s in detail["segments"]}
    assert segments["deal_state"]["present"] and segments["contact"]["present"]
    assert not segments["conversation"]["present"]
    assert segments["conversation"]["reason"] == "No call logged. No mail found."
    action = detail["actions"][0]
    assert action["objective"] == "advance" and action["evidence"][0]["source_label"] == "Zoho"


def test_unknown_deal_is_a_404(client):
    assert client.get("/api/deals/00000000-0000-0000-0000-000000000000").status_code == 404


@pytest.mark.parametrize("path", ["/api/me", "/api/auth/login", "/api/auth/logout"])
def test_sign_in_endpoints_are_gone(client, path):
    response = client.post(path, json={"username": "x", "password": "y"})
    assert response.status_code == 404 and response.headers["content-type"].startswith("application/json")


def test_portal_headers_do_not_gate_the_board(client, monkeypatch):
    from api.config import get_settings

    seed_deals()
    monkeypatch.setenv("PORTAL_IDENTITY_ENABLED", "true")
    monkeypatch.setenv("CGO_REPORTS_API_KEY", TEST_PORTAL_KEY)
    get_settings.cache_clear()
    headers = {"Authorization": f"Bearer {TEST_PORTAL_KEY}", "X-CGO-Portal-User-Email": "nobody@roibypractus.com"}
    assert client.get(PREFIX + "/api/week", headers=headers).status_code == 200


def test_starts_with_no_secrets_set(migrated, monkeypatch):
    from fastapi.testclient import TestClient

    from api.config import get_settings
    from api.main import app

    for name in ("SESSION_SECRET", "CGO_REPORTS_API_KEY", "PORTAL_IDENTITY_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    with TestClient(app) as c:
        assert c.get("/health").json()["status"] == "ok"
        assert c.get("/api/week").status_code == 200

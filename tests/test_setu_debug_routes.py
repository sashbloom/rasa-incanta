"""GET /api/setu/case-studies and /api/setu/enriched: raw and enriched Setu case studies, gated
behind ?key=<SESSION_SECRET> (require_debug_key) — a wrong or missing key looks like no route at
all, not like a locked door."""
from unittest.mock import patch

import pytest

from api.icp import setu_embeddings
from api.models import SetuCaseStudyContext

SECRET = "test-debug-secret"


@pytest.fixture()
def debug_key(monkeypatch):
    """SESSION_SECRET set to SECRET for this test; every other test sees it unset (conftest.py)."""
    from api.config import get_settings

    monkeypatch.setenv("SESSION_SECRET", SECRET)
    get_settings.cache_clear()
    yield SECRET
    get_settings.cache_clear()


def row(name, content="", industry="Food Processing", service_line="SFT"):
    return {"entity_name": name, "content": content, "industry": industry, "service_line": service_line}


# ---------------------------------------------------------------- the gate itself

@pytest.mark.parametrize("path", ["/api/setu/case-studies", "/api/setu/enriched"])
def test_no_key_is_a_404_not_a_401(client, debug_key, path):
    """A wrong or missing key must be indistinguishable from the route not existing at all —
    never a 401/403 that confirms something is there to unlock."""
    response = client.get(path)
    assert response.status_code == 404


@pytest.mark.parametrize("path", ["/api/setu/case-studies", "/api/setu/enriched"])
def test_the_wrong_key_is_a_404(client, debug_key, path):
    assert client.get(path, params={"key": "not-the-secret"}).status_code == 404


@pytest.mark.parametrize("path", ["/api/setu/case-studies", "/api/setu/enriched"])
def test_an_unset_secret_refuses_every_key_not_just_a_wrong_one(client, path):
    """SESSION_SECRET unset (the default while the board is open) must not become a wildcard --
    an empty key against an empty secret must still 404, matching CLAUDE.md's fail-closed rule."""
    assert client.get(path, params={"key": ""}).status_code == 404
    assert client.get(path, params={"key": "anything"}).status_code == 404


def test_the_right_key_gets_through(client, debug_key):
    with patch("api.icp.setu_db.fetch_case_studies", return_value=[]):
        response = client.get("/api/setu/case-studies", params={"key": SECRET})
    assert response.status_code == 200 and response.json() == []


# ---------------------------------------------------------------- the payloads, once past the gate

def test_case_studies_returns_the_raw_setu_fields(client, debug_key):
    rows = [row("Patisserie & Bakes", content="A real, detailed write-up well over fifty characters long.")]
    with patch("api.icp.setu_db.fetch_case_studies", return_value=rows):
        response = client.get("/api/setu/case-studies", params={"key": debug_key})

    assert response.status_code == 200
    [cs] = response.json()
    assert cs == {"entity_name": "Patisserie & Bakes", "content": rows[0]["content"],
                 "industry": "Food Processing", "service_line": "SFT"}


def test_case_studies_returns_503_when_setu_is_unreachable(client, debug_key):
    with patch("api.icp.setu_db.fetch_case_studies", side_effect=RuntimeError("Setu Postgres is not configured")):
        response = client.get("/api/setu/case-studies", params={"key": debug_key})

    assert response.status_code == 503 and "Setu" in response.json()["detail"]


def test_enriched_labels_real_content_enriched_and_pending(client, debug_key):
    from api.db import get_sessionmaker

    real = row("Patisserie & Bakes", content="A real, detailed write-up well over fifty characters long.")
    thin_done = row("Thin Co", content="")
    thin_pending = row("Pending Co", content="short")

    context_hash = setu_embeddings.source_hash(thin_done["entity_name"], thin_done["industry"],
                                               thin_done["service_line"], thin_done["content"])
    with get_sessionmaker()() as s:
        s.add(SetuCaseStudyContext(source_hash=context_hash, case_study_name="Thin Co",
                                   generated_context="A generated context for Thin Co.",
                                   model="claude-haiku-4-5-20251001"))
        s.commit()

    with patch("api.icp.setu_db.fetch_case_studies", return_value=[real, thin_done, thin_pending]):
        response = client.get("/api/setu/enriched", params={"key": debug_key})

    assert response.status_code == 200
    by_name = {cs["entity_name"]: cs for cs in response.json()}
    assert by_name["Patisserie & Bakes"]["status"] == "real_content"
    assert by_name["Patisserie & Bakes"]["generated_context"] is None
    assert by_name["Thin Co"]["status"] == "enriched"
    assert by_name["Thin Co"]["generated_context"] == "A generated context for Thin Co."
    assert by_name["Pending Co"]["status"] == "pending"
    assert by_name["Pending Co"]["generated_context"] is None


def test_enriched_returns_503_when_setu_is_unreachable(client, debug_key):
    with patch("api.icp.setu_db.fetch_case_studies", side_effect=RuntimeError("down")):
        response = client.get("/api/setu/enriched", params={"key": debug_key})

    assert response.status_code == 503

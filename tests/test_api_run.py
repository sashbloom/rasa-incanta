"""POST /api/run, GET /api/run and GET /api/deals, driven with recorded fixtures."""
import pytest

from api.db import get_sessionmaker
from api.models import Run
from api.sources.zoho import fetch_deals
from tests.fakes import NOW, FakeClaude, FakeConnection, claude_response, load, zoho_connect

PREFIX = "/reports/rasa-incanta"


def reply():
    # Cites only the stage, which every deal's card has, so it is valid for both fixture deals.
    return claude_response(dict(load("claude/nba_reply.json"), evidence_ids=["zoho.stage"]))


@pytest.fixture()
def sources():
    """Point runs at the recorded Zoho rows and a fake Claude; returns the dict so tests can swap parts."""
    from api.main import fastapi_app, run_sources

    chosen = {"fetch": lambda s: fetch_deals(s, connect_fn=zoho_connect(FakeConnection())),
              "llm_client": FakeClaude(reply(), reply())}
    fastapi_app.dependency_overrides[run_sources] = lambda: chosen
    yield chosen
    fastapi_app.dependency_overrides.pop(run_sources, None)


@pytest.fixture(autouse=True)
def zoho_configured(monkeypatch):
    from api.config import get_settings

    for name, value in {"ZOHO_PG_HOST": "zoho.example", "ZOHO_PG_USER": "ri_reader", "ZOHO_PG_PASSWORD": "x"}.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()


@pytest.mark.parametrize("base", ["", PREFIX])
def test_post_run_starts_a_full_run_and_get_run_reports_it(client, sources, base):
    response = client.post(base + "/api/run")
    assert response.status_code == 202
    started = response.json()
    assert started["status"] == "running" and started["kind"] == "manual"

    # TestClient runs the background task before returning, so the run has finished by now.
    run = client.get(base + "/api/run").json()
    assert run["id"] == started["id"]
    assert run["status"] == "succeeded", run
    assert run["stats"]["deals"] == 2
    assert run["stats"]["nba_to_draft"] == 2 and run["stats"]["nba_created"] == 2  # every deal, not one
    assert run["finished_at"] is not None and run["error"] is None
    assert run["started_at"].endswith("+00:00") and run["finished_at"].endswith("+00:00")  # always UTC


def test_get_run_before_any_run_is_a_404(client):
    assert client.get("/api/run").status_code == 404


def test_a_second_run_while_one_is_going_is_refused(client, sources):
    from api.main import _run_lock

    _run_lock.acquire()
    try:
        response = client.post("/api/run")
        assert response.status_code == 409 and response.json()["detail"] == "A run is already in progress."
    finally:
        _run_lock.release()
    assert client.post("/api/run").status_code == 202  # free again afterwards


def test_zoho_failure_is_reported_as_a_failed_run(client, sources):
    def down(_settings):
        return fetch_deals(_settings, connect_fn=lambda s: (_ for _ in ()).throw(OSError("host zoho.example refused")))

    sources["fetch"] = down
    client.post("/api/run")
    run = client.get("/api/run").json()
    assert run["status"] == "failed" and "Zoho Postgres read failed (OSError)" in run["error"]
    assert "zoho.example" not in run["error"]


def test_a_crash_marks_the_run_failed_and_frees_the_lock(client, sources):
    def boom(_settings):
        raise RuntimeError("secret detail from deep inside")

    sources["fetch"] = boom
    client.post("/api/run")
    run = client.get("/api/run").json()
    assert run["status"] == "failed"
    assert "RuntimeError" in run["error"] and "secret detail" not in run["error"]
    sources["fetch"] = lambda s: fetch_deals(s, connect_fn=zoho_connect(FakeConnection()))
    assert client.post("/api/run").status_code == 202


def test_no_api_key_still_builds_cards_and_says_why_there_are_no_nbas(client, sources):
    sources["llm_client"] = None  # real default: no ANTHROPIC_API_KEY in tests
    client.post("/api/run")
    run = client.get("/api/run").json()
    assert run["status"] == "partial" and run["stats"]["nba_created"] == 0
    assert run["stats"]["nba_skipped"][0]["reason"] == "ANTHROPIC_API_KEY is not set."


def test_runs_left_running_by_a_restart_are_marked_failed(migrated):
    from fastapi.testclient import TestClient

    from api.main import app

    with get_sessionmaker()() as s:
        s.add(Run(week_start=NOW.date(), status="running", started_at=NOW, stats={}))
        s.commit()
    with TestClient(app) as c:
        run = c.get("/api/run").json()
    assert run["status"] == "failed" and run["error"] == "Interrupted by a restart."


# ---------------------------------------------------------------- GET /api/deals

def test_deals_is_empty_before_any_run(client):
    assert client.get("/api/deals").json() == []


@pytest.mark.parametrize("base", ["", PREFIX])
def test_deals_lists_every_deal_with_its_latest_nbas(client, sources, base):
    client.post("/api/run")
    deals = {d["name"]: d for d in client.get(base + "/api/deals").json()}
    assert set(deals) == {"Northwind Foods - Working capital", "Blue Harbour Retail - Cost audit"}
    northwind = deals["Northwind Foods - Working capital"]
    assert northwind["zoho_id"] == "598723000011234001" and northwind["sbu"] == "India" and northwind["is_active"]
    [action] = northwind["actions"]
    assert action["objective"] == "advance" and action["evidence"][0]["fact_id"] == "zoho.stage"
    assert northwind["actions_week"] == client.get("/api/run").json()["week_start"]


def test_deals_shows_only_the_latest_runs_nbas(client, sources):
    client.post("/api/run")
    second = dict(load("claude/nba_reply.json"), objective="nurture", evidence_ids=["zoho.stage"])
    sources["llm_client"] = FakeClaude(claude_response(second), claude_response(second))
    client.post("/api/run")
    for deal in client.get("/api/deals").json():
        assert [a["objective"] for a in deal["actions"]] == ["nurture"]  # the earlier run's NBA is history


def test_deals_includes_deals_that_left_the_pipeline(client, sources):
    client.post("/api/run")
    only_northwind = load("zoho/deals.json")[:1]
    sources["fetch"] = lambda s: fetch_deals(s, connect_fn=zoho_connect(FakeConnection(deals=only_northwind)))
    sources["llm_client"] = FakeClaude(reply())
    client.post("/api/run")
    blue = next(d for d in client.get("/api/deals").json() if d["name"].startswith("Blue Harbour"))
    assert blue["is_active"] is False and blue["actions"]  # still listed, with its last NBA

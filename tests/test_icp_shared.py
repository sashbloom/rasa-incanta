"""The shared ICP database: reuse a fresh score, publish ours, fall back quietly when it is down.
psycopg.connect is replaced by a scripted connection; nothing leaves the machine."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from api.db import get_sessionmaker
from api.engine import icp_signal
from api.engine.run import run_week
from api.models import CompanyIcp, ContextCard
from api.sources import icp_shared
from api.sources.outlook import MailResult
from api.sources.zoho import fetch_deals
from tests.fakes import NOW, FakeClaude, FakeConnection, claude_response, load, settings, zoho_connect

URL = "postgresql://shared.example/icp"
KEY = icp_signal.company_key("Northwind Foods")
FACTS = {"status": "scored", "facts": [{"id": "icp.recommendation", "source": "icp", "label": "Recommendation",
                                        "value": "ICP recommendation: Pursue", "date": "2026-09-20"}]}


class SharedDb:
    """Stands in for psycopg.connect(): answers SELECT with `rows`, records INSERTs."""

    def __init__(self, rows=(), error=None):
        self.rows, self.error, self.inserted, self.statements, self.params = list(rows), error, [], [], []

    def connect(self, *args, **kwargs):
        if self.error:
            raise self.error
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return self

    def commit(self):
        pass

    def execute(self, sql, params=None):
        self.statements.append(sql)
        self.params.append(params)
        if sql.lstrip().startswith("INSERT"):
            self.inserted.append(params)

    def fetchall(self):
        return self.rows


def row(days_old=1, exa=True, status="scored"):
    return {"company_name": "Northwind Foods", "computed_at": NOW - timedelta(days=days_old), "status": status,
            "account_fit": FACTS, "stakeholder": {}, "result": {"exa_research": exa}, "data_gaps": []}


@pytest.fixture()
def session(migrated):
    with get_sessionmaker()() as s:
        yield s


def shared_settings(**kw):
    return settings(icp_shared_db_url=URL, **kw)


def zoho():
    return lambda s: fetch_deals(s, connect_fn=zoho_connect(FakeConnection()))


def llm():
    return FakeClaude(*[claude_response(load("claude/nba_reply.json")) for _ in range(4)])


def test_a_fresh_shared_score_is_found(monkeypatch):
    monkeypatch.setattr(icp_shared.psycopg, "connect", SharedDb([row()]).connect)
    found = icp_shared.find(shared_settings(), KEY, NOW, 28)
    assert found.ok and found.score.account_fit == FACTS


def test_a_score_made_without_exa_is_skipped_when_exa_is_available(monkeypatch):
    monkeypatch.setattr(icp_shared.psycopg, "connect", SharedDb([row(exa=False)]).connect)
    assert icp_shared.find(shared_settings(), KEY, NOW, 28, require_exa=True).score is None
    assert icp_shared.find(shared_settings(), KEY, NOW, 28, require_exa=False).score is not None


def test_the_query_only_asks_for_scores_inside_the_window(monkeypatch):
    db = SharedDb()
    monkeypatch.setattr(icp_shared.psycopg, "connect", db.connect)
    icp_shared.find(shared_settings(), KEY, NOW, 28)
    asked = db.params[0]
    assert asked["since"] == NOW - timedelta(days=28) and "failed" not in asked["statuses"]


def test_an_unreachable_shared_db_is_not_an_exception(monkeypatch):
    monkeypatch.setattr(icp_shared.psycopg, "connect", SharedDb(error=OSError("down")).connect)
    found = icp_shared.find(shared_settings(), KEY, NOW, 28)
    assert not found.ok and found.score is None
    score = icp_shared.SharedScore("Northwind Foods", NOW, "scored", {}, {}, {}, [])
    assert icp_shared.publish(shared_settings(), KEY, score) == "OSError"


def test_unset_means_not_configured():
    assert not icp_shared.configured(settings())
    assert not icp_shared.find(settings(), KEY, NOW, 28).ok


def test_only_usable_scores_are_published_and_only_by_insert(monkeypatch):
    db = SharedDb()
    monkeypatch.setattr(icp_shared.psycopg, "connect", db.connect)
    failed = icp_shared.SharedScore("Northwind Foods", NOW, "failed", {}, {}, {}, [])
    assert icp_shared.publish(shared_settings(), KEY, failed) is None and not db.inserted
    good = icp_shared.SharedScore("Northwind Foods", NOW, "scored", FACTS, {}, {"exa_research": True}, [])
    assert icp_shared.publish(shared_settings(), KEY, good) is None
    assert db.inserted[0]["agent"] == "rasa-incanta" and db.inserted[0]["key"] == KEY
    assert not any(s.lstrip().upper().startswith(("UPDATE", "DELETE")) for s in db.statements)


def run(session, monkeypatch, db, **kw):
    monkeypatch.setattr(icp_shared.psycopg, "connect", db.connect)
    return run_week(session, shared_settings(), now=NOW, fetch=zoho(), llm_client=llm(), **kw)


def test_a_run_uses_a_shared_score_instead_of_scoring(session, monkeypatch):
    def never(*a, **k):
        raise AssertionError("scored a company that had a fresh shared score")

    finished = run(session, monkeypatch, SharedDb([row()]), score_company_fn=never)
    assert finished.stats["icp_shared_reused"] >= 1
    assert finished.stats["sources"]["icp_shared"].startswith("ok")
    local = session.scalars(select(CompanyIcp).where(CompanyIcp.company_key == KEY)).all()
    assert len(local) == 1 and local[0].account_fit == FACTS  # kept in our own history
    assert any(c.account_fit == FACTS for c in session.scalars(select(ContextCard)))


def test_a_run_falls_back_to_its_own_scoring_when_the_shared_db_is_down(session, monkeypatch):
    called = []

    def score(company, generated_at=None):
        called.append(company)
        raise RuntimeError("no scoring in tests")

    finished = run(session, monkeypatch, SharedDb(error=OSError("down")), score_company_fn=score)
    assert finished.status in ("succeeded", "partial")  # the run carries on
    assert any(c.startswith("Northwind Foods") for c in called)
    assert "unreachable" in finished.stats["sources"]["icp_shared"]


# ---------------------------------------------------------------- a dead Outlook token

def test_a_dead_outlook_token_flags_no_mail_and_the_run_continues(session):
    ms = settings(ms_tenant_id="t", ms_client_id="c", ms_client_secret="s", myrah_mailbox="m@x.example")
    finished = run_week(session, ms, now=NOW, nba_limit=None, fetch=zoho(), llm_client=llm(),
                        fetch_mail=lambda *a, **k: MailResult(error="invalid_grant: refresh token expired"))
    assert finished.status in ("succeeded", "partial")
    assert finished.stats["sources"]["outlook"].startswith("invalid_grant")
    assert all("no_mail" in c.gaps for c in session.scalars(select(ContextCard)))
    assert finished.stats["nba_created"] >= 1


def test_a_fetch_that_raises_still_does_not_stop_the_run(session):
    def boom(*a, **k):
        raise RuntimeError("Graph exploded")

    finished = run_week(session, settings(), now=NOW, fetch=zoho(), fetch_mail=boom, llm_client=llm())
    assert finished.status in ("succeeded", "partial")
    assert "RuntimeError" in finished.stats["sources"]["outlook"]


def test_the_run_records_when_each_phase_started_and_ended(session):
    finished = run_week(session, settings(), now=NOW, fetch=zoho(), llm_client=llm())
    phases = finished.stats["phases"]
    assert list(phases)[:2] == ["pull", "mail"]
    assert "finished_at" in phases["pull"] and "finished_at" in phases["nba"]

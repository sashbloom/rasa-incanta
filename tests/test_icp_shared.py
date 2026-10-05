"""The shared ICP database, read only: the newest finished run per company, joined to its criteria.
psycopg.connect is replaced by a scripted connection; nothing leaves the machine."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from api.db import get_sessionmaker
from api.engine import icp_signal
from api.engine.run import run_week
from api.models import ContextCard
from api.sources import icp_shared
from api.sources.outlook import MailResult
from api.sources.zoho import fetch_deals
from tests.fakes import NOW, FakeClaude, FakeConnection, claude_response, load, settings, zoho_connect

URL = "postgresql://shared.example/icp"
KEY = icp_signal.company_key("Northwind Foods")


class SharedDb:
    """Stands in for psycopg.connect(): answers the three SELECTs from scripted rows, records everything."""

    def __init__(self, runs=(), detail=(), gates=(), error=None):
        self.runs, self.detail, self.gates, self.error = list(runs), list(detail), list(gates), error
        self.statements, self.params, self.connect_kwargs = [], [], []
        self.read_only = False
        self._sql = ""

    def connect(self, *args, **kwargs):
        if self.error:
            raise self.error
        self.connect_kwargs.append(kwargs)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return self

    def execute(self, sql, params=None):
        self.statements.append(sql)
        self.params.append(params)
        self._sql = sql

    def fetchall(self):
        if "FROM icp.run_gates" in self._sql:
            return self.gates
        if "run_criterion_scores" in self._sql:
            return self.detail
        return self.runs


def run_row(run_id="r1", name="Northwind Foods Pvt Ltd", days_old=1):
    return {"run_id": run_id, "company_name": name, "generated_at": NOW - timedelta(days=days_old)}


def detail_rows(run_id="r1", name="Northwind Foods Pvt Ltd", **over):
    head = {"run_id": run_id, "company_name": name, "generated_at": NOW - timedelta(days=1), "gate_1_stopped": False,
            "stop_reason": None, "recommendation": "Pursue selectively", "recommendation_reason": "Strong fit.",
            "client_total": 31, "client_verdict": "Moderate", "practus_total": 38, "practus_verdict": "Strong",
            "provisional": False, **over}

    def crit(cid, score, gap=False):
        return {"criterion_id": cid, "group_name": None, "raw_score": score, "is_na": False,
                "condition_label": "some_label", "rationale": f"{cid} rationale", "data_gap": gap}

    return [{**head, **crit("A1", 4)}, {**head, **crit("A2", 3, gap=True)}, {**head, **crit("C1", 4)},
            {**head, **crit("P2", 5)}]


GATE = {"run_id": "r1", "gate_id": "5", "name": "Staleness", "fired": True, "detail": "No stage change in 190 days"}


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


def load_with(monkeypatch, db, companies=None):
    monkeypatch.setattr(icp_shared.psycopg, "connect", db.connect)
    return icp_shared.load_scores(shared_settings(), companies or {KEY: "Northwind Foods"})


def test_a_score_is_found_with_its_full_criteria_breakdown(monkeypatch):
    db = SharedDb([run_row()], detail_rows(), [GATE])
    found = load_with(monkeypatch, db)
    score = found.scores[KEY]  # "Pvt Ltd" in the shared name does not matter
    assert found.ok and score.status == "scored" and (score.client_total, score.practus_total) == (31, 38)
    assert [c["criterion_id"] for c in score.criteria] == ["A1", "A2", "C1", "P2"]
    assert score.gates == [{"gate_id": "5", "name": "Staleness", "detail": "No stage change in 190 days"}]


def test_the_newest_finished_run_of_a_company_wins(monkeypatch):
    db = SharedDb([run_row("new", days_old=1), run_row("old", days_old=40)], detail_rows("new"))
    found = load_with(monkeypatch, db)
    assert found.scores[KEY].run_id == "new" and db.params[1] == {"ids": ["new"]}


def test_a_company_with_no_run_has_no_entry_and_no_error(monkeypatch):
    found = load_with(monkeypatch, SharedDb([run_row(name="Someone Else Ltd")]))
    assert found.ok and found.scores == {}


def test_a_gate_1_stop_is_reported_as_such(monkeypatch):
    rows = detail_rows(gate_1_stopped=True, stop_reason="Company not identified", recommendation=None)[:1]
    found = load_with(monkeypatch, SharedDb([run_row()], rows))
    assert found.scores[KEY].status == "gate_1_stopped" and found.scores[KEY].stop_reason == "Company not identified"


def test_we_only_read_never_touch_files_or_report_text_and_require_ssl(monkeypatch):
    db = SharedDb([run_row()], detail_rows(), [GATE])
    load_with(monkeypatch, db)
    sql = " ".join(db.statements).lower()
    assert db.statements and all(s.lstrip().upper().startswith("SELECT") for s in db.statements)
    for forbidden in ("run_files", "html_content", "markdown_content", "input_data", "insert", "update ", "delete", "create "):
        assert forbidden not in sql
    assert "join icp.run_criterion_scores" in sql
    assert db.connect_kwargs[0]["sslmode"] == "require"
    assert db.read_only is True


def test_an_unreachable_shared_db_is_not_an_exception(monkeypatch):
    found = load_with(monkeypatch, SharedDb(error=OSError("down")))
    assert not found.ok and found.scores == {} and found.error == "OSError"


def test_unset_means_not_configured():
    assert not icp_shared.configured(settings())
    assert not icp_shared.load_scores(settings(), {KEY: "Northwind Foods"}).ok


# ---------------------------------------------------------------- in a run

def run(session, monkeypatch, db, **kw):
    monkeypatch.setattr(icp_shared.psycopg, "connect", db.connect)
    return run_week(session, shared_settings(), now=NOW, fetch=zoho(), llm_client=llm(), **kw)


def test_a_run_reads_the_shared_score_and_never_scores_or_writes(session, monkeypatch):
    db = SharedDb([run_row()], detail_rows(), [GATE])
    finished = run(session, monkeypatch, db)
    assert finished.stats["icp_found"] >= 1 and finished.stats["sources"]["icp"].startswith("ok")
    cards = session.scalars(select(ContextCard)).all()
    fit = next(c.account_fit for c in cards if c.account_fit)
    assert fit["client_total"] == 31 and fit["practus_total"] == 38
    assert all(s.lstrip().upper().startswith("SELECT") for s in db.statements)


def test_a_company_with_no_shared_row_gets_no_icp_and_the_run_continues(session, monkeypatch):
    finished = run(session, monkeypatch, SharedDb())
    assert finished.status in ("succeeded", "partial")
    assert finished.stats["icp_found"] == 0
    assert all("no_icp" in c.gaps and not c.account_fit for c in session.scalars(select(ContextCard)))


def test_a_run_carries_on_when_the_shared_db_is_down(session, monkeypatch):
    finished = run(session, monkeypatch, SharedDb(error=OSError("down")))
    assert finished.status in ("succeeded", "partial")
    assert finished.stats["sources"]["icp"].startswith("unavailable (OSError)")
    assert all("no_icp" in c.gaps for c in session.scalars(select(ContextCard)))


# ---------------------------------------------------------------- a dead Outlook token

def test_a_dead_outlook_token_flags_no_mail_and_the_run_continues(session):
    ms = settings(ms_tenant_id="t", ms_client_id="c", ms_client_secret="s", myrah_mailbox="m@x.example")
    finished = run_week(session, ms, now=NOW, nba_limit=None, fetch=zoho(), llm_client=llm(),
                        fetch_mail=lambda *a, **k: MailResult(error="invalid_grant: refresh token expired"))
    assert finished.status in ("succeeded", "partial")
    assert finished.stats["sources"]["outlook"].startswith("invalid_grant")

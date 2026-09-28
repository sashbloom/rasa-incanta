"""Langfuse tracing on every Claude and Exa call, and prompt caching on the NBA calls. Fakes only."""
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select

from api.engine import nba
from api.engine.nba import generate_nba, system_blocks
from api.icp import exa_search, tracing
from api.models import Recommendation
from tests.fakes import NOW, FakeClaude, claude_response, load, settings
from tests.test_context_and_nba import northwind_card
from tests.test_run import zoho_fetch


def reply(**changes):
    return claude_response(dict(load("claude/nba_reply.json"), evidence_ids=["zoho.stage"], **changes))


# ---------------------------------------------------------------- prompt caching

def test_the_nba_system_prompt_is_one_cached_block_identical_for_every_deal():
    client = FakeClaude(reply(), reply())
    card = northwind_card()
    generate_nba(client, "claude-sonnet-5", "Northwind Foods - Working capital", card)
    generate_nba(client, "claude-sonnet-5", "Blue Harbour - Procurement", card)
    first, second = (c["system"] for c in client.calls)
    assert first == second  # byte-identical prefix, so the second call reads the first one's cache
    assert len(first) == 1 and first[0]["cache_control"] == {"type": "ephemeral"}
    text = first[0]["text"]
    assert "Objectives:" in text and "Rules:" in text  # instructions and compose rules are both in the prefix
    assert "Northwind" not in text and "Blue Harbour" not in text  # nothing per deal before the breakpoint
    assert "Northwind" in client.calls[0]["messages"][0]["content"]  # the card goes after it


def use_treasury(monkeypatch, path):
    monkeypatch.setattr(nba, "TREASURY_FILE", path)
    nba.load_treasury.cache_clear()
    nba.system_blocks.cache_clear()


@pytest.fixture(autouse=True)
def _fresh_treasury_cache():
    yield
    nba.load_treasury.cache_clear()
    nba.system_blocks.cache_clear()


@pytest.fixture()
def treasury(tmp_path, monkeypatch):
    path = tmp_path / "ideas_treasury.md"  # Mahak's format: numbered sections, ideas numbered within each
    path.write_text("# Ideas Treasury\n\n## 3. Community\n\n1. Invite to a closed-door CXO roundtable.\n\n"
                    "## 4. Decision Enablement\n\n1. Offer non-billable dip-sticks.\n2. Offer limited pre-work.\n",
                    encoding="utf-8")
    use_treasury(monkeypatch, path)
    return path


def test_the_real_treasury_is_loaded_with_every_idea_numbered():
    text, ids = nba.load_treasury()
    assert len(ids) == 44 and {"1.1", "3.6", "4.12", "6.4"} <= ids and "7.1" not in ids
    assert "\n4.1 Offer non-billable" in text and "\n6.4 Provide references" in text
    assert len(system_blocks()[0]["text"]) > 4 * 1024  # past Sonnet 5's 1024-token cache minimum (~4 chars a token)


def test_practus_people_from_the_treasury_may_be_named_but_client_names_come_from_the_facts():
    rules = system_blocks()[0]["text"].split("Rules:", 1)[1]
    exception = next(line for line in rules.splitlines() if "Ideas Treasury" in line)
    assert all(name in exception for name in ("Venkat", "Deepak", "Vamesh", "Vivek", "Arun", "Bimal"))
    assert "Client-side people still come only from the facts" in exception
    assert "Only name people who appear in the facts" in rules  # the client-side rule is kept


def test_the_ideas_treasury_rides_in_the_cached_prefix_and_refs_are_checked(treasury):
    text = system_blocks()[0]["text"]
    assert nba.load_treasury()[1] == {"3.1", "4.1", "4.2"}
    assert "4.1 Offer non-billable dip-sticks." in text and "3.1 Invite to a closed-door" in text
    assert text.index("Objectives:") < text.index("4.1 Offer") < text.index("Rules:")
    ok = generate_nba(FakeClaude(reply(treasury_ref="4.1")), "m", "Northwind", northwind_card())
    assert ok.draft.treasury_ref == "4.1"
    made_up = generate_nba(FakeClaude(reply(treasury_ref="9.9")), "m", "Northwind", northwind_card())
    assert made_up.ok and made_up.draft.treasury_ref is None  # an idea that is not in the treasury is dropped


def test_without_a_treasury_every_ref_is_dropped(tmp_path, monkeypatch):
    use_treasury(monkeypatch, tmp_path / "missing.md")
    assert "no Ideas Treasury is loaded" in system_blocks()[0]["text"]
    assert generate_nba(FakeClaude(reply(treasury_ref="4.1")), "m", "N", northwind_card()).draft.treasury_ref is None


class TimedClaude(FakeClaude):
    """Records when each call starts and ends, from whichever thread makes it."""

    def __init__(self, *responses):
        super().__init__(*responses)
        self.spans, self.lock = [], threading.Lock()

    def parse(self, **kwargs):
        start = time.monotonic()
        time.sleep(0.05)
        with self.lock:
            item = super().parse(**kwargs)
            self.spans.append((start, time.monotonic()))
        return item


def test_the_first_draft_runs_alone_so_the_rest_read_its_cache(migrated):
    from api.db import get_sessionmaker
    from api.engine.run import run_week

    client = TimedClaude(reply(), reply())
    with get_sessionmaker()() as s:
        r = run_week(s, settings(), now=NOW, fetch=zoho_fetch(), llm_client=client, nba_limit=None)
        assert r.stats["nba_created"] == 2, r.stats
        (first_start, first_end), (second_start, _) = sorted(client.spans)
        assert second_start >= first_end  # the cache is written before anyone else reads it
        assert s.scalar(select(Recommendation.treasury_ref)) is None


# ---------------------------------------------------------------- Langfuse

@pytest.fixture()
def langfuse(monkeypatch):
    """Tracing on, with a mock Langfuse client; returns the mock root span (every trace uses it)."""
    monkeypatch.setattr(tracing, "TRACING_ENABLED", True)
    client = MagicMock()
    with patch("langfuse.get_client", return_value=client):
        yield client


def traces(client, prefix):
    return [c.kwargs for c in client.start_span.return_value.update_trace.call_args_list
            if str(c.kwargs.get("name", "")).startswith(prefix)]


def test_every_nba_call_in_a_run_is_traced_under_the_runs_session(migrated, langfuse):
    from api.db import get_sessionmaker
    from api.engine.run import run_week

    with get_sessionmaker()() as s:
        r = run_week(s, settings(), now=NOW, fetch=zoho_fetch(), llm_client=FakeClaude(reply(), reply()), nba_limit=None)
    nba_traces = traces(langfuse, "nba:")
    assert len(nba_traces) == 2  # one warm-up call on this thread, one from the pool
    assert {t["session_id"] for t in nba_traces} == {f"rasa-incanta:run:{r.id}"}  # carried into the worker thread
    assert all(t["tags"][:2] == ["rasa-incanta", "nba"] for t in nba_traces)
    langfuse.flush.assert_called()  # buffered traces are sent when the run ends


def test_mail_key_points_are_traced(langfuse):
    from api.engine.mail import KeyPoints, key_points
    from tests.test_signals import mail

    usage = SimpleNamespace(input_tokens=120, output_tokens=30)
    client = FakeClaude(SimpleNamespace(parsed_output=KeyPoints(mails=[]), stop_reason="end_turn", content=[], usage=usage))
    key_points(client, "claude-haiku-4-5-20251001", "Northwind", [mail(1)])
    [trace] = traces(langfuse, "mail key points")
    assert trace["tags"] == ["rasa-incanta", "mail"]
    generation = langfuse.start_span.return_value.start_generation.return_value
    assert generation.update.call_args.kwargs["usage_details"] == {"input": 120, "output": 30}


def test_every_exa_call_is_traced_with_its_cost(monkeypatch, langfuse):
    from api.icp.compat import IcpSettings

    monkeypatch.setattr(exa_search, "get_settings", lambda: IcpSettings(exa_api_key="exa-secret-123"))
    body = {"results": [{"url": "https://example.com/a"}], "costDollars": {"total": 0.012}}
    response = SimpleNamespace(status_code=200, json=lambda: body, text="")
    with patch.object(exa_search.httpx, "post", return_value=response):
        exa_search._post({"query": "Northwind Foods CFO", "type": "auto", "numResults": 5}, timeout=5)
    [trace] = traces(langfuse, "exa.search")
    assert trace["tags"] == ["rasa-incanta", "exa"]
    generation = langfuse.start_span.return_value.start_generation.return_value
    assert generation.update.call_args.kwargs["cost_details"] == {"total": 0.012}
    assert "exa-secret-123" not in str(langfuse.mock_calls)  # the key is a header, never traced


def test_a_failed_exa_call_still_closes_its_span(monkeypatch, langfuse):
    from api.icp.compat import IcpSettings

    monkeypatch.setattr(exa_search, "get_settings", lambda: IcpSettings(exa_api_key="k"))
    with patch.object(exa_search.httpx, "post", return_value=SimpleNamespace(status_code=500, text="down")):
        with pytest.raises(exa_search.ExaError):
            exa_search._post({"query": "x", "type": "auto"}, timeout=5)
    langfuse.start_span.return_value.end.assert_called()


def test_the_langfuse_client_is_built_from_our_settings_including_the_host(monkeypatch):
    from api.icp.compat import IcpSettings

    monkeypatch.setattr(tracing, "_settings", IcpSettings(langfuse_public_key="pk", langfuse_secret_key="sk",
                                                          langfuse_baseurl="https://langfuse.practus.example"))
    tracing._init_client.cache_clear()
    try:
        with patch("langfuse.Langfuse") as built, patch("langfuse.get_client"):
            tracing._client()
            tracing._client()
        built.assert_called_once_with(public_key="pk", secret_key="sk", host="https://langfuse.practus.example")
    finally:
        tracing._init_client.cache_clear()


def test_tracing_is_a_silent_no_op_without_keys():
    assert tracing.TRACING_ENABLED is False  # the test environment has no Langfuse keys
    with patch("langfuse.get_client") as get_client:
        with tracing.observe_search("exa.search", input={}) as finish:
            finish(output={}, cost=0.01)
        tracing.flush()
    get_client.assert_not_called()

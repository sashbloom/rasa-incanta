from datetime import date

import anthropic
import httpx

from api.domain.nba_rules import check_nba
from api.domain.weeks import week_start
from api.engine.context import build_card
from api.engine.nba import generate_nba
from api.sources.zoho import row_to_deal
from tests.fakes import NOW, FakeClaude, claude_response, load

TODAY = date(2026, 9, 24)


def northwind_card():
    from api.sources.zoho import row_to_reachout

    reachouts = [row_to_reachout(r) for r in load("zoho/reachouts.json") if r["deal_id"] == 598723000011234001]
    return build_card(row_to_deal(load("zoho/deals.json")[0]), TODAY, reachouts=reachouts)


def test_week_starts_on_monday_in_kolkata():
    assert week_start(NOW, "Asia/Kolkata") == date(2026, 9, 21)


def test_card_carries_sourced_facts_and_brick_three_gaps():
    card = northwind_card()
    ids = {f["id"] for f in card.facts()}
    assert {"zoho.stage", "zoho.reachout_1", "zoho.account", "zoho.proposal_sent", "zoho.problem_statement_1"} <= ids
    assert "zoho.contact" not in ids
    assert all(f["source"] == "zoho" for f in card.facts())
    assert card.deal_state["days_in_stage"] == 34
    assert card.deal_state["board"] == "pipeline"
    assert card.deal_state["last_touch"] == "2026-09-09"
    assert set(card.gaps) == {"no_icp", "no_contact", "no_mail", "no_setu_match"}  # the Teams meeting clears no_call_logged


def test_card_days_are_kolkata_days_not_utc_days():
    # 20:00 UTC on 8 Sep is 01:30 on 9 Sep in Kolkata. A UTC .date() would say 8 Sep.
    row = dict(load("zoho/deals.json")[0], modified_time="2026-09-08T20:00:00+00:00")
    assert build_card(row_to_deal(row), TODAY).deal_state["last_touch"] == "2026-09-09"
    assert build_card(row_to_deal(row), TODAY, "UTC").deal_state["last_touch"] == "2026-09-08"


def test_card_flags_a_missing_contact():
    card = build_card(row_to_deal(load("zoho/deals.json")[1]), TODAY)
    assert "no_contact" in card.gaps
    assert card.stakeholder == {}


def test_nba_rules():
    known = {"zoho.stage"}
    assert check_nba("advance", "Send the plan.", "Proposal is 34 days old.", ["zoho.stage"], known) == []
    assert check_nba("advance", "Send the plan.", "Why.", [], known)  # no evidence
    assert check_nba("advance", "Send the plan.", "Why.", ["zoho.call"], known)  # made-up fact
    assert check_nba("close", "Send the plan.", "Why.", ["zoho.stage"], known)  # unknown objective
    assert check_nba("advance", "word " * 61, "Why.", ["zoho.stage"], known)  # too long
    assert check_nba("advance", "Send.", "Line one\nline two", ["zoho.stage"], known)  # two lines


def test_generates_an_nba_from_the_recorded_reply():
    client = FakeClaude(claude_response(load("claude/nba_reply.json")))
    result = generate_nba(client, "claude-sonnet-5", "Northwind Foods - Working capital", northwind_card())
    assert result.ok
    assert result.draft.objective == "advance"
    assert [e["fact_id"] for e in result.evidence] == [
        "zoho.stage", "zoho.proposal_sent", "zoho.problem_statement_1", "zoho.reachout_1",
    ]
    assert result.evidence[0]["date"] == "2026-08-21"

    request = client.calls[0]
    assert request["model"] == "claude-sonnet-5"
    assert request["thinking"] == {"type": "adaptive"}
    sent = request["messages"][0]["content"]
    assert "Teams meeting with Priya Shah" in sent and "No mail found." in sent and "No call logged." not in sent
    assert "@" not in sent  # the contact's email never goes to the model
    assert "raw" not in sent  # only card facts go to the model, never the raw record


def test_a_draft_citing_unknown_facts_is_retried_then_dropped():
    bad = dict(load("claude/nba_reply.json"), evidence_ids=["readai.call_9_sep"])
    client = FakeClaude(claude_response(bad), claude_response(bad))
    result = generate_nba(client, "claude-sonnet-5", "Northwind", northwind_card())
    assert not result.ok and result.draft is None
    assert "not on the card" in result.problems[0]
    retry = client.calls[1]["messages"]
    assert retry[1]["role"] == "assistant" and "cannot be saved" in retry[2]["content"]


def test_a_bad_first_draft_can_be_fixed_on_retry():
    good = load("claude/nba_reply.json")
    client = FakeClaude(claude_response(dict(good, evidence_ids=[])), claude_response(good))
    assert generate_nba(client, "claude-sonnet-5", "Northwind", northwind_card()).ok


def test_api_errors_and_refusals_do_not_raise():
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    down = anthropic.APIConnectionError(request=request)
    assert "Could not reach Claude" in generate_nba(FakeClaude(down), "m", "N", northwind_card()).error

    refused = FakeClaude(claude_response(None, stop_reason="refusal"))
    assert "declined" in generate_nba(refused, "m", "N", northwind_card()).error

"""Mail key points, the ICP signals and cache, the re-ranked capability, and a run with every
signal filled. All sources and models are fakes."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from api.db import get_sessionmaker
from api.engine import icp_signal
from api.engine.capability import capability_for
from api.engine.mail import KeyPoints, MailPoints, key_points
from api.icp.models import (CriterionInterpretation, CriterionScore, EvidenceLabel, GateResult, GroupScore,
                            OwnershipControl, ScoreResult)
from api.icp.pipeline import CompanyScore
from api.models import CompanyIcp, ContextCard, Meeting, Recommendation, Run
from api.sources.outlook import Mail, MailResult
from api.sources.readai import meeting_fields
from api.sources.setu import CaseStudy
from api.sources.zoho import row_to_deal
from tests.fakes import NOW, FakeClaude, claude_response, load, settings
from tests.test_readai import PAYLOAD
from tests.test_run import zoho_fetch


def mail(i, subject="Phasing", days_ago=1, preview="CFO asked for a phased plan by 30 Sep."):
    return Mail(id=f"m{i}", subject=subject, sender_name="Priya Shah", sender_domain="northwindfoods.example",
                domains=frozenset({"northwindfoods.example"}), received=NOW - timedelta(days=days_ago), preview=preview)


# ---------------------------------------------------------------- mail key points

def test_key_points_are_kept_per_mail_and_filtered():
    parsed = KeyPoints(mails=[MailPoints(index=1, key_points=["CFO wants a phased plan by 30 Sep", "x " * 30, ""]),
                              MailPoints(index=7, key_points=["out of range"])])
    client = FakeClaude(SimpleNamespace(parsed_output=parsed, stop_reason="end_turn", content=[]))
    points = key_points(client, "claude-haiku-4-5-20251001", "Northwind", [mail(1)])
    assert points == {1: ["CFO wants a phased plan by 30 Sep"]}  # too-long and empty points dropped, bad index ignored
    sent = client.calls[0]["messages"][0]["content"]
    assert "Preview: CFO asked for a phased plan" in sent and "@" not in sent


def test_key_points_fail_soft():
    import anthropic
    import httpx

    down = FakeClaude(anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com")))
    assert key_points(down, "m", "N", [mail(1)]) == {}
    assert key_points(None, "m", "N", [mail(1)]) == {} and key_points(FakeClaude(), "m", "N", []) == {}


# ---------------------------------------------------------------- ICP signals and cache

def fake_score(provisional=False):
    crit = lambda cid, score, gap=False: CriterionScore(criterion_id=cid, raw_score=score, weight=1, weighted_contribution=1,  # noqa: E731
                                                        condition_label="some_label", rationale=f"{cid} rationale", data_gap=gap,
                                                        evidence_label=EvidenceLabel.DATA_GAP)
    group = GroupScore(name="Ability to Pay", criteria=[crit("A1", 4), crit("A2", 3, gap=True)], subtotal=14, max_points=20,
                       sub_verdict="Moderate")
    access = GroupScore(name="Access", criteria=[crit("C1", 4)], subtotal=10, max_points=15, sub_verdict="Strong")
    score = ScoreResult(client_groups=[group, access], practus_criteria=[crit("P2", 5)], client_total=31,
                        client_verdict="Moderate", practus_total=38, practus_verdict="Strong",
                        gates=[GateResult(gate_id="5", name="Staleness", fired=True, detail="No stage change in 190 days"),
                               GateResult(gate_id="2", name="Authority", fired=False)],
                        recommendation="Pursue selectively", recommendation_reason="Strong fit, stale deal.",
                        provisional=provisional)
    interps = {"C1": CriterionInterpretation(criterion_id="C1", condition_label="sponsor_identified", rationale="CFO sponsors it."),
               "C2": CriterionInterpretation(criterion_id="C2", condition_label="single_threaded", rationale="One contact only.")}
    bundle = SimpleNamespace(entity=SimpleNamespace(ownership_control=OwnershipControl.WH), data_gaps=["No Exa key"])
    return CompanyScore("Northwind Foods Pvt Ltd", "scored", bundle, interps, score)


def test_icp_becomes_account_fit_and_stakeholder_facts():
    fit, stakeholder = icp_signal.signals(fake_score(), NOW)
    values = {f["id"]: f["value"] for f in fit["facts"]}
    assert values["icp.recommendation"] == "ICP recommendation: Pursue selectively. Strong fit, stale deal."
    assert values["icp.client_lens"].startswith("Client lens 31/50, Moderate: Ability 14/20 Moderate, Access 10/15 Strong")
    assert values["icp.gate_5"] == "Gate 5 (Staleness) fired: No stage change in 190 days"
    assert "icp.gate_2" not in values  # not fired
    assert values["icp.criterion_A1"] == "Scale 4/5 (some label): A1 rationale"
    assert "icp.criterion_A2" not in values and "icp.criterion_C1" not in values  # data gap; C1 is stakeholder
    assert [f["id"] for f in stakeholder["facts"]] == ["icp.authority", "icp.warmth"]
    assert stakeholder["facts"][1]["value"] == "Warmth & threading: single threaded. One contact only."
    assert all(f["source"] == "icp" and f["date"] == "2026-09-24" for f in fit["facts"] + stakeholder["facts"])


def test_provisional_scores_hide_the_total():
    fit, _ = icp_signal.signals(fake_score(provisional=True), NOW)
    client = next(f for f in fit["facts"] if f["id"] == "icp.client_lens")
    assert client["value"] == "Client lens: Moderate (provisional)"


def test_cache_reuses_for_four_weeks_and_never_reuses_failures(migrated):
    with get_sessionmaker()() as s:
        icp_signal.record(s, "Northwind Foods Pvt Ltd", NOW - timedelta(days=10), error="boom")  # newest-but-one: failed
        icp_signal.record(s, "Northwind Foods Pvt Ltd", NOW - timedelta(days=20), result=fake_score())
        s.commit()
        key = icp_signal.company_key("Northwind Foods Pvt Ltd")
        assert key == icp_signal.company_key("NORTHWIND FOODS PRIVATE LIMITED")  # legal suffixes don't matter
        assert icp_signal.fresh_icp(s, key, NOW, 28).status == "scored"
        assert icp_signal.fresh_icp(s, key, NOW + timedelta(days=9), 28) is None  # 29 days old now
        assert s.scalar(select(CompanyIcp).where(CompanyIcp.status == "scored")).result["score"]  # full result kept


# ---------------------------------------------------------------- capability with the re-rank

CORPUS = [CaseStudy(name="Patisserie & Bakes", content="Working capital controls.", industry="Food Processing"),
          CaseStudy(name="Steel Major", content="Cost audit in India.", industry="Steel Manufacturing")]


def northwind():
    return row_to_deal(load("zoho/deals.json")[0])


def team(**_):
    return [{"name": "A. Mehta", "grade": "EP", "status": "Active", "industry_match": True, "service_line_match": False,
             "keyword_hits": ["receivable"], "named_clients": ["Theobroma"]},
            {"name": "Old Hand", "grade": "EL", "status": "Inactive", "industry_match": True},
            {"name": "No Match", "grade": "TL", "status": "Active"}]


def test_reranked_cases_carry_claudes_reason_and_active_smes_only():
    find = lambda **kw: [{"name": "Steel Major", "industry": "Steel", "service_line": None, "content": "x", "industry_match": False}]  # noqa: E731
    rerank = lambda c, **kw: [{**c[0], "llm_rationale": "Same cost-leakage problem."}]  # noqa: E731
    cap = capability_for(northwind(), CORPUS, find_cases=find, rerank=rerank, find_team=team)
    assert cap.reranked and cap.cases[0]["name"] == "Steel Major" and cap.cases[0]["why"] == "Same cost-leakage problem."
    assert [s["name"] for s in cap.smes] == ["A. Mehta"]  # inactive and no-match people dropped
    assert "industry experience" in cap.smes[0]["why"] and "Theobroma" in cap.smes[0]["why"]


def test_a_failed_rerank_falls_back_to_the_strict_match_not_to_keyword_noise():
    zero_score = [{"name": "Steel Major", "industry": "Steel", "service_line": None, "content": "x", "industry_match": False}]
    fallback = lambda c, **kw: c[:3]  # the ICP bot's fallback: top keyword scores, no llm_rationale  # noqa: E731
    cap = capability_for(northwind(), CORPUS, find_cases=lambda **kw: zero_score, rerank=fallback, find_team=team)
    assert not cap.reranked and [c["name"] for c in cap.cases] == ["Patisserie & Bakes"]  # same industry, strict


def test_a_setu_outage_inside_the_rerank_still_gives_the_strict_match():
    def broken(**kw):
        raise RuntimeError("Setu down")

    cap = capability_for(northwind(), CORPUS, find_cases=broken, find_team=broken)
    assert [c["name"] for c in cap.cases] == ["Patisserie & Bakes"] and cap.smes == []


# ---------------------------------------------------------------- a run with every signal

@pytest.fixture()
def all_sources(migrated):
    with get_sessionmaker()() as s:
        s.add(Meeting(**meeting_fields({**PAYLOAD, "start_time": "2026-09-18T05:00:00Z"})))
        s.commit()
    calls = {"icp": 0}

    def fetch_mail(session, s, deals, belongs):
        return MailResult(by_deal={"598723000011234001": [mail(1)]})

    def capability(deal, corpus):
        rerank = lambda c, **kw: [{**c[0], "llm_rationale": "Fits the receivables problem."}]  # noqa: E731
        find = lambda **kw: [{"name": "Patisserie & Bakes", "industry": "Food Processing", "service_line": "SFT",  # noqa: E731
                              "content": "Working capital controls.", "industry_match": True}]
        return capability_for(deal, corpus, find_cases=find, rerank=rerank, find_team=team)

    def score(company, generated_at=None):
        calls["icp"] += 1
        return fake_score()

    return {"fetch_mail": fetch_mail, "capability_fn": capability, "score_company_fn": score, "calls": calls}


def run(session, src, reply_ids, **kw):
    from api.engine.run import run_week
    from api.sources.setu import SetuResult

    reply = claude_response(dict(load("claude/nba_reply.json"), evidence_ids=reply_ids))
    points = SimpleNamespace(parsed_output=KeyPoints(mails=[MailPoints(index=1, key_points=["CFO wants phasing by 30 Sep"])]),
                             stop_reason="end_turn", content=[])
    return run_week(session, settings(), now=NOW, fetch=zoho_fetch(), fetch_setu=lambda s: SetuResult(case_studies=CORPUS),
                    fetch_mail=src["fetch_mail"], capability_fn=src["capability_fn"], score_company_fn=src["score_company_fn"],
                    llm_client=FakeClaude(points, reply), deal_zoho_id="598723000011234001", **kw)


def test_a_run_fills_all_five_signals_and_the_nba_cites_across_them(all_sources):
    ids = ["zoho.stage", "outlook.mail_1", "readai.meeting_1", "setu.case_1", "icp.recommendation"]
    with get_sessionmaker()() as s:
        r = run(s, all_sources, ids)
        assert r.status == "succeeded", r.stats
        card = s.scalar(select(ContextCard).where(ContextCard.run_id == r.id,
                                                  ContextCard.deal_id == s.scalar(select(Recommendation.deal_id))))
        filled = {sig: bool((getattr(card, sig) or {}).get("facts"))
                  for sig in ("account_fit", "stakeholder", "conversation", "capability", "deal_state")}
        assert filled == dict.fromkeys(filled, True)
        assert set(card.gaps) == {"no_contact"}  # ICP, call, mail and Setu gaps all cleared
        conv = card.conversation
        assert conv["last_mail"]["subject"] == "Phasing" and conv["last_mail"]["key_points"] == ["CFO wants phasing by 30 Sep"]
        assert conv["meetings"] == 1 and conv["last_meeting"]["title"].startswith("Northwind")
        rec = s.scalar(select(Recommendation))
        assert [e["source"] for e in rec.evidence] == ["zoho", "outlook", "readai", "setu", "icp"]
        assert rec.proof == {"name": "Patisserie & Bakes", "why": "Fits the receivables problem."} and rec.sme == "A. Mehta"
        assert r.stats["phase"] == "done" and r.stats["icp_to_score"] == 2 and r.stats["capability_done"] == 2


def test_the_next_run_reuses_the_cached_icp(all_sources):
    ids = ["zoho.stage", "icp.recommendation"]
    with get_sessionmaker()() as s:
        run(s, all_sources, ids)
        assert all_sources["calls"]["icp"] == 2  # two companies scored once
        second = run(s, all_sources, ids)
        assert all_sources["calls"]["icp"] == 2 and second.stats["icp_cached"] == 2
        assert second.stats["sources"]["icp"] == "ok (all cached)"


def test_the_board_fills_the_segments_that_have_data(client, all_sources):
    with get_sessionmaker()() as s:
        run(s, all_sources, ["zoho.stage", "icp.recommendation"])
    boards = client.get("/api/week").json()["boards"]
    northwind_id = next(d["id"] for st in boards[0]["stages"] for d in st["deals"] if d["name"].startswith("Northwind"))
    detail = client.get(f"/api/deals/{northwind_id}").json()
    segments = {seg["key"]: seg for seg in detail["segments"]}
    assert all(seg["present"] for seg in segments.values())
    assert segments["conversation"]["sources"] == ["Zoho", "Call", "Mail"]
    assert segments["fit"]["source"] == "ICP" and segments["contact"]["source"] == "ICP"
    assert detail["icp"]["recommendation"] == "Pursue selectively"
    assert detail["actions"][0]["sme"] == "A. Mehta" and detail["actions"][0]["proof"]["name"] == "Patisserie & Bakes"


def test_without_an_anthropic_key_icp_and_rerank_are_skipped_and_said_so(migrated):
    from api.engine.run import run_week
    from api.sources.setu import SetuResult

    with get_sessionmaker()() as s:
        r = run_week(s, settings(), now=NOW, fetch=zoho_fetch(), fetch_setu=lambda _: SetuResult(case_studies=CORPUS),
                     fetch_mail=lambda *a: MailResult(error="Outlook is not configured"))
        assert r.stats["sources"]["icp"] == "skipped: ANTHROPIC_API_KEY is not set"
        assert r.stats["sources"]["setu_rerank"] == "skipped: ANTHROPIC_API_KEY is not set"
        assert r.stats["sources"]["outlook"] == "Outlook is not configured"
        assert s.scalar(select(CompanyIcp)) is None

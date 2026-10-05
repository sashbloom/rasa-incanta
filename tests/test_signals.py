"""Mail key points, the ICP signals and cache, the re-ranked capability, and a run with every
signal filled. All sources and models are fakes."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from api.db import get_sessionmaker
from api.engine import icp_signal
from api.engine.capability import Capability, capability_for, sme_text
from api.engine.context import build_card
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


def test_once_exa_is_set_a_score_made_without_it_is_redone(migrated):
    with get_sessionmaker()() as s:
        key = icp_signal.company_key("Northwind Foods Pvt Ltd")
        icp_signal.record(s, "Northwind Foods Pvt Ltd", NOW - timedelta(days=3), result=fake_score(provisional=True))
        s.commit()
        assert icp_signal.fresh_icp(s, key, NOW, 28) is not None  # still reused while Exa is off
        assert icp_signal.fresh_icp(s, key, NOW, 28, require_exa=True) is None  # rescored once the key is set
        icp_signal.record(s, "Northwind Foods Pvt Ltd", NOW - timedelta(days=1), result=fake_score(), exa_research=True)
        s.commit()
        assert icp_signal.fresh_icp(s, key, NOW, 28, require_exa=True).result["exa_research"] is True


def test_a_run_with_exa_rescores_companies_scored_without_it(all_sources):
    ids = ["zoho.stage", "icp.recommendation"]
    with get_sessionmaker()() as s:
        run(s, all_sources, ids)  # settings() has no EXA_API_KEY
        assert all_sources["calls"]["icp"] == 2
        run(s, all_sources, ids, settings_overrides={"exa_api_key": "exa-key"})
        assert all_sources["calls"]["icp"] == 4  # both companies rescored, now with research
        run(s, all_sources, ids, settings_overrides={"exa_api_key": "exa-key"})
        assert all_sources["calls"]["icp"] == 4  # and those scores are reused


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


def test_reranked_cases_carry_claudes_reason_and_the_deal_names_its_own_people():
    find = lambda **kw: [{"name": "Steel Major", "industry": "Steel", "service_line": None, "content": "x", "industry_match": False, "score": 1.0}]  # noqa: E731
    rerank = lambda c, **kw: [{**c[0], "llm_rationale": "Same cost-leakage problem."}]  # noqa: E731
    cap = capability_for(northwind(), CORPUS, find_cases=find, rerank=rerank, find_team=team)
    assert cap.reranked and cap.cases[0]["name"] == "Steel Major" and cap.cases[0]["why"] == "Same cost-leakage problem."
    assert cap.cases[0]["confidence"] == "moderate"  # same problem (score 1.0), different industry
    # Zoho names A. Mehta (EP) and R. Iyer (EL); "Unidentified EL" is a placeholder, and Setu is not asked
    assert [(s["name"], s["role"], s["on_deal"]) for s in cap.smes] == [("A. Mehta", "EP", True), ("R. Iyer", "EL", True)]
    assert sme_text(cap.smes) == "A. Mehta (EP), R. Iyer (EL)"


def no_one_named():
    return replace(northwind(), ep_involved=["Unidentified EP"], el_involved=[])


def test_a_deal_with_no_ep_or_el_gets_a_labelled_suggestion_from_setu_active_people_only():
    find = lambda **kw: []  # noqa: E731
    cap = capability_for(no_one_named(), CORPUS, find_cases=find, find_team=team)
    assert [s["name"] for s in cap.smes] == ["A. Mehta"]  # inactive and no-match people dropped
    assert cap.smes[0]["suggested"] is True and not cap.smes[0].get("on_deal")
    assert "industry experience" in cap.smes[0]["why"] and "Theobroma" in cap.smes[0]["why"]
    assert sme_text(cap.smes) == "A. Mehta (suggested SME, not yet on the deal)"


def test_the_setu_team_matcher_is_not_consulted_when_the_deal_names_people():
    def boom(**kw):
        raise AssertionError("the team matcher was asked although the deal names its EP and EL")

    cap = capability_for(northwind(), CORPUS, find_cases=lambda **kw: [], find_team=boom)
    assert [s["name"] for s in cap.smes] == ["A. Mehta", "R. Iyer"]


def test_ep_and_el_are_citable_facts_and_placeholders_are_not():
    card = build_card(northwind(), NOW.date())
    facts = {f["id"]: f for f in card.deal_state["facts"]}
    assert facts["zoho.ep_involved"]["value"] == "A. Mehta" and facts["zoho.el_involved"]["value"] == "R. Iyer"
    nobody = build_card(no_one_named(), NOW.date())
    ids = {f["id"] for f in nobody.deal_state["facts"]}
    assert "zoho.ep_involved" not in ids and "zoho.el_involved" not in ids  # "Unidentified EP" is not a person


def test_the_deals_own_people_are_on_the_card_even_without_setu():
    card = build_card(northwind(), NOW.date())
    assert [s["name"] for s in card.capability["smes"]] == ["A. Mehta", "R. Iyer"]
    assert not any(f["id"].startswith("setu.sme") for f in card.capability["facts"])  # cited via the zoho facts


def test_a_suggested_sme_is_a_fact_that_says_it_is_not_on_the_deal():
    cap = Capability(cases=[], smes=[{"name": "A. Mehta", "grade": "EP", "why": "industry experience", "suggested": True}], reranked=False)
    card = build_card(no_one_named(), NOW.date(), capability=cap)
    fact = next(f for f in card.capability["facts"] if f["id"] == "setu.sme_1")
    assert fact["value"].startswith("Suggested SME (not yet on the deal): A. Mehta")


def test_a_failed_rerank_falls_back_to_the_strict_match_not_to_keyword_noise():
    zero_score = [{"name": "Steel Major", "industry": "Steel", "service_line": None, "content": "x", "industry_match": False}]
    fallback = lambda c, **kw: c[:3]  # the ICP bot's fallback: top keyword scores, no llm_rationale  # noqa: E731
    cap = capability_for(northwind(), CORPUS, find_cases=lambda **kw: zero_score, rerank=fallback, find_team=team)
    assert not cap.reranked and [c["name"] for c in cap.cases] == ["Patisserie & Bakes"]  # same industry, strict


def test_a_setu_outage_inside_the_rerank_still_gives_the_strict_match():
    def broken(**kw):
        raise RuntimeError("Setu down")

    cap = capability_for(no_one_named(), CORPUS, find_cases=broken, find_team=broken)
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


def run(session, src, reply_ids, settings_overrides=None, **kw):
    from api.engine.run import run_week
    from api.sources.setu import SetuResult

    reply = claude_response(dict(load("claude/nba_reply.json"), evidence_ids=reply_ids))
    points = SimpleNamespace(parsed_output=KeyPoints(mails=[MailPoints(index=1, key_points=["CFO wants phasing by 30 Sep"])]),
                             stop_reason="end_turn", content=[])
    return run_week(session, settings(**(settings_overrides or {})), now=NOW, fetch=zoho_fetch(), fetch_setu=lambda s: SetuResult(case_studies=CORPUS),
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
        assert rec.proof == {"name": "Patisserie & Bakes", "why": "Fits the receivables problem."} and rec.sme == "A. Mehta (EP), R. Iyer (EL)"
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
    assert detail["actions"][0]["sme"] == "A. Mehta (EP), R. Iyer (EL)" and detail["actions"][0]["proof"]["name"] == "Patisserie & Bakes"


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


def test_match_confidence_tiers_and_the_relevance_floor():
    from api.engine.capability import match_confidence
    assert match_confidence(True, False, 2 + 0.6) == "strong"        # same industry and same problem
    assert match_confidence(False, True, 1 + 0.6) == "moderate"      # same problem, other industry
    assert match_confidence(False, True, 1.0) == "weak"              # geography only
    assert match_confidence(True, False, 2.0) == "weak"              # industry, but no shared problem
    assert match_confidence(False, False, 0.2) is None               # nothing: not offered


def _cap_with(cases):
    from api.engine.capability import Capability
    return Capability(cases=cases, smes=[], reranked=True)


def test_a_weak_best_match_is_flagged_on_every_case_fact_and_a_stronger_one_is_not():
    from api.engine.context import capability_signal
    weak = {"name": "Geo Only", "industry": "Retail", "service_line": None, "content": "x", "why": "w",
            "industry_match": False, "geography_match": True, "score": 1.0}
    sig, gaps = capability_signal(northwind(), None, _cap_with([{**weak, "confidence": "weak"}]))
    assert not gaps and sig["match_confidence"] == "weak"
    case_facts = [f for f in sig["facts"] if f["id"].startswith("setu.case_")]
    assert case_facts and all("[weak Setu match]" in f["value"] for f in case_facts)
    strong = {**weak, "name": "Both", "industry_match": True, "score": 2.9, "confidence": "strong"}
    sig, _ = capability_signal(northwind(), None, _cap_with([strong, {**weak, "confidence": "weak"}]))
    assert sig["match_confidence"] == "strong"
    assert not any("[weak Setu match]" in f["value"] for f in sig["facts"])


def test_no_case_study_over_the_floor_is_the_no_setu_match_gap():
    find = lambda **kw: [{"name": "Noise", "industry": "Steel", "service_line": None, "content": "x",
                          "industry_match": False, "geography_match": False, "score": 0.1}]  # noqa: E731
    rerank = lambda c, **kw: [{**c[0], "llm_rationale": "Generic."}]  # noqa: E731
    cap = capability_for(northwind(), CORPUS, find_cases=find, rerank=rerank, find_team=team)
    assert cap.cases == []


def test_the_proof_segment_carries_the_weak_flag():
    from types import SimpleNamespace
    from api.views import _segments
    fact = {"id": "setu.case_1", "source": "setu", "label": "x", "value": "v", "date": None}
    def card(conf):
        empty = {}
        return SimpleNamespace(account_fit=empty, stakeholder=empty, conversation=empty, deal_state=empty, gaps=[],
                               capability={"facts": [fact], "match_confidence": conf})
    proof = lambda c: next(s for s in _segments(c) if s["key"] == "proof")  # noqa: E731
    assert "[weak Setu match]" in proof(card("weak"))["source"]
    assert "[weak Setu match]" not in proof(card("strong"))["source"]

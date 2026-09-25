# Copied from icp-bot/backend/tests/test_evidence_assembler.py; only import paths are adapted.
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.icp import evidence_assembler as ea
from api.icp.models import (
    DuplicateAccount,
    EntityResolution,
    EvidenceLabel,
    ManualCompanyInfo,
    OwnershipControl,
    PractusHistory,
)
from api.icp.ownership_classifier import OwnershipClassification


@pytest.fixture(autouse=True)
def _no_real_assigned_person_lookup():
    """gather_setu_evidence() unconditionally looks up the deal's assigned
    EP/EL by exact name (find_assigned_person_profile, 2026-09-09 fix) --
    a real Setu DB call most tests in this file have nothing to do with and
    don't otherwise mock. Defaults it to "not found" everywhere; tests that
    actually exercise this wiring override it with their own nested patch."""
    with patch.object(ea.p3_team_matcher, "find_assigned_person_profile", return_value=None):
        yield


def _deal(**overrides):
    base = {
        "id": 101,
        "deal_name": "Sobha Ltd - Cost Visibility & Control",
        "stage": "Need Identification",
        "modified_time": datetime(2026, 8, 1),
        "created_time": datetime(2026, 7, 25),
        "closing_date": None,
        "ep_involved": "Bhavik Desai",
        "el_involved": None,
        "problem_area_1": "Cost Visibility",
        "problem_area_2": None,
        "client_problem_statement": None,
        "specify_reference": "Sangeeta",
        "potential_lead_source": None,
        "reason_for_loss": None,
        "industry_type": "Real Estate",
        "amount": 500000,
        "monthly_recurring_revenue_amount": None,
    }
    base.update(overrides)
    return base


def test_build_crm_structured_maps_deal_and_excludes_financials_from_a1a2a3_path():
    with patch.object(ea.zoho_db, "get_reachouts", return_value=[
        {"reachout_date": None, "client_contact_name": "CFO", "designation": "CFO", "contact_role": None, "email": "cfo@sobha.com", "reachout_medium": "Email", "remarks": None}
    ]), patch.object(ea.zoho_db, "get_deal_history", return_value=[
        {"stage": "Need Identification", "modified_time": datetime(2026, 7, 25)}
    ]):
        crm = ea.build_crm_structured(_deal())

    assert crm.deal_name == "Sobha Ltd - Cost Visibility & Control"
    assert crm.amount_context_only == 500000
    assert crm.reachout_tracker[0].email == "cfo@sobha.com"
    assert crm.stage_history[0].stage == "Need Identification"
    # CrmStructured has no field that scoring reads for A1/A2/A3 revenue —
    # the model itself, not just this function, is what enforces the ban.
    assert not hasattr(crm, "revenue")


def test_build_crm_structured_handles_no_deal():
    crm = ea.build_crm_structured(None)

    assert crm.deal_id is None
    assert crm.reachout_tracker == []


def test_problem_statement_text_prefers_explicit_statement_then_falls_back():
    crm = ea.build_crm_structured(_deal(client_problem_statement="Explicit statement"))
    assert ea.problem_statement_text(crm) == "Explicit statement" if crm.client_problem_statement else None

    with patch.object(ea.zoho_db, "get_reachouts", return_value=[]), patch.object(ea.zoho_db, "get_deal_history", return_value=[]):
        crm_no_statement = ea.build_crm_structured(_deal(client_problem_statement=None, problem_area_1="Cost Visibility"))
        assert ea.problem_statement_text(crm_no_statement) == "Cost Visibility"

        crm_only_deal_name = ea.build_crm_structured(_deal(client_problem_statement=None, problem_area_1=None, problem_area_2=None))
        assert ea.problem_statement_text(crm_only_deal_name) == "Sobha Ltd - Cost Visibility & Control"


def test_contact_domain_extracts_from_first_reachout_with_email():
    with patch.object(ea.zoho_db, "get_deal_history", return_value=[]):
        crm = ea.build_crm_structured(_deal())
    with patch.object(ea.zoho_db, "get_reachouts", return_value=[{"email": "cfo@sobha.com", "reachout_date": None, "client_contact_name": None, "designation": None, "contact_role": None, "reachout_medium": None, "remarks": None}]):
        crm = ea.build_crm_structured(_deal())

    assert ea.contact_domain(crm) == "sobha.com"


def test_gather_setu_evidence_records_data_gap_on_setu_error():
    """When the direct-DB lookup fails, it's recorded as a data gap and no
    item is returned -- Setu is DB-only now, there is no chat-endpoint
    fallback to retry through."""
    with patch.object(ea.zoho_db, "get_reachouts", return_value=[]), patch.object(ea.zoho_db, "get_deal_history", return_value=[]):
        crm = ea.build_crm_structured(_deal(client_problem_statement="Cost visibility and control"))

    data_gaps: list[str] = []
    with patch.object(ea.p2_case_study_matcher, "find_case_study_matches", side_effect=RuntimeError("db unreachable")), \
         patch.object(ea.p3_team_matcher, "find_team_matches", side_effect=RuntimeError("db unreachable")), \
         patch.object(ea.p3_external_sme_matcher, "find_external_sme_matches", side_effect=RuntimeError("db unreachable")):
        items = ea.gather_setu_evidence(crm, data_gaps)

    assert items == []
    assert any("P2" in g or "failed" in g for g in data_gaps)
    assert any("direct-DB lookup failed" in g for g in data_gaps)


def test_gather_setu_evidence_tags_p2_and_p3_items():
    with patch.object(ea.zoho_db, "get_reachouts", return_value=[]), patch.object(ea.zoho_db, "get_deal_history", return_value=[]):
        crm = ea.build_crm_structured(_deal(client_problem_statement="Cost visibility and control"))

    data_gaps: list[str] = []
    with patch.object(ea.p2_case_study_matcher, "find_case_study_matches", return_value=[]), \
         patch.object(ea.p3_team_matcher, "find_team_matches", return_value=[]), \
         patch.object(ea.p3_external_sme_matcher, "find_external_sme_matches", return_value=[]):
        items = ea.gather_setu_evidence(crm, data_gaps)

    tags = [tag for item in items for tag in item.criterion_tags]
    assert "P2" in tags
    assert "P3" in tags
    assert data_gaps == []


def test_gather_setu_evidence_uses_inferred_problem_statement_when_crm_is_empty():
    """Live-confirmed real bug (Uniparts India, 2026-09-09): a no-CRM
    manual entry with no client_problem_statement/problem_area/deal_name
    skipped the P2 case-study lookup entirely and scored P2 as "problem
    not established", even when secondary research contained a genuinely
    specific, dated management-stated priority. The inferred statement
    must both (a) unlock the P2 case-study lookup and (b) be surfaced as
    its own P2-tagged evidence item so scoring sees it too -- honestly
    labeled as inferred, not presented as a CRM fact."""
    crm = ea.CrmStructured()  # no problem fields, no deal name -- the no-CRM shape
    assert ea.problem_statement_text(crm) is None

    data_gaps: list[str] = []
    with patch.object(ea.p2_case_study_matcher, "find_case_study_matches", return_value=[]) as mock_find, \
         patch.object(ea.p3_team_matcher, "find_team_matches", return_value=[]), \
         patch.object(ea.p3_external_sme_matcher, "find_external_sme_matches", side_effect=RuntimeError("skip")):
        items = ea.gather_setu_evidence(
            crm, data_gaps, inferred_problem_statement="Customer concentration rose to 71% in Q1FY27."
        )

    mock_find.assert_called_once()
    assert "Customer concentration rose to 71% in Q1FY27." in mock_find.call_args.kwargs["problem_context"]
    inferred_items = [i for i in items if "Inferred client problem" in i.text]
    assert len(inferred_items) == 1
    assert "Customer concentration rose to 71% in Q1FY27." in inferred_items[0].text
    assert inferred_items[0].criterion_tags == ["P2"]
    assert not any("No client problem statement available" in g for g in data_gaps)


def test_gather_setu_evidence_prefers_the_real_crm_problem_over_an_inferred_one():
    """A real Zoho problem statement must always win -- the inferred
    fallback is only ever consulted when the CRM's own fields are empty."""
    with patch.object(ea.zoho_db, "get_reachouts", return_value=[]), patch.object(ea.zoho_db, "get_deal_history", return_value=[]):
        crm = ea.build_crm_structured(_deal(client_problem_statement="Real CRM-stated problem"))

    data_gaps: list[str] = []
    with patch.object(ea.p2_case_study_matcher, "find_case_study_matches", return_value=[]) as mock_find, \
         patch.object(ea.p3_team_matcher, "find_team_matches", return_value=[]), \
         patch.object(ea.p3_external_sme_matcher, "find_external_sme_matches", side_effect=RuntimeError("skip")):
        items = ea.gather_setu_evidence(
            crm, data_gaps, inferred_problem_statement="Should not be used"
        )

    assert "Real CRM-stated problem" in mock_find.call_args.kwargs["problem_context"]
    assert not any("Inferred client problem" in i.text for i in items)


def test_gather_setu_evidence_p3_team_lookup_uses_the_real_industry_and_deal_name():
    """P3 team evidence comes from a direct Setu-database lookup
    (p3_team_matcher.find_team_matches) -- Setu is DB-only, no chat-endpoint
    path exists at all. This checks the lookup receives the real,
    correctly-filtered industry/service-line/keyword context derived from
    the CRM record."""
    with patch.object(ea.zoho_db, "get_reachouts", return_value=[]), patch.object(ea.zoho_db, "get_deal_history", return_value=[]):
        crm = ea.build_crm_structured(_deal(client_problem_statement=None, problem_area_1=None, problem_area_2=None, industry_type="Real Estate"))

    data_gaps: list[str] = []
    # deal_name still resolves via problem_statement_text()'s deal-name
    # fallback, so P2/external-SME will also fire -- P2's DB lookup must be
    # mocked too, or this hits the real database over the network.
    with patch.object(ea.p2_case_study_matcher, "find_case_study_matches", return_value=[]), \
         patch.object(ea.p3_team_matcher, "find_team_matches", return_value=[]) as mock_find, \
         patch.object(ea.p3_external_sme_matcher, "find_external_sme_matches", return_value=[]):
        ea.gather_setu_evidence(crm, data_gaps)

    mock_find.assert_called_once()
    kwargs = mock_find.call_args.kwargs
    assert kwargs["industry"] == "Real Estate"
    assert kwargs["service_line"] is None
    assert "Real Estate" in kwargs["keyword_context"]
    assert kwargs["limit"] == 10  # a wider pool than the final count, for the LLM rerank step to choose among


def test_gather_setu_evidence_p3_team_passes_the_retrieved_candidates_through_llm_rerank():
    """Live-confirmed real gap: pure inverse-document-frequency ranking
    occasionally let a coincidentally-rare irrelevant word outrank a
    genuinely relevant one -- the deterministic DB retrieval stays the
    reliable part, but a small bounded LLM call now picks/reorders the
    final few from that already-retrieved, already-evidenced pool."""
    with patch.object(ea.zoho_db, "get_reachouts", return_value=[]), patch.object(ea.zoho_db, "get_deal_history", return_value=[]):
        crm = ea.build_crm_structured(_deal(client_problem_statement=None, problem_area_1=None, problem_area_2=None, industry_type="Real Estate"))

    candidates = [{"name": "Fenil Bhimani", "grade": "EL", "status": "Active", "industries": [], "service_lines": [],
                   "named_clients": [], "industry_match": False, "service_line_match": False, "keyword_hits": [], "resume_snippet": "", "score": 1.0}]
    reranked = [{**candidates[0], "llm_rationale": "Genuine fit."}]

    data_gaps: list[str] = []
    with patch.object(ea.p2_case_study_matcher, "find_case_study_matches", return_value=[]), \
         patch.object(ea.p3_team_matcher, "find_team_matches", return_value=candidates), \
         patch.object(ea.p3_team_matcher, "rerank_matches_with_llm", return_value=reranked) as mock_rerank, \
         patch.object(ea.p3_external_sme_matcher, "find_external_sme_matches", return_value=[]):
        items = ea.gather_setu_evidence(crm, data_gaps)

    mock_rerank.assert_called_once()
    assert mock_rerank.call_args.args[0] == candidates
    p3_db_item = next(i for i in items if i.source_type == "setu_db" and "Genuine fit." in i.text)
    assert p3_db_item is not None


def test_gather_setu_evidence_p3_team_lookup_skipped_records_data_gap_when_nothing_known():
    with patch.object(ea.zoho_db, "get_reachouts", return_value=[]), patch.object(ea.zoho_db, "get_deal_history", return_value=[]):
        crm = ea.build_crm_structured(_deal(deal_name=None, client_problem_statement=None, problem_area_1=None, problem_area_2=None, industry_type=None))

    data_gaps: list[str] = []
    with patch.object(ea.p3_team_matcher, "find_team_matches") as mock_find:
        ea.gather_setu_evidence(crm, data_gaps)

    mock_find.assert_not_called()
    assert any("P3 team lookup skipped" in g for g in data_gaps)


def test_gather_setu_evidence_p3_team_lookup_filters_generic_industry_and_anchors_on_deal_name():
    """Live-confirmed bug: `industry_type='Others'` (a generic Zoho
    placeholder, not a real industry) was previously passed straight through
    as if it were real. 'Others' must be filtered out, and the deal name
    used as a real search anchor instead of skipping the lookup outright."""
    with patch.object(ea.zoho_db, "get_reachouts", return_value=[]), patch.object(ea.zoho_db, "get_deal_history", return_value=[]):
        crm = ea.build_crm_structured(_deal(
            deal_name="Sula Wines - Growth & Expansion", client_problem_statement=None,
            problem_area_1=None, problem_area_2=None, industry_type="Others",
        ))

    data_gaps: list[str] = []
    with patch.object(ea.p2_case_study_matcher, "find_case_study_matches", return_value=[]), \
         patch.object(ea.p3_team_matcher, "find_team_matches", return_value=[]) as mock_find, \
         patch.object(ea.p3_external_sme_matcher, "find_external_sme_matches", return_value=[]):
        ea.gather_setu_evidence(crm, data_gaps)

    kwargs = mock_find.call_args.kwargs
    assert kwargs["industry"] is None
    assert "Sula Wines - Growth & Expansion" in kwargs["keyword_context"]
    assert not any("P3 team lookup skipped" in g for g in data_gaps)


def test_gather_setu_evidence_p3_team_records_data_gap_when_db_lookup_fails():
    with patch.object(ea.zoho_db, "get_reachouts", return_value=[]), patch.object(ea.zoho_db, "get_deal_history", return_value=[]):
        crm = ea.build_crm_structured(_deal(deal_name=None, client_problem_statement=None, problem_area_1=None, problem_area_2=None, industry_type="Real Estate"))

    data_gaps: list[str] = []
    with patch.object(ea.p3_team_matcher, "find_team_matches", side_effect=RuntimeError("db unreachable")):
        items = ea.gather_setu_evidence(crm, data_gaps)

    assert items == []
    assert any("P3 team direct-DB lookup failed" in g for g in data_gaps)


def test_gather_setu_evidence_p3_always_describes_the_assigned_ep_even_when_fuzzy_search_finds_nobody():
    """Real bug (Manappuram Finance, 2026-09-09): the deal's own assigned
    EP has a real, on-file skill_profile/resume, but the fuzzy industry/
    keyword search scored him 0 and dropped him -- the report then falsely
    claimed no record existed for him at all. The direct by-name lookup
    must always run (using crm.ep_involved/el_involved) and its result must
    always reach the evidence text, even when find_team_matches() itself
    returns nothing."""
    with patch.object(ea.zoho_db, "get_reachouts", return_value=[]), patch.object(ea.zoho_db, "get_deal_history", return_value=[]):
        crm = ea.build_crm_structured(_deal(ep_involved="Shashank Silhare", industry_type="BFSI"))

    assigned_profile = {
        "name": "Shashank Silhare", "grade": "EP", "status": "Active",
        "industries": ["Pharmaceuticals", "Metals"], "service_lines": ["Operations Transformation"],
        "named_clients": [], "has_resume_on_file": True,
    }
    data_gaps: list[str] = []
    with patch.object(ea.p2_case_study_matcher, "find_case_study_matches", return_value=[]), \
         patch.object(ea.p3_team_matcher, "find_team_matches", return_value=[]), \
         patch.object(ea.p3_team_matcher, "find_assigned_person_profile",
                       side_effect=lambda name: assigned_profile if name == "Shashank Silhare" else None) as mock_lookup, \
         patch.object(ea.p3_external_sme_matcher, "find_external_sme_matches", return_value=[]):
        items = ea.gather_setu_evidence(crm, data_gaps)

    mock_lookup.assert_any_call("Shashank Silhare")
    p3_item = next(i for i in items if i.source_type == "setu_db" and "Shashank Silhare" in i.text)
    assert "genuine zero-match" not in p3_item.text.lower()
    assert "Pharmaceuticals" in p3_item.text


def test_gather_setu_evidence_external_sme_uses_the_direct_db_lookup_and_llm_rerank():
    with patch.object(ea.zoho_db, "get_reachouts", return_value=[]), patch.object(ea.zoho_db, "get_deal_history", return_value=[]):
        crm = ea.build_crm_structured(_deal(client_problem_statement="Cost visibility and control"))

    candidates = [{"name": "Some Advisory LLP", "content": "Specialist governance advisory.", "keyword_hits": ["governance"], "score": 1.0}]
    reranked = [{**candidates[0], "llm_rationale": "Directly relevant specialist."}]

    data_gaps: list[str] = []
    with patch.object(ea.p2_case_study_matcher, "find_case_study_matches", return_value=[]), \
         patch.object(ea.p3_team_matcher, "find_team_matches", return_value=[]), \
         patch.object(ea.p3_external_sme_matcher, "find_external_sme_matches", return_value=candidates), \
         patch.object(ea.p3_external_sme_matcher, "rerank_external_smes_with_llm", return_value=reranked) as mock_rerank:
        items = ea.gather_setu_evidence(crm, data_gaps)

    mock_rerank.assert_called_once()
    assert mock_rerank.call_args.args[0] == candidates
    sme_item = next(i for i in items if i.source_type == "setu_db" and "Directly relevant specialist." in i.text)
    assert sme_item is not None
    assert data_gaps == []


def test_gather_setu_evidence_external_sme_records_data_gap_when_db_lookup_fails():
    with patch.object(ea.zoho_db, "get_reachouts", return_value=[]), patch.object(ea.zoho_db, "get_deal_history", return_value=[]):
        crm = ea.build_crm_structured(_deal(
            deal_name=None, client_problem_statement="Cost visibility and control", problem_area_1=None, problem_area_2=None, industry_type=None,
        ))

    data_gaps: list[str] = []
    with patch.object(ea.p2_case_study_matcher, "find_case_study_matches", return_value=[]), \
         patch.object(ea.p3_external_sme_matcher, "find_external_sme_matches", side_effect=RuntimeError("db unreachable")):
        items = ea.gather_setu_evidence(crm, data_gaps)

    assert not any("P3" in i.criterion_tags for i in items)  # the failed external-SME lookup contributes no item
    assert any("P3 external-SME direct-DB lookup failed" in g for g in data_gaps)


def test_gather_secondary_research_passes_six_needed_items_including_control_routed_asks():
    """A2/B2/B3 previously had no dedicated evidence ask at all -- this
    checks the 3 new asks (liquidity, advisory, priorities) actually reach
    build_research_prompt's `needed` kwarg, on top of the original 3."""
    ownership = OwnershipClassification(
        geography="india", is_listed=False, is_pe_vc_funded=False, control=OwnershipControl.PF,
        disclosure="Unlisted", management="Owner-managed", confidence=EvidenceLabel.FACT,
        rationale="test", sources=[],
    )

    with patch.object(ea.secondary_research, "build_research_prompt", wraps=ea.secondary_research.build_research_prompt) as spy, \
         patch.object(ea.exa_search, "search_many", return_value="Some evidence text."), \
         patch.object(ea, "research", return_value="ok"):
        ea.gather_secondary_research(
            company_name="Sobha", contracting_entity="Sobha Limited", ownership=ownership, data_gaps=[], now=datetime(2026, 8, 27),
        )

    needed = spy.call_args.kwargs["needed"]
    assert len(needed) == 6
    # Original 3 asks (revenue/margin trend, peers, hard triggers) untouched.
    assert "revenue and operating margin trend" in needed[0]
    assert "peers" in needed[1]
    assert "hard-trigger" in needed[2]
    # New asks target A2 liquidity, B2 advisory record, B3 priorities.
    assert any("liquidity" in item.lower() for item in needed[3:])
    assert any("Legal and Professional Fees" in item for item in needed[3:])
    assert any("priorities" in item.lower() for item in needed[3:])


def test_gather_secondary_research_needed_varies_between_pf_and_sc():
    def make_ownership(control):
        return OwnershipClassification(
            geography="india", is_listed=True, is_pe_vc_funded=False, control=control,
            disclosure="Listed", management="Professionally managed", confidence=EvidenceLabel.FACT,
            rationale="test", sources=[],
        )

    captured = {}

    def fake_build_research_prompt(**kwargs):
        captured[kwargs["control"]] = kwargs["needed"]
        return "prompt"

    with patch.object(ea.secondary_research, "build_research_prompt", side_effect=fake_build_research_prompt), \
         patch.object(ea.exa_search, "search_many", return_value="Some evidence text."), \
         patch.object(ea, "research", return_value="ok"):
        ea.gather_secondary_research(company_name="X", contracting_entity="X Ltd", ownership=make_ownership(OwnershipControl.PF), data_gaps=[], now=datetime(2026, 8, 27))
        ea.gather_secondary_research(company_name="X", contracting_entity="X Ltd", ownership=make_ownership(OwnershipControl.SC), data_gaps=[], now=datetime(2026, 8, 27))

    pf_needed = captured[OwnershipControl.PF]
    sc_needed = captured[OwnershipControl.SC]
    assert pf_needed[:3] == sc_needed[:3]
    assert pf_needed[3:] != sc_needed[3:]


def test_gather_secondary_research_gathers_exa_evidence_before_the_synthesis_call():
    """Live evidence-gathering now happens via exa_search.search_many()
    (see the plan's "run time" investigation — this replaced Anthropic's
    own agentic web_search tool, the single most expensive call site in a
    real run) -- research() itself is now just a plain synthesis call fed
    that pre-fetched evidence."""
    ownership = OwnershipClassification(
        geography="india", is_listed=True, is_pe_vc_funded=False, control=OwnershipControl.PF,
        disclosure="Listed", management="Professionally managed", confidence=EvidenceLabel.FACT,
        rationale="test", sources=[],
    )

    with patch.object(ea.exa_search, "search_many", return_value="Real Exa evidence block.") as mock_search_many, \
         patch.object(ea, "research", return_value="Synthesized finding.") as mock_research:
        items = ea.gather_secondary_research(
            company_name="Sobha", contracting_entity="Sobha Limited", ownership=ownership, data_gaps=[], now=datetime(2026, 8, 27),
        )

    mock_search_many.assert_called_once()
    queries = mock_search_many.call_args.args[0]
    assert all("Sobha Limited" in q for q in queries)
    assert "Real Exa evidence block." in mock_research.call_args.args[0]
    assert items[0].text == "Synthesized finding."


def test_gather_secondary_research_skips_the_synthesis_call_when_exa_finds_nothing():
    """search_many() degrades to an empty string (not an exception) when
    every query fails or returns nothing -- this must become an honest
    data gap rather than calling research() with zero real evidence, which
    would risk a hallucinated-sounding answer built on nothing."""
    ownership = OwnershipClassification(
        geography="india", is_listed=True, is_pe_vc_funded=False, control=OwnershipControl.PF,
        disclosure="Listed", management="Professionally managed", confidence=EvidenceLabel.FACT,
        rationale="test", sources=[],
    )
    data_gaps: list[str] = []

    with patch.object(ea.exa_search, "search_many", return_value=""), \
         patch.object(ea, "research") as mock_research:
        items = ea.gather_secondary_research(
            company_name="Sobha", contracting_entity="Sobha Limited", ownership=ownership, data_gaps=data_gaps, now=datetime(2026, 8, 27),
        )

    mock_research.assert_not_called()
    assert items == []
    assert any("no usable results" in g for g in data_gaps)


def test_gather_meeting_evidence_skips_when_no_contact_domain():
    with patch.object(ea.zoho_db, "get_reachouts", return_value=[]), patch.object(ea.zoho_db, "get_deal_history", return_value=[]):
        crm = ea.build_crm_structured(_deal())  # no reachouts -> no domain

    data_gaps: list[str] = []
    items = ea.gather_meeting_evidence(crm, data_gaps)

    assert items == []
    assert any("Read.ai" in g for g in data_gaps)


def test_gather_meeting_evidence_wraps_matching_meetings():
    with patch.object(ea.zoho_db, "get_reachouts", return_value=[{"email": "cfo@sobha.com", "reachout_date": None, "client_contact_name": None, "designation": None, "contact_role": None, "reachout_medium": None, "remarks": None}]), \
         patch.object(ea.zoho_db, "get_deal_history", return_value=[]):
        crm = ea.build_crm_structured(_deal())

    with patch.object(ea.meetings_store, "find_by_participant_domain", return_value=[
        {"title": "CFO sync", "start_time": "2026-07-23T10:00:00Z", "summary": "Discussed margins."}
    ]):
        items = ea.gather_meeting_evidence(crm, [])

    assert len(items) == 1
    assert "CFO sync" in items[0].text
    assert items[0].source_type == "meeting"


def test_gather_mailbox_evidence_wraps_messages():
    with patch.object(ea.mail_client, "search_configured_mailboxes", return_value=[
        {"subject": "RE: Sobha proposal", "from": {"emailAddress": {"address": "cfo@sobha.com"}}, "bodyPreview": "...", "_mailbox": "mahak@practus.com", "receivedDateTime": "2026-07-20T00:00:00Z"}
    ]):
        items = ea.gather_mailbox_evidence("Sobha")

    assert len(items) == 1
    assert items[0].source_type == "outlook"
    assert "mahak@practus.com" in items[0].source


def test_assemble_wires_everything_together_and_updates_ownership():
    entity = EntityResolution(
        company_name_queried="Sobha",
        matched_account_ids=[1],
        matched_deal_ids=[101],
        duplicate_accounts=[DuplicateAccount(account_id=1, account_name="Sobha Ltd.", confirmed=True)],
        contracting_entity_name="Sobha Limited",
        contracting_entity_note="test",
        entity_resolved=True,
    )
    ownership = OwnershipClassification(
        geography="india", is_listed=True, is_pe_vc_funded=False, control=OwnershipControl.PF,
        disclosure="Listed", management="Professionally managed", confidence=EvidenceLabel.FACT,
        rationale="test", sources=[], ipo_track=True,
    )

    with patch.object(ea.entity_resolver, "resolve", return_value=entity), \
         patch.object(ea.zoho_db, "get_deal", return_value=_deal()), \
         patch.object(ea.zoho_db, "get_reachouts", return_value=[]), \
         patch.object(ea.zoho_db, "get_deal_history", return_value=[]), \
         patch.object(ea.practus_history, "classify", return_value=PractusHistory(is_practus_client=False)), \
         patch.object(ea.ownership_classifier, "classify", return_value=ownership), \
         patch.object(ea.exa_search, "search_many", return_value="Some evidence text."), \
         patch.object(ea.p2_case_study_matcher, "find_case_study_matches", return_value=[]), \
         patch.object(ea.p3_team_matcher, "find_team_matches", return_value=[]), \
         patch.object(ea.p3_external_sme_matcher, "find_external_sme_matches", return_value=[]), \
         patch.object(ea, "research", return_value="Some research finding."), \
         patch.object(ea.secondary_research, "extract_revenue_figure", return_value={
             "revenue_cr": 2500.0, "ebitda_cr": 300.0, "post_money_cash_cr": None,
             "source": "Screener.in", "date": "2026-03",
         }), \
         patch.object(ea.meetings_store, "find_by_participant_domain", return_value=[]), \
         patch.object(ea.mail_client, "search_configured_mailboxes", return_value=[]):
        bundle = ea.assemble("Sobha")

    assert bundle.entity.ownership_control == OwnershipControl.PF
    assert bundle.entity.ownership_disclosure == "Listed"
    # Confirmed by a wiring audit these were computed at both ends
    # (ownership_classifier.py -> this model_copy -> brief_section.py's
    # hedging / talk_section.py's IPO overlay) but never actually asserted
    # right here, at the assemble() call itself.
    assert bundle.entity.ownership_confidence == EvidenceLabel.FACT
    assert bundle.entity.ipo_track is True
    assert bundle.secondary_financials.revenue_cr == 2500.0
    assert bundle.secondary_financials.source == "Screener.in"
    source_types = {item.source_type for item in bundle.unstructured}
    # P2/P3-team/external-SME all succeed via the direct Setu-database
    # lookup here (mocked to return_value=[] -- no exception), so nothing
    # falls back to the Setu chat endpoint at all -- "setu_db", not "setu",
    # is the expected source_type for a fully-successful run now that all
    # three have been migrated off the chat endpoint.
    assert "setu_db" in source_types
    assert "secondary_research" in source_types
    # The fixture's deal has no reachouts, so there's no contact domain to
    # look meetings up by — that's the one expected gap here, not a failure.
    assert bundle.data_gaps == ["No contact email on file — Read.ai meeting lookup skipped."]


def test_assemble_degrades_to_a_data_gap_when_outlook_mail_search_raises():
    """Live-confirmed real gap: a fresh deploy with no Graph OAuth refresh
    token on file yet raised GraphOAuthError from gather_mailbox_evidence(),
    which -- unlike every other evidence source in this function -- had no
    try/except around its call site, so it took down the entire run instead
    of just being recorded as a data gap like Setu/Gate 4/Read.ai already
    are."""
    entity = EntityResolution(
        company_name_queried="Sobha", matched_account_ids=[1], matched_deal_ids=[101],
        duplicate_accounts=[DuplicateAccount(account_id=1, account_name="Sobha Ltd.", confirmed=True)],
        contracting_entity_name="Sobha Limited", contracting_entity_note="test", entity_resolved=True,
    )
    ownership = OwnershipClassification(
        geography="india", is_listed=True, is_pe_vc_funded=False, control=OwnershipControl.PF,
        disclosure="Listed", management="Professionally managed", confidence=EvidenceLabel.FACT,
        rationale="test", sources=[],
    )

    with patch.object(ea.entity_resolver, "resolve", return_value=entity), \
         patch.object(ea.zoho_db, "get_deal", return_value=_deal()), \
         patch.object(ea.zoho_db, "get_reachouts", return_value=[]), \
         patch.object(ea.zoho_db, "get_deal_history", return_value=[]), \
         patch.object(ea.practus_history, "classify", return_value=PractusHistory(is_practus_client=False)), \
         patch.object(ea.ownership_classifier, "classify", return_value=ownership), \
         patch.object(ea.exa_search, "search_many", return_value="Some evidence text."), \
         patch.object(ea.p2_case_study_matcher, "find_case_study_matches", return_value=[]), \
         patch.object(ea.p3_team_matcher, "find_team_matches", return_value=[]), \
         patch.object(ea.p3_external_sme_matcher, "find_external_sme_matches", return_value=[]), \
         patch.object(ea, "research", return_value="Some research finding."), \
         patch.object(ea.secondary_research, "extract_revenue_figure", return_value={
             "revenue_cr": 2500.0, "ebitda_cr": 300.0, "post_money_cash_cr": None,
             "source": "Screener.in", "date": "2026-03",
         }), \
         patch.object(ea.meetings_store, "find_by_participant_domain", return_value=[]), \
         patch.object(ea.mail_client, "search_configured_mailboxes", side_effect=RuntimeError("No refresh_token on file")):
        bundle = ea.assemble("Sobha")  # must not raise

    assert not any(item.source_type == "outlook" for item in bundle.unstructured)
    assert any("Outlook mail search failed" in g for g in bundle.data_gaps)


def test_assemble_invokes_on_industry_hint_early_with_the_crms_industry_type():
    """Track A perf fix: orchestrator.py uses this callback to kick off
    Gate 4's identify_competitors() as soon as the CRM's industry_type is
    known, overlapping it with the rest of evidence assembly (which
    previously ran it entirely sequentially, afterward). Must fire with the
    real CRM value, not None/unset, and specifically BEFORE the slow
    ownership/research calls -- verified here by asserting ownership.classify
    hasn't been called yet at the moment the callback runs."""
    entity = EntityResolution(
        company_name_queried="Sobha", matched_account_ids=[1], matched_deal_ids=[101],
        duplicate_accounts=[DuplicateAccount(account_id=1, account_name="Sobha Ltd.", confirmed=True)],
        contracting_entity_name="Sobha Limited", contracting_entity_note="test", entity_resolved=True,
    )
    ownership = OwnershipClassification(
        geography="india", is_listed=True, is_pe_vc_funded=False, control=OwnershipControl.PF,
        disclosure="Listed", management="Professionally managed", confidence=EvidenceLabel.FACT,
        rationale="test", sources=[],
    )
    received: list[str | None] = []
    ownership_call_count_when_fired: list[int] = []

    def _capture(industry_hint):
        received.append(industry_hint)
        ownership_call_count_when_fired.append(ea.ownership_classifier.classify.call_count)

    with patch.object(ea.entity_resolver, "resolve", return_value=entity), \
         patch.object(ea.zoho_db, "get_deal", return_value=_deal(industry_type="Real Estate")), \
         patch.object(ea.zoho_db, "get_reachouts", return_value=[]), \
         patch.object(ea.zoho_db, "get_deal_history", return_value=[]), \
         patch.object(ea.practus_history, "classify", return_value=PractusHistory(is_practus_client=False)), \
         patch.object(ea.ownership_classifier, "classify", return_value=ownership) as mock_classify, \
         patch.object(ea.exa_search, "search_many", return_value="Some evidence text."), \
         patch.object(ea.p2_case_study_matcher, "find_case_study_matches", return_value=[]), \
         patch.object(ea.p3_team_matcher, "find_team_matches", return_value=[]), \
         patch.object(ea.p3_external_sme_matcher, "find_external_sme_matches", return_value=[]), \
         patch.object(ea, "research", return_value="Some research finding."), \
         patch.object(ea.secondary_research, "extract_revenue_figure", return_value={
             "revenue_cr": None, "ebitda_cr": None, "post_money_cash_cr": None, "source": None, "date": None,
         }), \
         patch.object(ea.meetings_store, "find_by_participant_domain", return_value=[]), \
         patch.object(ea.mail_client, "search_configured_mailboxes", return_value=[]):
        ea.assemble("Sobha", on_industry_hint=_capture)

    assert received == ["Real Estate"]
    assert ownership_call_count_when_fired == [0]
    mock_classify.assert_called_once()


def test_assemble_does_not_require_on_industry_hint():
    """Default (omitted) must not break assemble() -- every existing caller
    that doesn't know about this callback keeps working unchanged."""
    with patch.object(ea.zoho_db, "get_deal", return_value=None), \
         patch.object(ea.entity_resolver, "resolve", return_value=EntityResolution(company_name_queried="Nobody", entity_resolved=False)), \
         patch.object(ea.practus_history, "classify", return_value=PractusHistory(is_practus_client=False)), \
         patch.object(ea.ownership_classifier, "classify", side_effect=RuntimeError("no data")), \
         patch.object(ea.p3_team_matcher, "find_team_matches", side_effect=RuntimeError("db unreachable")), \
         patch.object(ea.meetings_store, "find_by_participant_domain", return_value=[]), \
         patch.object(ea.mail_client, "search_configured_mailboxes", return_value=[]):
        bundle = ea.assemble("Nobody")

    assert bundle.entity.entity_resolved is False


def test_gather_secondary_research_and_financials_both_run_when_ownership_resolves():
    """Track A perf fix: these two calls now run concurrently
    (ThreadPoolExecutor) instead of one after another -- this doesn't
    assert on real wall-clock overlap (that would be flaky in a unit test),
    but confirms both still genuinely run and their results both still
    land on the bundle, i.e. the concurrency refactor didn't silently drop
    either call or its result."""
    entity = EntityResolution(
        company_name_queried="Sobha", matched_account_ids=[1], matched_deal_ids=[101],
        duplicate_accounts=[DuplicateAccount(account_id=1, account_name="Sobha Ltd.", confirmed=True)],
        contracting_entity_name="Sobha Limited", contracting_entity_note="test", entity_resolved=True,
    )
    ownership = OwnershipClassification(
        geography="india", is_listed=True, is_pe_vc_funded=False, control=OwnershipControl.PF,
        disclosure="Listed", management="Professionally managed", confidence=EvidenceLabel.FACT,
        rationale="test", sources=[],
    )

    with patch.object(ea.entity_resolver, "resolve", return_value=entity), \
         patch.object(ea.zoho_db, "get_deal", return_value=_deal()), \
         patch.object(ea.zoho_db, "get_reachouts", return_value=[]), \
         patch.object(ea.zoho_db, "get_deal_history", return_value=[]), \
         patch.object(ea.practus_history, "classify", return_value=PractusHistory(is_practus_client=False)), \
         patch.object(ea.ownership_classifier, "classify", return_value=ownership), \
         patch.object(ea.exa_search, "search_many", return_value="Some evidence text."), \
         patch.object(ea.p2_case_study_matcher, "find_case_study_matches", return_value=[]), \
         patch.object(ea.p3_team_matcher, "find_team_matches", return_value=[]), \
         patch.object(ea.p3_external_sme_matcher, "find_external_sme_matches", return_value=[]), \
         patch.object(ea, "research", return_value="Named peer: Diageo India.") as mock_research, \
         patch.object(ea.secondary_research, "extract_revenue_figure", return_value={
             "revenue_cr": 2500.0, "ebitda_cr": 300.0, "post_money_cash_cr": None,
             "source": "Screener.in", "date": "2026-03",
         }) as mock_extract, \
         patch.object(ea.meetings_store, "find_by_participant_domain", return_value=[]), \
         patch.object(ea.mail_client, "search_configured_mailboxes", return_value=[]):
        bundle = ea.assemble("Sobha")

    mock_research.assert_called_once()
    mock_extract.assert_called_once()
    assert any("Named peer: Diageo India." in item.text for item in bundle.unstructured)
    assert bundle.secondary_financials.revenue_cr == 2500.0


def test_assemble_feeds_secondary_research_text_into_the_p3_team_keyword_context():
    """Live-confirmed gap: the CRM's own fields alone found a resume
    keyword hit for "wine" but never the real peer-set match (a person
    whose resume names an actual industry peer/competitor as a client) --
    those names only ever appear in secondary research (icp-skill.md A3
    already asks it to name real peers), never in any CRM field. Secondary
    research must run BEFORE the P3 team lookup and its text must reach
    p3_team_matcher.find_team_matches as part of the keyword context."""
    entity = EntityResolution(
        company_name_queried="Sula Wines", matched_account_ids=[1], matched_deal_ids=[101],
        duplicate_accounts=[DuplicateAccount(account_id=1, account_name="Sula Wines", confirmed=True)],
        contracting_entity_name="Sula Vineyards Limited", contracting_entity_note="test", entity_resolved=True,
    )
    ownership = OwnershipClassification(
        geography="india", is_listed=True, is_pe_vc_funded=False, control=OwnershipControl.PF,
        disclosure="Listed", management="Professionally managed", confidence=EvidenceLabel.FACT,
        rationale="test", sources=[],
    )

    with patch.object(ea.entity_resolver, "resolve", return_value=entity), \
         patch.object(ea.zoho_db, "get_deal", return_value=_deal(deal_name="Sula Wines - Growth & Expansion", industry_type="Others")), \
         patch.object(ea.zoho_db, "get_reachouts", return_value=[]), \
         patch.object(ea.zoho_db, "get_deal_history", return_value=[]), \
         patch.object(ea.practus_history, "classify", return_value=PractusHistory(is_practus_client=False)), \
         patch.object(ea.practus_history, "confirmed_clients_by_keyword_context", return_value=(None, [])), \
         patch.object(ea.ownership_classifier, "classify", return_value=ownership), \
         patch.object(ea.exa_search, "search_many", return_value="Some evidence text."), \
         patch.object(ea.p2_case_study_matcher, "find_case_study_matches", return_value=[]), \
         patch.object(ea.p3_team_matcher, "find_team_matches", return_value=[]) as mock_find, \
         patch.object(ea.p3_external_sme_matcher, "find_external_sme_matches", return_value=[]), \
         patch.object(ea, "research", return_value="Named peer: Diageo India, via its subsidiary United Spirits."), \
         patch.object(ea.secondary_research, "extract_revenue_figure", return_value={
             "revenue_cr": None, "ebitda_cr": None, "post_money_cash_cr": None, "source": None, "date": None,
         }), \
         patch.object(ea.meetings_store, "find_by_participant_domain", return_value=[]), \
         patch.object(ea.mail_client, "search_configured_mailboxes", return_value=[]):
        ea.assemble("Sula Wines")

    mock_find.assert_called_once()
    assert "Diageo India" in mock_find.call_args.kwargs["keyword_context"]


def test_assemble_finds_a_keyword_matched_industry_credential_when_exact_match_is_empty():
    """Live-confirmed real gap: "Theobroma Foods Private Limited" (a real
    confirmed client tagged FMCG) never surfaced for a wine/beverage
    prospect because the exact-match path only ever compares against the
    CRM's own generic industry_type. Once secondary research names the
    real sector, assemble() should find it and attach it to the bundle's
    practus_history — but only when the exact-match list came back empty."""
    entity = EntityResolution(
        company_name_queried="Sula Wines", matched_account_ids=[1], matched_deal_ids=[101],
        duplicate_accounts=[DuplicateAccount(account_id=1, account_name="Sula Wines", confirmed=True)],
        contracting_entity_name="Sula Vineyards Limited", contracting_entity_note="test", entity_resolved=True,
    )
    ownership = OwnershipClassification(
        geography="india", is_listed=True, is_pe_vc_funded=False, control=OwnershipControl.PF,
        disclosure="Listed", management="Professionally managed", confidence=EvidenceLabel.FACT,
        rationale="test", sources=[],
    )

    with patch.object(ea.entity_resolver, "resolve", return_value=entity), \
         patch.object(ea.zoho_db, "get_deal", return_value=_deal(deal_name="Sula Wines - Growth & Expansion", industry_type="Others")), \
         patch.object(ea.zoho_db, "get_reachouts", return_value=[]), \
         patch.object(ea.zoho_db, "get_deal_history", return_value=[]), \
         patch.object(ea.practus_history, "classify", return_value=PractusHistory(is_practus_client=False)), \
         patch.object(ea.practus_history, "confirmed_clients_by_keyword_context",
                       return_value=("FMCG", ["Theobroma Foods Private Limited"])) as mock_proxy, \
         patch.object(ea.practus_history, "filter_sector_irrelevant_names",
                       return_value=["Theobroma Foods Private Limited"]), \
         patch.object(ea.ownership_classifier, "classify", return_value=ownership), \
         patch.object(ea.exa_search, "search_many", return_value="Some evidence text."), \
         patch.object(ea.p2_case_study_matcher, "find_case_study_matches", return_value=[]), \
         patch.object(ea.p3_team_matcher, "find_team_matches", return_value=[]), \
         patch.object(ea.p3_external_sme_matcher, "find_external_sme_matches", return_value=[]), \
         patch.object(ea, "research", return_value="Sula operates in the FMCG / alcoholic beverages segment."), \
         patch.object(ea.secondary_research, "extract_revenue_figure", return_value={
             "revenue_cr": None, "ebitda_cr": None, "post_money_cash_cr": None, "source": None, "date": None,
         }), \
         patch.object(ea.meetings_store, "find_by_participant_domain", return_value=[]), \
         patch.object(ea.mail_client, "search_configured_mailboxes", return_value=[]):
        bundle = ea.assemble("Sula Wines")

    mock_proxy.assert_called_once()
    assert "FMCG / alcoholic beverages" in mock_proxy.call_args.args[0]
    assert bundle.practus_history.keyword_matched_industry == "FMCG"
    assert bundle.practus_history.keyword_matched_client_names == ["Theobroma Foods Private Limited"]


def test_assemble_skips_the_keyword_proxy_lookup_when_an_exact_industry_match_already_exists():
    entity = EntityResolution(
        company_name_queried="Sobha", matched_account_ids=[1], matched_deal_ids=[101],
        duplicate_accounts=[DuplicateAccount(account_id=1, account_name="Sobha", confirmed=True)],
        contracting_entity_name="Sobha Limited", contracting_entity_note="test", entity_resolved=True,
    )
    ownership = OwnershipClassification(
        geography="india", is_listed=True, is_pe_vc_funded=False, control=OwnershipControl.PF,
        disclosure="Listed", management="Professionally managed", confidence=EvidenceLabel.FACT,
        rationale="test", sources=[],
    )

    with patch.object(ea.entity_resolver, "resolve", return_value=entity), \
         patch.object(ea.zoho_db, "get_deal", return_value=_deal(industry_type="Real Estate")), \
         patch.object(ea.zoho_db, "get_reachouts", return_value=[]), \
         patch.object(ea.zoho_db, "get_deal_history", return_value=[]), \
         patch.object(ea.practus_history, "classify", return_value=PractusHistory(
             is_practus_client=False, industry_confirmed_client_names=["Casa Grande"],
         )), \
         patch.object(ea.practus_history, "filter_sector_irrelevant_names", return_value=["Casa Grande"]), \
         patch.object(ea.practus_history, "confirmed_clients_by_keyword_context") as mock_proxy, \
         patch.object(ea.ownership_classifier, "classify", return_value=ownership), \
         patch.object(ea.exa_search, "search_many", return_value="Some evidence text."), \
         patch.object(ea.p2_case_study_matcher, "find_case_study_matches", return_value=[]), \
         patch.object(ea.p3_team_matcher, "find_team_matches", return_value=[]), \
         patch.object(ea.p3_external_sme_matcher, "find_external_sme_matches", return_value=[]), \
         patch.object(ea, "research", return_value="Some research finding."), \
         patch.object(ea.secondary_research, "extract_revenue_figure", return_value={
             "revenue_cr": None, "ebitda_cr": None, "post_money_cash_cr": None, "source": None, "date": None,
         }), \
         patch.object(ea.meetings_store, "find_by_participant_domain", return_value=[]), \
         patch.object(ea.mail_client, "search_configured_mailboxes", return_value=[]):
        ea.assemble("Sobha")

    mock_proxy.assert_not_called()


def test_assemble_records_data_gap_when_entity_not_resolved():
    entity = EntityResolution(company_name_queried="Nobody Inc", entity_resolved=False)

    with patch.object(ea.entity_resolver, "resolve", return_value=entity), \
         patch.object(ea.practus_history, "classify", return_value=PractusHistory(is_practus_client=False)), \
         patch.object(ea.ownership_classifier, "classify", side_effect=RuntimeError("no data to research")), \
         patch.object(ea.p3_team_matcher, "find_team_matches", side_effect=RuntimeError("db unreachable")), \
         patch.object(ea.meetings_store, "find_by_participant_domain", return_value=[]), \
         patch.object(ea.mail_client, "search_configured_mailboxes", return_value=[]):
        bundle = ea.assemble("Nobody Inc")

    assert bundle.entity.entity_resolved is False
    assert any("could not be resolved" in g for g in bundle.data_gaps)
    assert any("Ownership classification" in g for g in bundle.data_gaps)


def test_assemble_applies_manual_override_when_entity_unresolved():
    """Gate 1 fallback (item 1 of the plan's manual-entry feature): a
    company with zero Zoho footprint can still be scored if the caller
    supplies manual_info -- entity_resolved is forced True and the manual
    fields populate CrmStructured, but the real resolver still ran first
    (no query skipped) so a real record would have taken priority."""
    entity = EntityResolution(company_name_queried="Ghost Co", entity_resolved=False)
    manual = ManualCompanyInfo(
        legal_name="Ghost Company Pvt Ltd", stage="Potential", industry="FMCG",
        problem_statement="Cost visibility", ep_name="Bhavik Desai",
    )

    with patch.object(ea.entity_resolver, "resolve", return_value=entity) as mock_resolve, \
         patch.object(ea.practus_history, "classify", return_value=PractusHistory(is_practus_client=False)), \
         patch.object(ea.ownership_classifier, "classify", side_effect=RuntimeError("no data")), \
         patch.object(ea.p3_team_matcher, "find_team_matches", side_effect=RuntimeError("db unreachable")), \
         patch.object(ea.meetings_store, "find_by_participant_domain", return_value=[]), \
         patch.object(ea.mail_client, "search_configured_mailboxes", return_value=[]):
        bundle = ea.assemble("Ghost Co", manual_info=manual)

    mock_resolve.assert_called_once_with("Ghost Co")  # real lookup still ran
    assert bundle.entity.entity_resolved is True
    assert bundle.entity.contracting_entity_name == "Ghost Company Pvt Ltd"
    assert "Manually entered" in bundle.entity.contracting_entity_note
    assert bundle.crm.stage == "Potential"
    assert bundle.crm.industry_type == "FMCG"
    assert bundle.crm.client_problem_statement == "Cost visibility"
    assert bundle.crm.ep_involved == "Bhavik Desai"
    assert not any("could not be resolved" in g for g in bundle.data_gaps)
    assert any("manually entered" in g.lower() for g in bundle.data_gaps)


def test_assemble_uses_manual_website_to_disambiguate_websearch_calls_and_report_note():
    """A user may not know the exact legal entity name for a brand-new
    prospect, but usually knows its website -- a much stronger
    disambiguator than a possibly-approximate name for the web-search-based
    calls (ownership + secondary research/financials), and worth surfacing
    plainly in the report note too so a reader can confirm the right
    company was researched."""
    entity = EntityResolution(company_name_queried="Ghost Co", entity_resolved=False)
    manual = ManualCompanyInfo(website="ghostco.example.com", industry="FMCG")
    ownership = OwnershipClassification(
        control=OwnershipControl.PF, disclosure="Unlisted", management="Owner-managed",
        confidence=EvidenceLabel.FACT, geography="india", is_listed=False, is_pe_vc_funded=False,
    )

    with patch.object(ea.entity_resolver, "resolve", return_value=entity), \
         patch.object(ea.practus_history, "classify", return_value=PractusHistory(is_practus_client=False)), \
         patch.object(ea.ownership_classifier, "classify", return_value=ownership) as mock_classify, \
         patch.object(ea, "gather_secondary_research", return_value=[]) as mock_research, \
         patch.object(ea, "gather_secondary_financials", return_value=ea.SecondaryFinancials()) as mock_financials, \
         patch.object(ea.p3_team_matcher, "find_team_matches", side_effect=RuntimeError("db unreachable")), \
         patch.object(ea.meetings_store, "find_by_participant_domain", return_value=[]), \
         patch.object(ea.mail_client, "search_configured_mailboxes", return_value=[]):
        bundle = ea.assemble("Ghost Co", manual_info=manual)

    assert "ghostco.example.com" in mock_classify.call_args.args[1]
    assert "ghostco.example.com" in mock_research.call_args.kwargs["contracting_entity"]
    assert "ghostco.example.com" in mock_financials.call_args.kwargs["contracting_entity"]
    assert "ghostco.example.com" in bundle.entity.contracting_entity_note
    assert "Manually entered" in bundle.entity.contracting_entity_note
    # The clean display name must stay untouched by the search-disambiguation string.
    assert bundle.entity.contracting_entity_name == "Ghost Co"


def test_assemble_ignores_manual_info_when_entity_already_resolved():
    """A real Zoho record must always win -- manual_info is only ever
    consulted as a fallback, never overriding a real find."""
    deal = _deal()
    entity = EntityResolution(
        company_name_queried="Sobha", matched_account_ids=[1], matched_deal_ids=[101],
        contracting_entity_name="Sobha Limited", contracting_entity_note="Account and Potential both found.",
        entity_resolved=True,
    )
    manual = ManualCompanyInfo(industry="Should Not Apply")

    with patch.object(ea.zoho_db, "get_deal", return_value=deal), \
         patch.object(ea.entity_resolver, "resolve", return_value=entity), \
         patch.object(ea.practus_history, "classify", return_value=PractusHistory(is_practus_client=False)), \
         patch.object(ea.ownership_classifier, "classify", side_effect=RuntimeError("no data")), \
         patch.object(ea.p3_team_matcher, "find_team_matches", side_effect=RuntimeError("db unreachable")), \
         patch.object(ea.meetings_store, "find_by_participant_domain", return_value=[]), \
         patch.object(ea.mail_client, "search_configured_mailboxes", return_value=[]):
        bundle = ea.assemble("Sobha", manual_info=manual)

    assert bundle.entity.contracting_entity_note == "Account and Potential both found."
    assert bundle.crm.industry_type != "Should Not Apply"


def test_assemble_does_not_apply_manual_override_when_manual_info_is_empty():
    entity = EntityResolution(company_name_queried="Ghost Co", entity_resolved=False)

    with patch.object(ea.entity_resolver, "resolve", return_value=entity), \
         patch.object(ea.practus_history, "classify", return_value=PractusHistory(is_practus_client=False)), \
         patch.object(ea.ownership_classifier, "classify", side_effect=RuntimeError("no data")), \
         patch.object(ea.p3_team_matcher, "find_team_matches", side_effect=RuntimeError("db unreachable")), \
         patch.object(ea.meetings_store, "find_by_participant_domain", return_value=[]), \
         patch.object(ea.mail_client, "search_configured_mailboxes", return_value=[]):
        bundle = ea.assemble("Ghost Co", manual_info=ManualCompanyInfo())

    assert bundle.entity.entity_resolved is False


def test_build_crm_structured_from_manual_leaves_unsupplied_fields_at_default():
    manual = ManualCompanyInfo(industry="FMCG")

    crm = ea.build_crm_structured_from_manual(manual)

    assert crm.industry_type == "FMCG"
    assert crm.stage is None
    assert crm.reachout_tracker == []
    assert crm.stage_history == []
    assert crm.amount_context_only is None


def test_gather_setu_evidence_runs_the_three_lookups_concurrently():
    """P2 case studies, P3 team and P3 external SME are mutually independent
    but ran back to back: Langfuse measured 27 + 26 + 27s on the 2026-09-10
    Manappuram run for three calls that could cost ~27s together.

    Each stub blocks on a 3-party barrier, so all three must be in flight at
    once for any to return -- a sequential implementation fails on the
    barrier's own timeout rather than passing slowly."""
    import threading

    started = threading.Barrier(3, timeout=10)

    def _blocking(value):
        def _fn(*args, **kwargs):
            started.wait()
            return value
        return _fn

    with patch.object(ea.zoho_db, "get_reachouts", return_value=[]), \
         patch.object(ea.zoho_db, "get_deal_history", return_value=[]):
        crm = ea.build_crm_structured(_deal(client_problem_statement="Cost visibility and control"))

    data_gaps: list[str] = []
    with patch.object(ea.p2_case_study_matcher, "find_case_study_matches", _blocking([])), \
         patch.object(ea.p2_case_study_matcher, "rerank_case_studies_with_llm", return_value=[]), \
         patch.object(ea.p2_case_study_matcher, "format_case_studies_as_evidence_text", return_value="p2 text"), \
         patch.object(ea.p3_team_matcher, "find_assigned_person_profile", return_value=None), \
         patch.object(ea.p3_team_matcher, "find_team_matches", _blocking([])), \
         patch.object(ea.p3_team_matcher, "rerank_matches_with_llm", return_value=[]), \
         patch.object(ea.p3_team_matcher, "format_matches_as_evidence_text", return_value="p3 team text"), \
         patch.object(ea.p3_external_sme_matcher, "find_external_sme_matches", _blocking([])), \
         patch.object(ea.p3_external_sme_matcher, "rerank_external_smes_with_llm", return_value=[]), \
         patch.object(ea.p3_external_sme_matcher, "format_external_smes_as_evidence_text", return_value="sme text"):
        items = ea.gather_setu_evidence(crm, data_gaps)

    # Order must stay P2 -> P3 team -> P3 SME regardless of which finished
    # first: every prompt built downstream reads this list in order.
    assert [i.text for i in items] == ["p2 text", "p3 team text", "sme text"]


def test_gather_setu_evidence_keeps_one_failing_lookup_from_sinking_the_others():
    """Each block owns its own try/except and degrades to a data gap -- that
    has to survive the fan-out, not just the old sequential path."""
    with patch.object(ea.zoho_db, "get_reachouts", return_value=[]), \
         patch.object(ea.zoho_db, "get_deal_history", return_value=[]):
        crm = ea.build_crm_structured(_deal(client_problem_statement="Cost visibility and control"))

    data_gaps: list[str] = []
    with patch.object(ea.p2_case_study_matcher, "find_case_study_matches", side_effect=RuntimeError("setu down")), \
         patch.object(ea.p3_team_matcher, "find_assigned_person_profile", return_value=None), \
         patch.object(ea.p3_team_matcher, "find_team_matches", return_value=[]), \
         patch.object(ea.p3_team_matcher, "rerank_matches_with_llm", return_value=[]), \
         patch.object(ea.p3_team_matcher, "format_matches_as_evidence_text", return_value="p3 team text"), \
         patch.object(ea.p3_external_sme_matcher, "find_external_sme_matches", return_value=[]), \
         patch.object(ea.p3_external_sme_matcher, "rerank_external_smes_with_llm", return_value=[]), \
         patch.object(ea.p3_external_sme_matcher, "format_external_smes_as_evidence_text", return_value="sme text"):
        items = ea.gather_setu_evidence(crm, data_gaps)

    assert [i.text for i in items] == ["p3 team text", "sme text"]
    assert any("P2 case-study direct-DB lookup failed" in g for g in data_gaps)

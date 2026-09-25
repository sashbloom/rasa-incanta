# Copied from icp-bot/backend/tests/test_secondary_research.py; only import paths are adapted.
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.icp import secondary_research as sr
from api.icp.models import OwnershipControl


def test_source_bucket_routes_india_listed_vs_unlisted_vs_funded():
    assert sr.source_bucket(geography="india", is_listed=True, is_pe_vc_funded=False) == "india_listed"
    assert sr.source_bucket(geography="india", is_listed=False, is_pe_vc_funded=False) == "india_unlisted"
    assert sr.source_bucket(geography="india", is_listed=False, is_pe_vc_funded=True) == "india_funded"


def test_source_bucket_routes_mea_and_defaults_to_us():
    assert sr.source_bucket(geography="MEA", is_listed=True, is_pe_vc_funded=False) == "mea"
    assert sr.source_bucket(geography="other", is_listed=True, is_pe_vc_funded=False) == "us"


def test_build_research_queries_includes_the_entity_name_in_every_query():
    queries = sr.build_research_queries("Sobha Limited", bucket="india_listed")

    assert len(queries) >= 5
    assert all("Sobha Limited" in q for q in queries)


def test_build_research_queries_varies_by_bucket():
    listed = sr.build_research_queries("Foo Ltd", bucket="india_listed")
    us = sr.build_research_queries("Foo Ltd", bucket="us")

    assert listed[0] != us[0]


def test_build_research_prompt_includes_named_sources_and_conflict_rule():
    prompt = sr.build_research_prompt(
        company_name="Sobha", contracting_entity="Sobha Limited", geography="india",
        is_listed=True, is_pe_vc_funded=False, control=OwnershipControl.PF,
        needed=["latest 3 years revenue and margin"],
    )
    assert "Screener.in" in prompt
    assert "latest 3 years revenue and margin" in prompt
    assert "internal evidence beats public web" in prompt
    assert "PF" in prompt


def test_build_research_prompt_anchors_recency_to_todays_date():
    """Confirmed live: this prompt previously had NO date context at all --
    the model had no way to know a search result citing an old figure was
    stale relative to the real current date, or that it should prefer a
    newer filing over an older one."""
    prompt = sr.build_research_prompt(
        company_name="Sobha", contracting_entity="Sobha Limited", geography="india",
        is_listed=True, is_pe_vc_funded=False, control=OwnershipControl.PF,
        needed=["latest 3 years revenue and margin"], today="2026-08-27",
    )
    assert "2026-08-27" in prompt
    assert "most recent" in prompt.lower()


def test_build_research_prompt_omits_date_anchor_when_not_given():
    prompt = sr.build_research_prompt(
        company_name="Sobha", contracting_entity="Sobha Limited", geography="india",
        is_listed=True, is_pe_vc_funded=False, control=OwnershipControl.PF,
        needed=["latest 3 years revenue and margin"],
    )
    assert "Today's date" not in prompt


def test_extract_revenue_figure_uses_exa_search_and_synthesize(monkeypatch):
    with patch.object(sr.exa_search, "search_and_synthesize", return_value=(
        {"revenue_cr": 2500.0, "ebitda_cr": 300.0, "post_money_cash_cr": None, "source": "Screener.in", "date": "2026-03"},
        [],
    )) as mock_search:
        result = sr.extract_revenue_figure(
            contracting_entity="Sobha Limited", geography="india", is_listed=True, is_pe_vc_funded=False, control=OwnershipControl.PF,
        )

    assert result["revenue_cr"] == 2500.0
    call_kwargs = mock_search.call_args.kwargs
    assert call_kwargs["output_schema"] == sr.REVENUE_SCHEMA
    assert "Sobha Limited" in mock_search.call_args.args[0]


def test_extract_revenue_figure_adds_cash_instruction_only_for_si_control():
    with patch.object(sr.exa_search, "search_and_synthesize", return_value=({}, [])) as mock_search:
        sr.extract_revenue_figure(
            contracting_entity="Foo Ltd", geography="india", is_listed=False, is_pe_vc_funded=True, control=OwnershipControl.SI,
        )
        si_prompt = mock_search.call_args.kwargs["system_prompt"]

        sr.extract_revenue_figure(
            contracting_entity="Foo Ltd", geography="india", is_listed=False, is_pe_vc_funded=False, control=OwnershipControl.PF,
        )
        pf_prompt = mock_search.call_args.kwargs["system_prompt"]

    assert "also find its post-money cash" in si_prompt
    assert "leave it null" in pf_prompt


def test_extract_revenue_figure_propagates_exa_errors():
    with patch.object(sr.exa_search, "search_and_synthesize", side_effect=sr.exa_search.ExaError("boom")):
        with pytest.raises(sr.exa_search.ExaError, match="boom"):
            sr.extract_revenue_figure(
                contracting_entity="Sobha Limited", geography="india", is_listed=True, is_pe_vc_funded=False, control=OwnershipControl.PF,
            )


def test_build_extra_needed_items_returns_three_items_routed_by_control():
    pf_items = sr.build_extra_needed_items(OwnershipControl.PF)
    sc_items = sr.build_extra_needed_items(OwnershipControl.SC)

    assert len(pf_items) == 3
    assert len(sc_items) == 3
    # A2 liquidity ask must differ by control -- it's built straight from
    # criteria_tables.A2_LIQUIDITY_ROUTING, which is control-specific.
    assert pf_items[0] != sc_items[0]
    assert "promoter" in pf_items[0].lower()
    assert "dry powder" in sc_items[0].lower()
    # B3 priorities ask must also differ by control.
    assert pf_items[2] != sc_items[2]
    assert "promoter" in pf_items[2].lower()
    assert "value-creation plan" in sc_items[2].lower()


def test_build_extra_needed_items_advisory_ask_mentions_legal_and_professional_fees():
    for control in (OwnershipControl.PF, OwnershipControl.SC, None):
        items = sr.build_extra_needed_items(control)
        assert "Legal and Professional Fees" in items[1]


def test_build_extra_needed_items_handles_none_control_generically():
    items = sr.build_extra_needed_items(None)
    assert len(items) == 3
    # Must not crash and must still ask about liquidity/advisors/priorities.
    assert "liquidity" in items[0].lower() or "cash" in items[0].lower()
    assert "advisor" in items[1].lower() or "auditor" in items[1].lower()
    assert "priorities" in items[2].lower()


def test_gather_secondary_research_needed_list_differs_between_control_types():
    """The actual bug fix: the `needed` list passed to build_research_prompt
    must contain the 3 original asks plus 3 new ones, and the new ones must
    vary by control type -- not just have text added."""
    from api.icp import evidence_assembler as ea
    from api.icp.models import EvidenceLabel
    from api.icp.ownership_classifier import OwnershipClassification

    def make_ownership(control):
        return OwnershipClassification(
            geography="india", is_listed=True, is_pe_vc_funded=False, control=control,
            disclosure="Listed", management="Professionally managed",
            confidence=EvidenceLabel.FACT, rationale="test", sources=[],
        )

    captured = {}

    def fake_build_research_prompt(**kwargs):
        captured["needed"] = kwargs["needed"]
        return "prompt"

    with patch("api.icp.evidence_assembler.secondary_research.build_research_prompt", side_effect=fake_build_research_prompt), \
         patch("api.icp.evidence_assembler.exa_search.search_many", return_value="Some evidence text."), \
         patch("api.icp.evidence_assembler.research", return_value="ok"):
        ea.gather_secondary_research(
            company_name="X", contracting_entity="X Ltd",
            ownership=make_ownership(OwnershipControl.PF), data_gaps=[], now=datetime(2026, 8, 27),
        )
        pf_needed = captured["needed"]

        ea.gather_secondary_research(
            company_name="X", contracting_entity="X Ltd",
            ownership=make_ownership(OwnershipControl.SC), data_gaps=[], now=datetime(2026, 8, 27),
        )
        sc_needed = captured["needed"]

    assert len(pf_needed) == 6
    assert len(sc_needed) == 6
    assert pf_needed[:3] == sc_needed[:3]  # original 3 asks untouched
    assert pf_needed[3:] != sc_needed[3:]  # new 3 asks vary by control


def test_extract_revenue_figure_returns_nulls_when_not_findable():
    with patch.object(sr.exa_search, "search_and_synthesize", return_value=(
        {"revenue_cr": None, "ebitda_cr": None, "post_money_cash_cr": None, "source": None, "date": None}, [],
    )):
        result = sr.extract_revenue_figure(
            contracting_entity="Unknown Co", geography="other", is_listed=False, is_pe_vc_funded=False, control=None,
        )

    assert result["revenue_cr"] is None


def test_infer_problem_statement_from_research_returns_the_llm_finding():
    with patch.object(sr, "generate_structured_narrative", return_value={
        "problem_statement": "Customer concentration rose to 71% of revenue in Q1FY27, up from 64%.",
        "rationale": "A specific, dated, quantified concentration-risk figure the company itself disclosed.",
    }) as mock_generate:
        result = sr.infer_problem_statement_from_research(
            "Q1FY27 earnings call: top-5 customer concentration rose to 71% from 64% a year ago.",
            "Uniparts India Limited",
        )

    assert result == "Customer concentration rose to 71% of revenue in Q1FY27, up from 64%."
    call_args = mock_generate.call_args
    assert call_args.args[1] == sr._PROBLEM_STATEMENT_SCHEMA
    assert "Uniparts India Limited" in call_args.args[0]


def test_infer_problem_statement_from_research_honors_a_genuine_no_match():
    """Same honesty philosophy as practus_history.confirmed_clients_by_
    keyword_context() -- a generic strategic-sounding phrase must not be
    manufactured into a "problem" just because the research text exists."""
    with patch.object(sr, "generate_structured_narrative", return_value={
        "problem_statement": None,
        "rationale": "Only generic strategy language ('continue to grow') is present -- nothing specific.",
    }):
        result = sr.infer_problem_statement_from_research("The company aims to grow and improve efficiency.", "Foo Ltd")

    assert result is None


def test_infer_problem_statement_from_research_returns_none_for_empty_text():
    result = sr.infer_problem_statement_from_research("", "Foo Ltd")

    assert result is None


def test_infer_problem_statement_from_research_degrades_to_none_on_llm_failure():
    with patch.object(sr, "generate_structured_narrative", side_effect=RuntimeError("rate limited")):
        result = sr.infer_problem_statement_from_research("Some research text.", "Foo Ltd")

    assert result is None

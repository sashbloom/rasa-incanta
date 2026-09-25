# Copied from icp-bot/backend/tests/test_p2_case_study_matcher.py; only import paths are adapted.
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.icp import p2_case_study_matcher as cm


def _case_study(name, content, industry=None, service_line=None):
    return {"entity_name": name, "content": content, "industry": industry, "service_line": service_line}


def test_find_case_study_matches_matches_fmcg_directly_without_the_p3_employee_alias():
    """Real, confirmed bug this fixes: case_study has its OWN native
    "FMCG"/"FMCG / Retail" vocabulary (confirmed live), a DIFFERENT
    taxonomy from skill_profile's (used for P3 team matching, which has NO
    "FMCG" value at all and instead aliases it to "Consumer Goods").
    Reusing p3_team_matcher's alias here used to translate "FMCG" ->
    "Consumer Goods" BEFORE comparing, which broke the match against a case
    study's own literal "FMCG" tag even though they're the same real value.
    This must now match directly, with no alias translation at all."""
    with patch.object(cm.setu_db, "fetch_case_studies", return_value=[
        _case_study("HUL — PMI Customer Journey", "Case Study: HUL — PMI Customer Journey. Problem: brand awareness.", industry="FMCG"),
    ]):
        matches = cm.find_case_study_matches(problem_context="Sula Wines FMCG growth", industry="FMCG")

    assert len(matches) == 1
    assert matches[0]["industry_match"] is True


def test_find_case_study_matches_does_not_wrongly_translate_fmcg_to_consumer_goods():
    """The inverse of the bug above: a case study tagged "Consumer Goods"
    (a genuinely different, real case_study value) must NOT count as an
    industry match for a deal whose industry is "FMCG" -- confirming the
    old p3_team_matcher alias (fmcg -> Consumer Goods) is no longer being
    applied here at all."""
    with patch.object(cm.setu_db, "fetch_case_studies", return_value=[
        _case_study("Some Consumer Goods Case", "Case Study: Some Consumer Goods Case. Problem: something else.", industry="Consumer Goods"),
    ]):
        matches = cm.find_case_study_matches(problem_context="Sula Wines FMCG growth", industry="FMCG")

    assert len(matches) == 1
    assert matches[0]["industry_match"] is False


def test_find_case_study_matches_scores_geography_match():
    """A geography match (from ownership_classifier's already-computed
    geography, mapped to real country-name keywords) is a secondary
    scoring boost on top of industry/keyword matching -- an India-mentioning
    case study should outscore an otherwise-identical one with no
    geography signal."""
    with patch.object(cm.setu_db, "fetch_case_studies", return_value=[
        _case_study("India Case", "Case Study: India Case. Delivered for an American Multinational Pharma Company (India)."),
        _case_study("No Geography Case", "Case Study: No Geography Case. Delivered for a multinational company."),
    ]):
        matches = cm.find_case_study_matches(problem_context="growth expansion", industry=None, geography="india")

    by_name = {m["name"]: m for m in matches}
    assert by_name["India Case"]["geography_match"] is True
    assert by_name["No Geography Case"]["geography_match"] is False
    assert by_name["India Case"]["score"] > by_name["No Geography Case"]["score"]


def test_find_case_study_matches_geography_none_does_not_boost_anything():
    with patch.object(cm.setu_db, "fetch_case_studies", return_value=[
        _case_study("India Case", "Case Study: India Case. Delivered for a company in India."),
    ]):
        matches = cm.find_case_study_matches(problem_context="growth", industry=None, geography=None)

    assert matches[0]["geography_match"] is False


def test_find_case_study_matches_ranks_keyword_overlap_above_no_overlap():
    with patch.object(cm.setu_db, "fetch_case_studies", return_value=[
        _case_study("Unrelated Auto Case", "Case Study: Unrelated Auto Case. Problem: automotive component sourcing."),
        _case_study("Patisserie & Bakes — Financial Governance", "Case Study: Patisserie & Bakes. Problem: rapid multi-city bakery expansion."),
    ]):
        matches = cm.find_case_study_matches(problem_context="Sula Wines Bakery expansion issue", industry=None)

    assert matches[0]["name"] == "Patisserie & Bakes — Financial Governance"
    assert matches[0]["score"] > matches[1]["score"]


def test_find_case_study_matches_includes_zero_keyword_score_case_studies_for_the_llm_to_judge():
    """Live-confirmed real gap: unlike P3's resume matching, the MOST
    relevant case study for a given problem can be a genuinely conceptual
    analogy with ZERO keyword overlap ("Patisserie & Bakes" -- rapid
    multi-city expansion, weak financial governance -- is the closest real
    match for a wine company's growth/margin problem despite sharing no
    literal words with it). A hard keyword-score cutoff would exclude such
    a case from ever reaching the LLM rerank step, which is the only place
    that kind of judgment can happen -- so this must NOT filter zero-score
    case studies out, since the corpus is small and bounded (~83 total),
    unlike an open-ended search."""
    with patch.object(cm.setu_db, "fetch_case_studies", return_value=[
        _case_study("Totally Unrelated Case", "Case Study: Totally Unrelated Case. Problem: something else entirely."),
    ]):
        matches = cm.find_case_study_matches(problem_context="Sula Wines Growth Expansion", industry="FMCG")

    assert len(matches) == 1
    assert matches[0]["score"] == 0


def test_format_case_studies_as_evidence_text_handles_empty_list_honestly():
    text = cm.format_case_studies_as_evidence_text([])

    assert "genuine zero-match" in text.lower()
    assert "not a search failure" in text.lower()


def test_format_case_studies_as_evidence_text_orders_matches_and_includes_rationale():
    matches = [
        {"name": "Patisserie & Bakes", "industry": "F&B", "service_line": "Finance", "content": "Bakery expansion case.",
         "industry_match": True, "keyword_hits": ["bakery"], "score": 2.5, "llm_rationale": "Closest industry analogue."},
    ]

    text = cm.format_case_studies_as_evidence_text(matches)

    assert "Patisserie & Bakes" in text
    assert "closest match first" in text.lower()
    assert "Closest industry analogue." in text


def _match(name, **overrides):
    base = {"name": name, "industry": None, "service_line": None, "content": "", "industry_match": False, "keyword_hits": [], "score": 1.0}
    base.update(overrides)
    return base


def test_rerank_case_studies_with_llm_returns_empty_immediately_for_empty_input():
    with patch("api.icp.anthropic_client.generate_structured_narrative") as mock_generate:
        result = cm.rerank_case_studies_with_llm([], problem_context="anything")

    assert result == []
    mock_generate.assert_not_called()


def test_rerank_case_studies_with_llm_orders_per_the_llm_response():
    matches = [_match("Generic Manufacturing Case", score=2.0), _match("Patisserie & Bakes", score=1.0)]

    with patch(
        "api.icp.anthropic_client.generate_structured_narrative",
        return_value={"selected": [
            {"name": "Patisserie & Bakes", "rationale": "Closest real industry analogue — F&B expansion."},
            {"name": "Generic Manufacturing Case", "rationale": "Adjacent problem type only."},
        ]},
    ):
        result = cm.rerank_case_studies_with_llm(matches, problem_context="Sula Wines growth")

    assert result[0]["name"] == "Patisserie & Bakes"
    assert result[0]["llm_rationale"] == "Closest real industry analogue — F&B expansion."
    assert result[1]["name"] == "Generic Manufacturing Case"


def test_rerank_case_studies_with_llm_falls_back_to_deterministic_ranking_on_failure():
    matches = [_match("Patisserie & Bakes", score=1.0)]

    with patch(
        "api.icp.anthropic_client.generate_structured_narrative",
        side_effect=RuntimeError("truncated"),
    ):
        result = cm.rerank_case_studies_with_llm(matches, problem_context="Sula Wines growth")

    assert result == matches[:3]


def test_rerank_case_studies_with_llm_falls_back_when_it_hallucinates_a_name():
    matches = [_match("Patisserie & Bakes", score=1.0)]

    with patch(
        "api.icp.anthropic_client.generate_structured_narrative",
        return_value={"selected": [{"name": "Made Up Case Study", "rationale": "invented"}]},
    ):
        result = cm.rerank_case_studies_with_llm(matches, problem_context="Sula Wines growth")

    assert result == matches[:3]

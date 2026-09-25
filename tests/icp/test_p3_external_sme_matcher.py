# Copied from icp-bot/backend/tests/test_p3_external_sme_matcher.py; only import paths are adapted.
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.icp import p3_external_sme_matcher as sm


def _profile(name, content):
    return {"entity_name": name, "content": content}


def test_find_external_sme_matches_ranks_keyword_overlap_above_no_overlap():
    with patch.object(sm.setu_db, "fetch_partner_profiles", return_value=[
        _profile("Unrelated Consultancy", "Generic advisory services across sectors."),
        _profile("Wine Industry Specialists LLP", "Specialist consultancy for the wine and vineyard sector."),
    ]):
        matches = sm.find_external_sme_matches(problem_context="Sula Wines vineyard expansion")

    assert matches[0]["name"] == "Wine Industry Specialists LLP"
    assert matches[0]["score"] > matches[1]["score"]


def test_find_external_sme_matches_includes_zero_keyword_score_profiles():
    """No structured tag exists on partner_profile at all (confirmed live —
    metadata is just {"chunk": N}), so a hard cutoff would exclude a
    genuinely relevant profile that happens to share no literal keyword
    with the problem statement -- the LLM rerank step is the only place
    that judgment can happen, so zero-score profiles must still reach it."""
    with patch.object(sm.setu_db, "fetch_partner_profiles", return_value=[
        _profile("Totally Unrelated Profile", "Something else entirely."),
    ]):
        matches = sm.find_external_sme_matches(problem_context="Sula Wines vineyard expansion")

    assert len(matches) == 1
    assert matches[0]["score"] == 0


def test_find_external_sme_matches_excludes_raw_filenames():
    """Real bug (Livpure Smart Homes, 2026-09-09): a partner_profile chunk's
    own entity_name was a raw working-file name ("Vivek, Vamesh,
    Bimal.pptx"), not a person/firm name -- it reached client-facing prose
    verbatim. No genuine partner/firm name ends in a file extension, so this
    is filtered out deterministically before the LLM rerank step even sees
    it."""
    with patch.object(sm.setu_db, "fetch_partner_profiles", return_value=[
        _profile("Vivek, Vamesh, Bimal.pptx", "Some internal working deck content."),
        _profile("Wine Industry Specialists LLP", "Specialist consultancy for the wine and vineyard sector."),
    ]):
        matches = sm.find_external_sme_matches(problem_context="Sula Wines vineyard expansion")

    assert len(matches) == 1
    assert matches[0]["name"] == "Wine Industry Specialists LLP"


def test_format_external_smes_as_evidence_text_handles_empty_list_honestly():
    text = sm.format_external_smes_as_evidence_text([])

    assert "genuine zero-match" in text.lower()
    assert "not a search failure" in text.lower()


def test_format_external_smes_as_evidence_text_includes_rationale():
    matches = [
        {"name": "Wine Industry Specialists LLP", "content": "Specialist consultancy.", "keyword_hits": ["wine"],
         "score": 1.0, "llm_rationale": "Directly relevant to the wine sector."},
    ]

    text = sm.format_external_smes_as_evidence_text(matches)

    assert "Wine Industry Specialists LLP" in text
    assert "closest match first" in text.lower()
    assert "Directly relevant to the wine sector." in text


def _match(name, **overrides):
    base = {"name": name, "content": "", "keyword_hits": [], "score": 1.0}
    base.update(overrides)
    return base


def test_rerank_external_smes_with_llm_returns_empty_immediately_for_empty_input():
    with patch("api.icp.anthropic_client.generate_structured_narrative") as mock_generate:
        result = sm.rerank_external_smes_with_llm([], problem_context="anything")

    assert result == []
    mock_generate.assert_not_called()


def test_rerank_external_smes_with_llm_orders_per_the_llm_response_and_excludes_mis_filed_entries():
    matches = [
        _match("Generic Practus Engagement Letter — Client X", score=2.0),
        _match("Wine Industry Specialists LLP", score=1.0),
    ]

    with patch(
        "api.icp.anthropic_client.generate_structured_narrative",
        return_value={"selected": [
            {"name": "Wine Industry Specialists LLP", "rationale": "Genuine external partner, directly relevant sector."},
        ]},
    ):
        result = sm.rerank_external_smes_with_llm(matches, problem_context="Sula Wines growth")

    assert len(result) == 1
    assert result[0]["name"] == "Wine Industry Specialists LLP"
    assert result[0]["llm_rationale"] == "Genuine external partner, directly relevant sector."


def test_rerank_external_smes_with_llm_falls_back_to_deterministic_ranking_on_failure():
    matches = [_match("Wine Industry Specialists LLP", score=1.0)]

    with patch(
        "api.icp.anthropic_client.generate_structured_narrative",
        side_effect=RuntimeError("truncated"),
    ):
        result = sm.rerank_external_smes_with_llm(matches, problem_context="Sula Wines growth")

    assert result == matches[:3]


def test_rerank_external_smes_with_llm_falls_back_when_it_hallucinates_a_name():
    matches = [_match("Wine Industry Specialists LLP", score=1.0)]

    with patch(
        "api.icp.anthropic_client.generate_structured_narrative",
        return_value={"selected": [{"name": "Made Up Partner", "rationale": "invented"}]},
    ):
        result = sm.rerank_external_smes_with_llm(matches, problem_context="Sula Wines growth")

    assert result == matches[:3]

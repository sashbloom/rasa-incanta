# Copied from icp-bot/backend/tests/test_ownership_classifier.py; only import paths are adapted.
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.icp import ownership_classifier as oc
from api.icp.models import EvidenceLabel, OwnershipControl


def test_build_prompt_includes_company_and_entity_names():
    prompt = oc.build_prompt("Sobha", "Sobha Limited")

    assert "Sobha Limited" in prompt
    assert "Sobha" in prompt
    assert "PF (promoter/family" in prompt


def test_build_prompt_warns_against_confusing_a_professional_ceo_with_control_type():
    """Real bug (Livpure Smart Homes, 2026-09-10): a company with a family-
    trust-controlled ownership structure (the real reference confirmed
    "Navodhyam Trust held 48.17%", founder holds a single personal share,
    professional CEO Rakesh Kaul runs day-to-day) was misclassified as SI
    with confidence="Fact" and zero supporting shareholding/trust research
    -- likely inferring control type from "there's a professional CEO"
    rather than from real ownership evidence."""
    prompt = oc.build_prompt("Livpure Smart Homes", "Livpure Smart Homes Private Limited")

    assert "family trust structure" in prompt
    assert "NOT evidence of `control` type on its own" in prompt
    assert 'confidence="Fact"' in prompt
    assert "confidence=\"Inference\" at best, never \"Fact\"" in prompt


def test_parse_response_maps_valid_control_and_derives_disclosure():
    payload = json.dumps({
        "geography": "india",
        "is_listed": True,
        "is_pe_vc_funded": False,
        "control": "PF",
        "management": "Professionally managed",
        "confidence": "Fact",
        "rationale": "Promoter holds 52.9%.",
        "sources": [{"claim": "Promoter holds 52.9%", "source": "Screener.in", "url": None, "date": "2025-12"}],
    })

    result = oc.parse_response(payload)

    assert result.control == OwnershipControl.PF
    assert result.disclosure == "Listed"
    assert result.is_listed is True
    assert result.confidence == EvidenceLabel.FACT
    assert result.sources[0].source == "Screener.in"


def test_parse_response_maps_ipo_track_true():
    payload = json.dumps({
        "geography": "india", "is_listed": False, "is_pe_vc_funded": True,
        "control": "SC", "management": "Professionally managed", "ipo_track": True,
        "confidence": "Fact", "rationale": "Filed DRHP with SEBI.",
        "sources": [{"claim": "Filed DRHP", "source": "SEBI", "url": None, "date": "2026-01"}],
    })

    result = oc.parse_response(payload)

    assert result.ipo_track is True


def test_parse_response_defaults_ipo_track_to_false_when_omitted():
    payload = json.dumps({
        "geography": "india", "is_listed": True, "is_pe_vc_funded": False,
        "control": "PF", "management": "Professionally managed",
        "confidence": "Fact", "rationale": "test", "sources": [],
    })

    result = oc.parse_response(payload)

    assert result.ipo_track is False


def test_parse_response_derives_unlisted_disclosure():
    payload = json.dumps({
        "geography": "india", "is_listed": False, "is_pe_vc_funded": True,
        "control": "SC", "management": "Professionally managed", "confidence": "Inference",
        "rationale": "", "sources": [],
    })

    result = oc.parse_response(payload)

    assert result.disclosure == "Unlisted"
    assert result.control == OwnershipControl.SC


def test_parse_response_falls_back_to_wh_and_data_gap_on_invalid_control():
    payload = json.dumps({
        "geography": "other", "is_listed": False, "is_pe_vc_funded": False,
        "control": "NOT_A_REAL_CONTROL_TYPE", "management": "Owner-managed", "confidence": "Fact",
        "rationale": "some rationale", "sources": [],
    })

    result = oc.parse_response(payload)

    assert result.control == OwnershipControl.WH
    assert result.confidence == EvidenceLabel.DATA_GAP
    assert "invalid control type" in result.rationale.lower()


def test_parse_response_defaults_to_wh_when_confidence_is_data_gap_even_with_a_valid_guess():
    # icp-skill.md scenario 48: a schema-valid but low-confidence guess
    # (the model's own "best inference of last resort") must not become the
    # scored control type -- default to WH vocabulary instead.
    payload = json.dumps({
        "geography": "other", "is_listed": False, "is_pe_vc_funded": False,
        "control": "PF", "management": "Owner-managed", "confidence": "Data gap",
        "rationale": "No shareholding data found; guessing based on entity name only.", "sources": [],
    })

    result = oc.parse_response(payload)

    assert result.control == OwnershipControl.WH
    assert result.confidence == EvidenceLabel.DATA_GAP


def test_classify_uses_exa_search_and_synthesize(monkeypatch):
    with patch.object(oc.exa_search, "search_and_synthesize", return_value=(
        {
            "geography": "india", "is_listed": True, "is_pe_vc_funded": False,
            "control": "PF", "management": "Professionally managed", "confidence": "Fact",
            "rationale": "test",
        },
        [],
    )) as mock_search:
        result = oc.classify("Sobha", "Sobha Limited")

    assert result.control == OwnershipControl.PF
    call_kwargs = mock_search.call_args.kwargs
    assert call_kwargs["output_schema"] == oc.CLASSIFICATION_SCHEMA
    assert call_kwargs["search_type"] == "deep"
    assert "Sobha Limited" in mock_search.call_args.args[0]


def test_classify_maps_exas_grounding_into_sources():
    """Exa's outputSchema synthesis returns its own per-field citations
    (output.grounding) instead of the model authoring a `sources` array
    itself -- classify() must map that real structured evidence onto the
    same OwnershipSource shape every existing render template expects."""
    grounding = [
        {"field": "control", "citations": [{"url": "https://screener.in/x", "title": "Screener.in"}], "confidence": "high"},
    ]
    with patch.object(oc.exa_search, "search_and_synthesize", return_value=(
        {
            "geography": "india", "is_listed": True, "is_pe_vc_funded": False,
            "control": "PF", "management": "Professionally managed", "confidence": "Fact",
            "rationale": "test",
        },
        grounding,
    )):
        result = oc.classify("Sobha", "Sobha Limited")

    assert len(result.sources) == 1
    assert result.sources[0].source == "Screener.in"
    assert result.sources[0].url == "https://screener.in/x"


def test_classify_propagates_exa_errors():
    with patch.object(oc.exa_search, "search_and_synthesize", side_effect=oc.exa_search.ExaError("boom")):
        with pytest.raises(oc.exa_search.ExaError, match="boom"):
            oc.classify("Sobha", "Sobha Limited")


def test_build_prompt_requires_checking_a_shareholding_table_against_change_of_control():
    """Real bug (Manappuram Finance, 2026-09-11): classify() read a June-2026
    shareholding table showing promoters at 41.66%, returned PF with
    confidence="Fact", and never surfaced that Bain Capital held RBI approval
    to acquire ~41.7% and take control. PF and SC give diametrically opposite
    pitch advice (verdicts.ARCHETYPE_PLAYBOOK: "never use exit vocabulary"
    vs "IRR and multiple expansion"), so this is not a cosmetic miss."""
    prompt = oc.build_prompt("Manappuram Finance", "Manappuram Finance Limited")

    assert "SHAREHOLDING TABLE IS A SNAPSHOT" in prompt
    assert "change-of-control event in the last 18 months" in prompt
    assert "POST-TRANSACTION structure" in prompt
    # A primary-source table alone must no longer be enough to claim "Fact".
    assert "uncorroborated against more recent events" in prompt


def test_build_prompt_keeps_a_pe_co_promoter_out_of_the_jc_bucket():
    """JC's playbook is written for two OPERATING parents ("map both parents'
    objectives", "phase to the slower parent"). A PE fund sitting as
    co-promoter beside a founder under an SHA is SC. Confirmed live: without
    this, the Manappuram post-Bain structure classified as JC."""
    prompt = oc.build_prompt("Manappuram Finance", "Manappuram Finance Limited")

    assert "JC vs SC when a FUND is involved" in prompt
    assert "not a joint venture" in prompt.lower()
    # A fund routinely buys through a vehicle whose name doesn't say "fund".
    assert "investment vehicles" in prompt


def test_classify_asks_for_change_of_control_not_just_the_shareholding_pattern():
    """The prompt instructing the model to weigh a transaction over a stale
    table is useless if the RETRIEVAL only ever fetches the table -- confirmed
    live that the shareholding-only query returned 24 grounded citations and
    none of them mentioned the acquirer."""
    captured = {}

    def _capture(query, **kwargs):
        captured["query"] = query
        return (
            {"geography": "india", "is_listed": True, "is_pe_vc_funded": True, "control": "SC",
             "management": "Professionally managed", "confidence": "Fact", "rationale": "test"},
            [],
        )

    with patch.object(oc.exa_search, "search_and_synthesize", side_effect=_capture):
        oc.classify("Manappuram Finance", "Manappuram Finance Limited")

    query = captured["query"]
    assert "change of control" in query
    assert "regulatory approval" in query
    # and it must still ask the original question too
    assert "shareholding pattern" in query

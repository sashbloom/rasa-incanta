# Copied from icp-bot/backend/tests/test_practus_history.py; only import paths are adapted.
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.icp.practus_history import (
    classify,
    confirmed_client_industry_breakdown,
    confirmed_clients_by_keyword_context,
    confirmed_clients_in_industry,
    deduplicate_client_names,
    filter_sector_irrelevant_names,
    is_generic_industry_value,
    normalize_name,
)

NOW = datetime(2026, 8, 20, tzinfo=timezone.utc)


def test_is_generic_industry_value_flags_the_known_placeholders():
    assert is_generic_industry_value("Others") is True
    assert is_generic_industry_value("other") is True
    assert is_generic_industry_value(None) is True
    assert is_generic_industry_value("") is True
    assert is_generic_industry_value("Real Estate") is False


def test_normalize_name_strips_legal_suffixes_and_whitespace():
    assert normalize_name("\nAcme Pvt Ltd") == "acme"
    assert normalize_name("Acme Private Limited") == "acme"
    assert normalize_name("ACME LIMITED  ") == "acme"
    assert normalize_name("Acme Inc.") == "acme"


def test_normalize_name_matches_across_variant_spellings():
    assert normalize_name("Acme Pvt Ltd") == normalize_name("Acme Private Limited")


def test_normalize_name_collapses_recon_and_mgt_noise_tokens():
    """Real bug (Manappuram Finance, 2026-09-09): 'Sanctum Wealth Mgt' /
    'Sanctum Wealth Private Limited' / 'Sanctum Wealth Private Limited
    Recon' counted as 3 separate confirmed clients instead of 1 -- 'Recon'
    is Zoho's own naming convention for a reconciliation/duplicate account,
    'Mgt' is just an abbreviation, neither is distinguishing information."""
    assert normalize_name("Sanctum Wealth Mgt") == "sanctum wealth"
    assert normalize_name("Sanctum Wealth Private Limited") == "sanctum wealth"
    assert normalize_name("Sanctum Wealth Private Limited Recon") == "sanctum wealth"


@patch("api.icp.practus_history._load_client_names_index")
@patch("api.icp.practus_history.zoho_db.find_lost_deals_by_account_ids")
@patch("api.icp.practus_history.zoho_db.find_won_deals_by_account_ids")
def test_classify_both_sources_gives_source_both(mock_won, mock_lost, mock_index):
    mock_won.return_value = [{"id": 1}]
    mock_lost.return_value = []
    mock_index.return_value = {"acme": {"client_name": "Acme Ltd"}}

    result = classify(["Acme Ltd"], [123])

    assert result.is_practus_client is True
    assert result.source == "both"
    assert result.client_lost_count == 0


@patch("api.icp.practus_history._load_client_names_index")
@patch("api.icp.practus_history.zoho_db.find_lost_deals_by_account_ids")
@patch("api.icp.practus_history.zoho_db.find_won_deals_by_account_ids")
def test_classify_neither_source_gives_source_none(mock_won, mock_lost, mock_index):
    mock_won.return_value = []
    mock_lost.return_value = []
    mock_index.return_value = {}

    result = classify(["Nobody Ever Heard Of Ltd"], [123])

    assert result.is_practus_client is False
    assert result.source is None


@patch("api.icp.practus_history._load_client_names_index")
@patch("api.icp.practus_history.zoho_db.find_lost_deals_by_account_ids")
@patch("api.icp.practus_history.zoho_db.find_won_deals_by_account_ids")
def test_classify_counts_lost_deals_and_unexplained_recent_losses(mock_won, mock_lost, mock_index):
    mock_won.return_value = []
    mock_lost.return_value = [
        {"reason_for_loss": "CFO moved on", "specify_reason_for_lost": None, "date_client_lost": (NOW - timedelta(days=30)).date()},
        {"reason_for_loss": None, "specify_reason_for_lost": None, "date_client_lost": (NOW - timedelta(days=30)).date()},
    ]
    mock_index.return_value = {}

    result = classify(["Some Co"], [123], now=NOW)

    assert result.client_lost_count == 2
    assert result.client_lost_unexplained_count == 1


@patch("api.icp.practus_history._load_client_names_index")
@patch("api.icp.practus_history.zoho_db.find_lost_deals_by_account_ids")
@patch("api.icp.practus_history.zoho_db.find_won_deals_by_account_ids")
def test_classify_gate_3_fires_per_loss_not_all_or_nothing(mock_won, mock_lost, mock_index):
    """Scenario (a) from the Gate 3 bug fix: 3 total losses, 1 has a reason,
    2 don't, all recent. The OLD `reasons_captured == 0` check would have
    required EVERY loss to lack a reason (so this case would NOT have
    fired). The correct, per-loss reading of icp-skill.md line 430 counts
    each loss that individually lacks a reason -- 2 of them do, so this
    should reach the 2+ threshold."""
    mock_won.return_value = []
    mock_lost.return_value = [
        {"reason_for_loss": "Lost on price", "specify_reason_for_lost": None, "date_client_lost": (NOW - timedelta(days=10)).date()},
        {"reason_for_loss": None, "specify_reason_for_lost": None, "date_client_lost": (NOW - timedelta(days=20)).date()},
        {"reason_for_loss": None, "specify_reason_for_lost": None, "date_client_lost": (NOW - timedelta(days=30)).date()},
    ]
    mock_index.return_value = {}

    result = classify(["Some Co"], [123], now=NOW)

    assert result.client_lost_count == 3
    assert result.client_lost_unexplained_count == 2


@patch("api.icp.practus_history._load_client_names_index")
@patch("api.icp.practus_history.zoho_db.find_lost_deals_by_account_ids")
@patch("api.icp.practus_history.zoho_db.find_won_deals_by_account_ids")
def test_classify_treats_generic_others_reason_as_unexplained(mock_won, mock_lost, mock_index):
    """Live-confirmed against the real CRM: `reason_for_loss` defaults to
    the picklist placeholder 'Others' on essentially every Client Lost deal
    (Sula's three included), which is not a captured reason -- the real
    reason, when one exists, lives in `specify_reason_for_lost`. Before this
    fix, 'Others' alone (a non-empty string) short-circuited the old
    `reason_for_loss or specify_reason_for_lost` check to always be
    "explained", so Gate 3 could never fire CRM-wide."""
    mock_won.return_value = []
    mock_lost.return_value = [
        {"reason_for_loss": "Others", "specify_reason_for_lost": "CFO Moved on", "date_client_lost": (NOW - timedelta(days=10)).date()},
        {"reason_for_loss": "Others", "specify_reason_for_lost": None, "date_client_lost": (NOW - timedelta(days=20)).date()},
        {"reason_for_loss": "Others", "specify_reason_for_lost": None, "date_client_lost": (NOW - timedelta(days=30)).date()},
    ]
    mock_index.return_value = {}

    result = classify(["Some Co"], [123], now=NOW)

    assert result.client_lost_count == 3
    assert result.client_lost_unexplained_count == 2


@patch("api.icp.practus_history._load_client_names_index")
@patch("api.icp.practus_history.zoho_db.find_lost_deals_by_account_ids")
@patch("api.icp.practus_history.zoho_db.find_won_deals_by_account_ids")
def test_classify_a_real_specify_reason_still_counts_as_explained_even_with_others_bucket(mock_won, mock_lost, mock_index):
    mock_won.return_value = []
    mock_lost.return_value = [
        {"reason_for_loss": "Others", "specify_reason_for_lost": "Budget frozen", "date_client_lost": (NOW - timedelta(days=10)).date()},
    ]
    mock_index.return_value = {}

    result = classify(["Some Co"], [123], now=NOW)

    assert result.client_lost_count == 1
    assert result.client_lost_unexplained_count == 0


@patch("api.icp.practus_history._load_client_names_index")
@patch("api.icp.practus_history.zoho_db.find_lost_deals_by_account_ids")
@patch("api.icp.practus_history.zoho_db.find_won_deals_by_account_ids")
def test_classify_excludes_losses_older_than_24_months(mock_won, mock_lost, mock_index):
    """Scenario 17 / (b): 2 unexplained losses, but both dated more than 24
    months (730 days) ago -- neither should count toward Gate 3's
    threshold."""
    mock_won.return_value = []
    mock_lost.return_value = [
        {"reason_for_loss": None, "specify_reason_for_lost": None, "date_client_lost": (NOW - timedelta(days=800)).date()},
        {"reason_for_loss": None, "specify_reason_for_lost": None, "date_client_lost": (NOW - timedelta(days=900)).date()},
    ]
    mock_index.return_value = {}

    result = classify(["Some Co"], [123], now=NOW)

    assert result.client_lost_count == 2
    assert result.client_lost_unexplained_count == 0


@patch("api.icp.practus_history._load_client_names_index")
@patch("api.icp.practus_history.zoho_db.find_lost_deals_by_account_ids")
@patch("api.icp.practus_history.zoho_db.find_won_deals_by_account_ids")
def test_classify_one_recent_one_stale_unexplained_loss_stays_under_threshold(mock_won, mock_lost, mock_index):
    """(c): 2 unexplained losses, only one within the last 24 months -- only
    the recent one counts, leaving just 1 qualifying loss (below the 2+
    threshold Gate 3 needs)."""
    mock_won.return_value = []
    mock_lost.return_value = [
        {"reason_for_loss": None, "specify_reason_for_lost": None, "date_client_lost": (NOW - timedelta(days=100)).date()},
        {"reason_for_loss": None, "specify_reason_for_lost": None, "date_client_lost": (NOW - timedelta(days=800)).date()},
    ]
    mock_index.return_value = {}

    result = classify(["Some Co"], [123], now=NOW)

    assert result.client_lost_count == 2
    assert result.client_lost_unexplained_count == 1


@patch("api.icp.practus_history._load_client_names_index")
@patch("api.icp.practus_history.zoho_db.find_lost_deals_by_account_ids")
@patch("api.icp.practus_history.zoho_db.find_won_deals_by_account_ids")
def test_classify_two_recent_unexplained_losses_baseline_scenario_16(mock_won, mock_lost, mock_index):
    """(d): the baseline scenario 16 case -- 2 recent losses, neither with a
    reason captured -- must still reach the 2+ threshold."""
    mock_won.return_value = []
    mock_lost.return_value = [
        {"reason_for_loss": None, "specify_reason_for_lost": None, "date_client_lost": (NOW - timedelta(days=10)).date()},
        {"reason_for_loss": None, "specify_reason_for_lost": None, "date_client_lost": (NOW - timedelta(days=200)).date()},
    ]
    mock_index.return_value = {}

    result = classify(["Some Co"], [123], now=NOW)

    assert result.client_lost_count == 2
    assert result.client_lost_unexplained_count == 2


@patch("api.icp.practus_history._load_client_names_index")
def test_classify_checks_every_name_variant_against_xlsx(mock_index):
    mock_index.return_value = {"shobha": {"client_name": "Shobha Limited"}}
    with patch("api.icp.practus_history.zoho_db.find_won_deals_by_account_ids", return_value=[]), \
         patch("api.icp.practus_history.zoho_db.find_lost_deals_by_account_ids", return_value=[]):
        result = classify(["Sobha Ltd.", "Shobha Limited"], [1, 2])

    assert result.is_practus_client is True
    assert result.source == "client_names_file"


@patch("api.icp.practus_history._load_client_names_index")
@patch("api.icp.practus_history.zoho_db.find_won_deals_by_industry")
def test_confirmed_clients_in_industry_unions_and_dedupes_both_sources(mock_zoho_industry, mock_index):
    """icp-skill.md P1: "Counts ONLY confirmed Practus clients — Zoho
    Client Won + the Client Names file" -- previously nothing computed this
    real count at all, so P1 always scored off zero evidence."""
    mock_zoho_industry.return_value = [
        {"account_name": "Alpha Retail Ltd"},
        {"account_name": "Beta Stores Ltd"},
    ]
    mock_index.return_value = {
        "beta stores": {"client_name": "Beta Stores Ltd", "industry": "Retail"},
        "gamma mart": {"client_name": "Gamma Mart", "industry": "Retail"},
        "delta pharma": {"client_name": "Delta Pharma", "industry": "Pharma"},
    }

    names = confirmed_clients_in_industry("Retail")

    assert set(names) == {"Alpha Retail Ltd", "Beta Stores Ltd", "Gamma Mart"}
    mock_zoho_industry.assert_called_once_with("Retail")


def test_confirmed_clients_in_industry_returns_empty_for_no_industry():
    assert confirmed_clients_in_industry(None) == []
    assert confirmed_clients_in_industry("") == []


@patch("api.icp.practus_history._load_client_names_index")
@patch("api.icp.practus_history.zoho_db.find_won_deals_by_industry")
def test_confirmed_clients_in_industry_excludes_leftover_prospect_named_accounts(mock_zoho_industry, mock_index):
    """Real bug (Manappuram Finance, 2026-09-09): 'Blue Foods CFO
    Recruitment - Prospect' is a real account whose `stage` IS 'Client Won'
    in the DB (so the SQL stage filter can't catch it) but whose own
    account_name is a leftover artifact from when it was still a live
    prospect, never renamed on conversion."""
    mock_zoho_industry.return_value = [
        {"account_name": "Alpha Finance Ltd"},
        {"account_name": "Blue Foods CFO Recruitment - Prospect"},
    ]
    mock_index.return_value = {}

    names = confirmed_clients_in_industry("BFSI")

    assert names == ["Alpha Finance Ltd"]


@patch("api.icp.practus_history._load_client_names_index")
@patch("api.icp.practus_history.zoho_db.find_won_deals_by_industry")
def test_confirmed_clients_in_industry_collapses_recon_and_mgt_name_variants(mock_zoho_industry, mock_index):
    """Real bug (Manappuram Finance, 2026-09-09): 3 account-name variants of
    the same underlying client inflated the confirmed-client count by 2."""
    mock_zoho_industry.return_value = [
        {"account_name": "Sanctum Wealth Mgt"},
        {"account_name": "Sanctum Wealth Private Limited"},
        {"account_name": "Sanctum Wealth Private Limited Recon"},
    ]
    mock_index.return_value = {}

    names = confirmed_clients_in_industry("BFSI")

    assert names == ["Sanctum Wealth Mgt"]


def test_confirmed_clients_in_industry_treats_generic_bucket_values_as_no_match():
    """Confirmed live against the real CRM: `industry_type = 'Others'` is a
    generic catch-all 638 completely unrelated won deals share (a
    car-detailing shop, a mental-health nonprofit, an industrial-parts
    distributor, ...) -- pooling it as a genuine industry match inflated P1
    to a false top-band score. Must never even query the DB for these."""
    from unittest.mock import patch as _patch

    for value in ("Others", "other", "OTHERS", "N/A", "unknown", "General"):
        with _patch("api.icp.practus_history.zoho_db.find_won_deals_by_industry") as mock_query:
            assert confirmed_clients_in_industry(value) == []
            mock_query.assert_not_called()


@patch("api.icp.practus_history._load_client_names_index")
@patch("api.icp.practus_history.zoho_db.find_lost_deals_by_account_ids")
@patch("api.icp.practus_history.zoho_db.find_won_deals_by_account_ids")
@patch("api.icp.practus_history.zoho_db.find_won_deals_by_industry")
def test_classify_threads_industry_client_names_onto_the_result(mock_zoho_industry, mock_won, mock_lost, mock_index):
    mock_won.return_value = []
    mock_lost.return_value = []
    mock_index.return_value = {}
    mock_zoho_industry.return_value = [{"account_name": "Alpha Retail Ltd"}]

    result = classify(["Test Co"], [123], industry="Retail")

    assert result.industry_confirmed_client_names == ["Alpha Retail Ltd"]


@patch("api.icp.practus_history._load_client_names_index")
@patch("api.icp.practus_history.zoho_db.find_all_won_deals")
def test_confirmed_client_industry_breakdown_counts_across_both_sources(mock_won, mock_index):
    """Live-confirmed real gap: the reference report's own honest fallback
    for a wine-industry deal with zero exact matches was "Twenty-four
    clients across FMCG, retail, consumer goods and hospitality. None in
    alcoholic beverages." -- our narrative had no way to say this since
    confirmed_clients_in_industry() only ever returns an exact-match list."""
    mock_won.return_value = [
        {"account_name": "Alpha FMCG Ltd", "industry_type": "FMCG"},
        {"account_name": "Beta Retail Ltd", "industry_type": "Retail"},
        {"account_name": "Gamma FMCG Ltd", "industry_type": "FMCG"},
    ]
    mock_index.return_value = {
        "delta hospitality": {"client_name": "Delta Hospitality", "industry": "Hospitality"},
    }

    breakdown = confirmed_client_industry_breakdown()

    assert breakdown == {"FMCG": 2, "Retail": 1, "Hospitality": 1}


@patch("api.icp.practus_history._load_client_names_index")
@patch("api.icp.practus_history.zoho_db.find_all_won_deals")
def test_confirmed_client_industry_breakdown_excludes_generic_bucket(mock_won, mock_index):
    mock_won.return_value = [
        {"account_name": "Alpha Ltd", "industry_type": "Others"},
        {"account_name": "Beta Ltd", "industry_type": "Real Estate"},
    ]
    mock_index.return_value = {}

    breakdown = confirmed_client_industry_breakdown()

    assert breakdown == {"Real Estate": 1}


@patch("api.icp.anthropic_client.generate_structured_narrative")
@patch("api.icp.practus_history.confirmed_clients_in_industry")
@patch("api.icp.practus_history.confirmed_client_industry_breakdown")
def test_confirmed_clients_by_keyword_context_finds_a_real_sector_proxy_match(mock_breakdown, mock_in_industry, mock_llm):
    """Live-confirmed real gap: "Theobroma Foods Private Limited" (tagged
    FMCG in the Client Names file) never surfaced as a credential for a
    wine/beverage prospect because the CRM's own industry_type is the
    generic 'Others' -- confirmed_clients_in_industry() can only compare
    against that broken field. Secondary research naming the company's
    real sector ("FMCG") should find this real confirmed client instead."""
    mock_breakdown.return_value = {"FMCG": 3, "Real Estate": 12}
    mock_in_industry.return_value = ["Theobroma Foods Private Limited", "Other FMCG Co", "Third FMCG Co"]
    mock_llm.return_value = {"matched_industry": "FMCG", "rationale": "The text describes a wine/beverage company in FMCG."}

    industry, clients = confirmed_clients_by_keyword_context(
        "Sula Wines is a wine and beverage company operating in the FMCG sector."
    )

    assert industry == "FMCG"
    assert "Theobroma Foods Private Limited" in clients
    mock_in_industry.assert_called_once_with("FMCG")


@patch("api.icp.anthropic_client.generate_structured_narrative")
@patch("api.icp.practus_history.confirmed_clients_in_industry")
@patch("api.icp.practus_history.confirmed_client_industry_breakdown")
def test_confirmed_clients_by_keyword_context_only_accepts_a_label_from_the_real_list(mock_breakdown, mock_in_industry, mock_llm):
    """The model must copy its answer verbatim from the real candidate
    list -- if it returns something not in that list (a paraphrase, an
    invented label, a near-miss spelling), treat it as no match rather
    than trusting an unverifiable string."""
    mock_breakdown.return_value = {"FMCG": 3, "Real Estate": 12}
    mock_llm.return_value = {"matched_industry": "Beverages", "rationale": "Close enough to FMCG."}

    industry, clients = confirmed_clients_by_keyword_context("A beverage company.")

    assert industry is None
    assert clients == []
    mock_in_industry.assert_not_called()


@patch("api.icp.anthropic_client.generate_structured_narrative")
@patch("api.icp.practus_history.confirmed_clients_in_industry")
@patch("api.icp.practus_history.confirmed_client_industry_breakdown")
def test_confirmed_clients_by_keyword_context_honors_a_genuine_no_match(mock_breakdown, mock_in_industry, mock_llm):
    """Live-confirmed real bugs (Indegene 2026-09-04, Sula Wines and
    Uniparts India 2026-09-09): a pure keyword/inverse-document-frequency
    matcher repeatedly produced confident, wrong matches ("Real Estate" for
    a wine company, "Consumer goods industry" for an auto-components
    company) because a label's own words happened to be ordinary English
    that shows up incidentally in any long narrative. Replacing that
    arithmetic with an LLM call means the model can correctly say "none of
    these genuinely fit" instead of being forced to pick the least-bad
    keyword overlap."""
    mock_breakdown.return_value = {"Real Estate": 12, "BFSI": 5}
    mock_llm.return_value = {"matched_industry": None, "rationale": "Neither label describes this company's real business."}

    industry, clients = confirmed_clients_by_keyword_context(
        "The company discusses real growth in its BFSI-adjacent fintech lending book."
    )

    assert industry is None
    assert clients == []
    mock_in_industry.assert_not_called()


@patch("api.icp.anthropic_client.generate_structured_narrative")
@patch("api.icp.practus_history.confirmed_clients_in_industry")
@patch("api.icp.practus_history.confirmed_client_industry_breakdown")
def test_confirmed_clients_by_keyword_context_degrades_to_no_match_on_llm_failure(mock_breakdown, mock_in_industry, mock_llm):
    """A failed classification call must degrade to an honest "no match"
    (a data gap), never crash the run and never fall back to a keyword
    guess -- there is no deterministic fallback here on purpose, since
    that fallback was the very source of the confident-wrong-match bugs
    this replaced."""
    mock_breakdown.return_value = {"FMCG": 3}
    mock_llm.side_effect = RuntimeError("rate limited")

    industry, clients = confirmed_clients_by_keyword_context("Some company research text.")

    assert industry is None
    assert clients == []
    mock_in_industry.assert_not_called()


@patch("api.icp.anthropic_client.generate_structured_narrative")
@patch("api.icp.practus_history.confirmed_client_industry_breakdown")
def test_confirmed_clients_by_keyword_context_returns_nothing_when_no_industries_exist(mock_breakdown, mock_llm):
    mock_breakdown.return_value = {}

    industry, clients = confirmed_clients_by_keyword_context("A completely unrelated deal about widgets.")

    assert industry is None
    assert clients == []
    mock_llm.assert_not_called()


def test_confirmed_clients_by_keyword_context_returns_nothing_for_empty_context():
    industry, clients = confirmed_clients_by_keyword_context("")

    assert industry is None
    assert clients == []


@patch("api.icp.anthropic_client.generate_structured_narrative")
def test_filter_sector_irrelevant_names_drops_names_the_llm_flags(mock_llm):
    """Real bug (Manappuram Finance, 2026-09-09): a law firm (Kobre & Kim),
    accounting firms, a hospital-focused PE deal, and outright nonprofits
    all counted as confirmed BFSI clients purely because their own deal's
    CRM industry_type field said so."""
    mock_llm.return_value = {"irrelevant_names": ["Kobre & Kim", "Michael & Susan Dell Foundation"]}

    result = filter_sector_irrelevant_names(
        "BFSI", ["Blackstone", "Kobre & Kim", "Michael & Susan Dell Foundation", "Sequoia Capital India"]
    )

    assert result == ["Blackstone", "Sequoia Capital India"]


@patch("api.icp.anthropic_client.generate_structured_narrative")
def test_filter_sector_irrelevant_names_keeps_the_full_list_when_nothing_flagged(mock_llm):
    mock_llm.return_value = {"irrelevant_names": []}

    result = filter_sector_irrelevant_names("BFSI", ["Blackstone", "Sequoia Capital India"])

    assert result == ["Blackstone", "Sequoia Capital India"]


@patch("api.icp.anthropic_client.generate_structured_narrative")
def test_filter_sector_irrelevant_names_degrades_to_the_full_list_on_llm_failure(mock_llm):
    """A missed false-positive is a much narrower harm than losing real,
    correct client evidence entirely -- same fail-open philosophy as
    confirmed_clients_by_keyword_context()."""
    mock_llm.side_effect = RuntimeError("rate limited")

    result = filter_sector_irrelevant_names("BFSI", ["Blackstone", "Kobre & Kim"])

    assert result == ["Blackstone", "Kobre & Kim"]


def test_filter_sector_irrelevant_names_returns_empty_for_empty_input():
    assert filter_sector_irrelevant_names("BFSI", []) == []


@patch("api.icp.anthropic_client.generate_structured_narrative")
def test_filter_sector_irrelevant_names_deterministic_backstop_catches_what_the_llm_missed(mock_llm):
    """Real bug (Manappuram Finance, 2026-09-10): a live LLM sector-
    relevance call missed even the most obvious cases on this run --
    "Michael & Susan Dell Foundation" and "FRR Immigration" both survived
    despite carrying words no genuine BFSI client would have. A small,
    safe, deterministic keyword backstop must catch these regardless of
    what the (non-deterministic) LLM call itself returns."""
    mock_llm.return_value = {"irrelevant_names": []}  # the LLM missed both this run

    result = filter_sector_irrelevant_names(
        "BFSI", ["Blackstone", "Michael & Susan Dell Foundation", "FRR Immigration", "FRR Shares & Securities"]
    )

    assert result == ["Blackstone", "FRR Shares & Securities"]


@patch("api.icp.anthropic_client.generate_structured_narrative")
def test_filter_sector_irrelevant_names_deterministic_backstop_still_applies_on_llm_failure(mock_llm):
    mock_llm.side_effect = RuntimeError("rate limited")

    result = filter_sector_irrelevant_names("BFSI", ["Blackstone", "Some Charitable Trust"])

    assert result == ["Blackstone"]


def test_filter_sector_irrelevant_names_deterministic_backstop_skips_a_nonprofit_target_industry():
    """A genuine nonprofit-sector client shouldn't be excluded by its own
    industry's defining word."""
    with patch("api.icp.anthropic_client.generate_structured_narrative") as mock_llm:
        mock_llm.return_value = {"irrelevant_names": []}
        result = filter_sector_irrelevant_names("Nonprofit / NGO", ["Some Real Foundation", "Blackstone"])

    assert result == ["Some Real Foundation", "Blackstone"]
    mock_llm.assert_called_once()


def test_filter_sector_irrelevant_names_does_not_flag_legitimate_trust_or_society_names():
    """"Trust" and "Society" are deliberately excluded from the
    deterministic keyword list -- both have real, legitimate BFSI meanings
    (unit trusts, REITs, cooperative credit societies) a keyword match
    would wrongly exclude."""
    with patch("api.icp.anthropic_client.generate_structured_narrative") as mock_llm:
        mock_llm.return_value = {"irrelevant_names": []}
        result = filter_sector_irrelevant_names("BFSI", ["ABC Unit Trust", "XYZ Cooperative Credit Society"])

    assert result == ["ABC Unit Trust", "XYZ Cooperative Credit Society"]


@patch("api.icp.anthropic_client.generate_structured_narrative")
def test_deduplicate_client_names_collapses_a_confirmed_name_variant_pair(mock_llm):
    """Real bug (Sobha, 2026-09-09): "Vianaar Homes Private Limited" and
    "Vianaar" counted as 2 separate confirmed clients -- normalize_name()'s
    legal-suffix/noise-token stripping can't catch this (one is a genuine
    abbreviation, not just a suffix difference), so it needs real judgment."""
    mock_llm.return_value = {"duplicate_groups": [
        {"canonical_name": "Vianaar Homes Private Limited", "duplicate_names": ["Vianaar"]},
    ]}

    result = deduplicate_client_names(["Vianaar Homes Private Limited", "Vianaar", "Casa Grande"])

    assert result == ["Vianaar Homes Private Limited", "Casa Grande"]


@patch("api.icp.anthropic_client.generate_structured_narrative")
def test_deduplicate_client_names_does_not_force_a_grouping_when_genuinely_unsure(mock_llm):
    """Real bug (Sobha, 2026-09-09): "Mani Group" and "Mani Realty Projects
    Private Limited" share only a root word -- they may be the same company
    or two different real entities under one family/conglomerate. The model
    correctly returning an empty group list must be honored, not overridden."""
    mock_llm.return_value = {"duplicate_groups": []}

    result = deduplicate_client_names(["Mani Group", "Mani Realty Projects Private Limited"])

    assert result == ["Mani Group", "Mani Realty Projects Private Limited"]


@patch("api.icp.anthropic_client.generate_structured_narrative")
def test_deduplicate_client_names_ignores_a_hallucinated_name_not_in_the_list(mock_llm):
    mock_llm.return_value = {"duplicate_groups": [
        {"canonical_name": "Not A Real Candidate", "duplicate_names": ["Casa Grande"]},
    ]}

    result = deduplicate_client_names(["Casa Grande", "Vianaar"])

    assert result == ["Casa Grande", "Vianaar"]


@patch("api.icp.anthropic_client.generate_structured_narrative")
def test_deduplicate_client_names_degrades_to_the_full_list_on_llm_failure(mock_llm):
    mock_llm.side_effect = RuntimeError("rate limited")

    result = deduplicate_client_names(["Vianaar Homes Private Limited", "Vianaar"])

    assert result == ["Vianaar Homes Private Limited", "Vianaar"]


def test_deduplicate_client_names_skips_the_llm_call_for_fewer_than_two_names():
    assert deduplicate_client_names([]) == []
    assert deduplicate_client_names(["Solo Co"]) == ["Solo Co"]


@patch("api.icp.practus_history._load_client_names_index")
@patch("api.icp.practus_history.zoho_db.find_all_won_deals")
@patch("api.icp.practus_history.zoho_db.find_lost_deals_by_account_ids")
@patch("api.icp.practus_history.zoho_db.find_won_deals_by_account_ids")
@patch("api.icp.practus_history.zoho_db.find_won_deals_by_industry")
def test_classify_only_computes_breakdown_when_exact_industry_match_is_empty(
    mock_industry, mock_won, mock_lost, mock_all_won, mock_index
):
    mock_won.return_value = []
    mock_lost.return_value = []
    mock_index.return_value = {}
    mock_industry.return_value = [{"account_name": "Alpha Wine Ltd"}]

    result = classify(["Test Co"], [123], industry="Wine")

    assert result.industry_confirmed_client_names == ["Alpha Wine Ltd"]
    assert result.all_confirmed_clients_by_industry == {}
    mock_all_won.assert_not_called()


@patch("api.icp.practus_history._load_client_names_index")
@patch("api.icp.practus_history.zoho_db.find_all_won_deals")
@patch("api.icp.practus_history.zoho_db.find_lost_deals_by_account_ids")
@patch("api.icp.practus_history.zoho_db.find_won_deals_by_account_ids")
@patch("api.icp.practus_history.zoho_db.find_won_deals_by_industry")
def test_classify_computes_breakdown_when_exact_industry_match_is_empty(
    mock_industry, mock_won, mock_lost, mock_all_won, mock_index
):
    mock_won.return_value = []
    mock_lost.return_value = []
    mock_index.return_value = {}
    mock_industry.return_value = []
    mock_all_won.return_value = [{"account_name": "Alpha FMCG Ltd", "industry_type": "FMCG"}]

    result = classify(["Test Co"], [123], industry="Wine")

    assert result.industry_confirmed_client_names == []
    assert result.all_confirmed_clients_by_industry == {"FMCG": 1}

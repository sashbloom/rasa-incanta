# Copied from icp-bot/backend/tests/test_conflict_checker.py; only import paths are adapted.
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.icp import conflict_checker as cc


def _account(id_, name):
    return {"id": id_, "account_name": name, "modified_time": None}


def _deal(id_, name, stage):
    return {"id": id_, "deal_name": name, "stage": stage}


def test_identify_competitors_uses_exa_search_and_synthesize(monkeypatch):
    with patch.object(cc.exa_search, "search_and_synthesize", return_value=({"competitors": ["Rival Co", "Other Co"]}, [])) as mock_search:
        competitors = cc.identify_competitors("Test Co", industry_hint="Widgets")

    assert competitors == ["Rival Co", "Other Co"]
    call_kwargs = mock_search.call_args.kwargs
    assert call_kwargs["output_schema"] == cc.COMPETITOR_SCHEMA
    assert "Widgets" in mock_search.call_args.args[0]


def test_identify_competitors_propagates_exa_errors(monkeypatch):
    with patch.object(cc.exa_search, "search_and_synthesize", side_effect=cc.exa_search.ExaError("boom")):
        with pytest.raises(cc.exa_search.ExaError, match="boom"):
            cc.identify_competitors("Test Co")


def test_check_fires_when_a_confirmed_competitor_has_an_active_deal():
    with patch.object(cc, "identify_competitors", return_value=["Rival Co"]), \
         patch.object(cc.entity_resolver, "find_matching_accounts", return_value=[_account(1, "Rival Co")]), \
         patch.object(cc.zoho_db, "find_deals_by_account_ids", return_value=[_deal(10, "Rival Deal", "Qualified Prospect")]):
        result = cc.check("Test Co")

    assert result.active_competitor_pursuit is True
    assert result.competitor_name == "Rival Co"
    assert "Rival Co" in result.detail
    assert "Rival Deal" in result.detail


def test_check_does_not_fire_when_competitor_is_only_a_past_client():
    """icp-skill.md scenario #45: a delivered client, not an active
    pursuit, must NOT fire this gate."""
    with patch.object(cc, "identify_competitors", return_value=["Rival Co"]), \
         patch.object(cc.entity_resolver, "find_matching_accounts", return_value=[_account(1, "Rival Co")]), \
         patch.object(cc.zoho_db, "find_deals_by_account_ids", return_value=[_deal(10, "Old Deal", "Client Won")]):
        result = cc.check("Test Co")

    assert result.active_competitor_pursuit is False


def test_check_does_not_fire_on_a_lost_deal():
    with patch.object(cc, "identify_competitors", return_value=["Rival Co"]), \
         patch.object(cc.entity_resolver, "find_matching_accounts", return_value=[_account(1, "Rival Co")]), \
         patch.object(cc.zoho_db, "find_deals_by_account_ids", return_value=[_deal(10, "Old Deal", "Client Lost")]):
        result = cc.check("Test Co")

    assert result.active_competitor_pursuit is False


def test_check_ignores_a_merely_fuzzy_matched_competitor_account():
    """Same lesson as entity_resolver.py: a fuzzy name hit is not
    confirmation of identity -- "Rival Corp" fuzzy-matching "Rival Co"
    should not be treated as the same account without a tighter bar."""
    with patch.object(cc, "identify_competitors", return_value=["Rival Co"]), \
         patch.object(cc.entity_resolver, "find_matching_accounts", return_value=[_account(1, "Rival Corp International Holdings")]), \
         patch.object(cc.zoho_db, "find_deals_by_account_ids", return_value=[_deal(10, "Some Deal", "Qualified Prospect")]) as mock_deals:
        result = cc.check("Test Co")

    assert result.active_competitor_pursuit is False
    mock_deals.assert_not_called()  # never even queried an unconfirmed account's deals


def test_check_uses_pre_fetched_competitors_without_calling_identify_competitors_again():
    """Track A perf fix: orchestrator.py now kicks off identify_competitors()
    as an early background future (overlapping with the rest of evidence
    assembly) and hands the already-resolved list straight into `check()`
    via the new `competitors` param -- this must skip re-fetching entirely,
    not just ignore the pre-fetched list."""
    with patch.object(cc, "identify_competitors") as mock_identify, \
         patch.object(cc.entity_resolver, "find_matching_accounts", return_value=[_account(1, "Rival Co")]), \
         patch.object(cc.zoho_db, "find_deals_by_account_ids", return_value=[_deal(10, "Rival Deal", "Qualified Prospect")]):
        result = cc.check("Test Co", competitors=["Rival Co"])

    mock_identify.assert_not_called()
    assert result.active_competitor_pursuit is True
    assert result.competitor_name == "Rival Co"


def test_check_fetches_fresh_when_competitors_not_given():
    """`competitors=None` (the default) preserves the original behavior for
    every existing caller/test that doesn't know about the early-future
    optimization."""
    with patch.object(cc, "identify_competitors", return_value=["Rival Co"]) as mock_identify, \
         patch.object(cc.entity_resolver, "find_matching_accounts", return_value=[]):
        cc.check("Test Co", industry_hint="Retail")

    mock_identify.assert_called_once_with("Test Co", "Retail")


def test_check_returns_no_conflict_when_no_competitors_identified():
    with patch.object(cc, "identify_competitors", return_value=[]):
        result = cc.check("Test Co")

    assert result.active_competitor_pursuit is False
    assert result.competitor_name is None


def test_check_returns_no_conflict_when_competitor_has_no_zoho_account():
    with patch.object(cc, "identify_competitors", return_value=["Unknown Rival"]), \
         patch.object(cc.entity_resolver, "find_matching_accounts", return_value=[]):
        result = cc.check("Test Co")

    assert result.active_competitor_pursuit is False


def test_check_checks_competitors_in_order_and_stops_at_first_conflict():
    with patch.object(cc, "identify_competitors", return_value=["Rival A", "Rival B"]), \
         patch.object(cc.entity_resolver, "find_matching_accounts", side_effect=lambda name: [_account(1, name)]), \
         patch.object(cc.zoho_db, "find_deals_by_account_ids", return_value=[_deal(10, "Deal A", "Qualified Prospect")]) as mock_deals:
        result = cc.check("Test Co")

    assert result.competitor_name == "Rival A"
    assert mock_deals.call_count == 1  # never checked Rival B once Rival A confirmed a conflict

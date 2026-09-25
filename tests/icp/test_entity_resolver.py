# Copied from icp-bot/backend/tests/test_entity_resolver.py; only import paths are adapted.
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.icp import entity_resolver as er


def test_normalize_for_matching_strips_suffixes_and_noise_words():
    assert er.normalize_for_matching("Sobha Ltd.") == "sobha"
    assert er.normalize_for_matching("Kalyani Group") == "kalyani"
    assert er.normalize_for_matching("Sobha India Industries") == "sobha"


def test_is_candidate_catches_short_root_against_longer_name():
    assert er._is_candidate("sula", "sula wines") is True
    assert er._is_candidate("sula", "sula vineyards") is True


def test_is_candidate_catches_genuine_misspelling_via_fuzzy_ratio():
    assert er._is_candidate("sobha", "shobha") is True


def test_is_candidate_rejects_unrelated_names():
    assert er._is_candidate("sobha", "reliance industries") is False


def test_name_exact_threshold_separates_true_and_false_positive():
    # Regression test for the real mistake this module made before being
    # corrected: fuzzy name similarity alone cannot tell "Sobha"/"Shobha"
    # (same company, one inserted letter) apart from "Trent"/"Trident"
    # (different companies, also edit-distance ~2) — only email-domain
    # confirmation (or an exact/near-exact name) may promote a fuzzy
    # candidate to "confirmed". Assert the raw similarity scores sit on the
    # correct sides of NAME_EXACT_THRESHOLD.
    assert er.similarity("sobha", "shobha") < er.NAME_EXACT_THRESHOLD
    assert er.similarity("trent", "trident") < er.NAME_EXACT_THRESHOLD
    # Neither clears the "exact" bar on name alone — both need domain
    # confirmation, which is exactly the point: name similarity alone must
    # never be sufficient to merge two different-sounding accounts.


def test_domain_confirms_matches_query_root_against_domain_label():
    assert er._domain_confirms({"sobha.com"}, "sobha") is True
    assert er._domain_confirms({"tridentindia.com"}, "trent") is False
    assert er._domain_confirms({"gmail.com", "sulawines.com"}, "sula wines") is True


def test_is_confirmed_true_for_exact_name_even_without_domain():
    assert er._is_confirmed("sobha", "sobha", domains=set()) is True


def test_is_confirmed_false_for_fuzzy_name_without_domain():
    assert er._is_confirmed("trent", "trident", domains=set()) is False


def test_is_confirmed_true_for_fuzzy_name_with_confirming_domain():
    assert er._is_confirmed("sobha", "shobha", domains={"sobha.com"}) is True


def test_root_token_prefers_first_word_when_substantial():
    assert er._root_token("sula wines") == "sula"
    assert er._root_token("sula vineyards") == "sula"


def test_root_token_falls_back_to_longest_word_when_first_is_short():
    assert er._root_token("ab mauri india") == "mauri"


def test_root_token_confirms_true_for_shared_distinctive_root():
    assert er._root_token_confirms("sula wines", "sula vineyards") is True


def test_root_token_confirms_false_for_genuinely_different_words():
    # The module's own documented true-negative: "trent" and "trident" are
    # different words despite being fuzzy-close by edit distance.
    assert er._root_token_confirms("trent", "trident") is False
    assert er._root_token_confirms("trent", "tata trent") is False


def test_is_confirmed_true_via_root_token_without_domain_or_near_exact_name():
    """Live-confirmed real bug: "Sula Vineyards"'s only deal has zero
    logged reachout emails, so it can never self-confirm via domain, and
    its full-string similarity to "Sula Wines" (0.75) is well under the
    exact-name bar -- but "sula" is a real, identical distinctive root, so
    this should confirm without needing a domain."""
    assert er._is_confirmed("sula wines", "sula vineyards", domains=set()) is True


def _account(id_, name, modified_time=None):
    return {"id": id_, "account_name": name, "modified_time": modified_time}


def _deal(id_, name, account_id=None, stage="Qualified Prospect", modified_time=None):
    return {"id": id_, "deal_name": name, "account_id": account_id, "stage": stage, "modified_time": modified_time}


def test_resolve_pools_deals_only_from_confirmed_accounts_not_fuzzy_lookalikes():
    """The real regression case this module was rewritten for: querying
    "Trent" must not pool "Trident Limited"'s deals into the result."""
    accounts = [
        _account(1, "Trent Limited"),
        _account(2, "Trident Limited"),
        _account(3, "Tata Trent"),
    ]
    deals_by_account = {
        1: [_deal(101, "Trent Limited - Cost Optimization", account_id=1, stage="Client Won", modified_time=datetime(2026, 3, 1))],
        2: [_deal(201, "Trident Limited - Some Deal", account_id=2, modified_time=datetime(2026, 1, 1))],
        3: [],
    }
    domains_by_account = {1: set(), 2: {"tridentindia.com"}, 3: set()}

    with patch.object(er, "find_matching_accounts", return_value=accounts), \
         patch.object(er, "find_matching_deals_by_name", return_value=[]), \
         patch.object(er, "_account_domains", side_effect=lambda aid: domains_by_account[aid]), \
         patch("api.icp.entity_resolver.zoho_db.find_deals_by_account_ids",
               side_effect=lambda ids: [d for i in ids for d in deals_by_account.get(i, [])]):
        result = er.resolve("Trent")

    assert result.matched_account_ids == [1]
    assert result.matched_deal_ids == [101]
    assert result.contracting_entity_name == "Trent Limited"
    confirmed_flags = {d.account_id: d.confirmed for d in result.duplicate_accounts}
    assert confirmed_flags == {1: True, 2: False, 3: False}
    assert "not confirmed" in result.contracting_entity_note.lower() or "NOT confirmed" in result.contracting_entity_note


def test_resolve_pools_a_genuine_root_token_duplicate_with_no_logged_email():
    """Live regression case: "Sula Vineyards" has one Client-Lost deal with
    zero reachout_tracker rows, so it could never self-confirm via domain --
    it should now confirm via the shared "sula" root and its deal should be
    pooled into the result, unlike "Trent"/"Trident" above."""
    accounts = [
        _account(1, "Sula Wines"),
        _account(2, "Sula Vineyards"),
    ]
    deals_by_account = {
        1: [_deal(101, "Sula Wines - Growth & Penetration", account_id=1, stage="Client Lost", modified_time=datetime(2026, 3, 1))],
        2: [_deal(201, "Sula Vineyards - Project", account_id=2, stage="Client Lost", modified_time=datetime(2024, 6, 20))],
    }
    domains_by_account = {1: {"sulawines.com"}, 2: set()}

    with patch.object(er, "find_matching_accounts", return_value=accounts), \
         patch.object(er, "find_matching_deals_by_name", return_value=[]), \
         patch.object(er, "_account_domains", side_effect=lambda aid: domains_by_account[aid]), \
         patch("api.icp.entity_resolver.zoho_db.find_deals_by_account_ids",
               side_effect=lambda ids: [d for i in ids for d in deals_by_account.get(i, [])]):
        result = er.resolve("Sula Wines")

    assert result.matched_account_ids == [1, 2]
    assert sorted(result.matched_deal_ids) == [101, 201]
    confirmed_flags = {d.account_id: d.confirmed for d in result.duplicate_accounts}
    assert confirmed_flags == {1: True, 2: True}


def test_resolve_sets_contracting_entity_name_when_confirmed_only_via_deal_name_path():
    """An account confirmed ONLY via §0.1 step 3 (deal-name fuzzy match,
    independent of account) never appears in `account_candidates` (step 1's
    account-name search) -- previously this left `contracting_entity_name`
    None even though `entity_resolved` was True from `deals` being
    non-empty, silently skipping Gate 1's stop for an entity that genuinely
    WAS resolved, just not by account name."""
    # Deal name matches the query closely enough to confirm on name alone
    # (>= NAME_EXACT_THRESHOLD), so no domain corroboration is needed here --
    # isolates the bug under test (account_candidates never populated) from
    # the separate domain-confirmation path.
    deal = _deal(701, "Sobha Realty", account_id=42, modified_time=datetime(2026, 2, 1))
    account = _account(42, "Sobha Realty Private Limited")

    with patch.object(er, "find_matching_accounts", return_value=[]), \
         patch.object(er, "find_matching_deals_by_name", return_value=[deal]), \
         patch.object(er, "_deal_domains", return_value=set()), \
         patch("api.icp.entity_resolver.zoho_db.find_deals_by_account_ids", return_value=[deal]), \
         patch("api.icp.entity_resolver.zoho_db.get_account", return_value=account):
        result = er.resolve("Sobha Realty")

    assert result.entity_resolved is True
    assert result.contracting_entity_name == "Sobha Realty Private Limited"


def test_resolve_reports_duplicate_when_second_account_confirmed_only_via_deal_name_path():
    """Live-confirmed real gap (Gabriel Auto, 2026-09-02): a SECOND account
    confirmed only via §0.1 step 3 (deal-name path, independent of account
    name) never appears in `account_candidates` (step 1's account-name
    search) at all -- "Gabriel Auto"'s account-name search only surfaced
    "Gabriel India", but a deal named "Anand Group - Gabriel Auto - AI
    Automation" under a DIFFERENT account ("Anand Group") confirmed that
    second account via its deal name alone. `contracting_entity_name`
    already accounted for this kind of account (see the test above), but
    `duplicate_accounts` did not: it was built and length-gated using
    ONLY `account_candidates`, so a real "2 confirmed accounts" hygiene
    finding was silently dropped from `duplicate_accounts` (QC check #2's
    own job to catch) even though `matched_account_ids` correctly had both."""
    account_1 = _account(1, "Gabriel India")
    deal = _deal(701, "Anand Group - Gabriel Auto - AI Automation", account_id=2, modified_time=datetime(2026, 2, 1))
    account_2 = _account(2, "Anand Group")

    with patch.object(er, "find_matching_accounts", return_value=[account_1]), \
         patch.object(er, "find_matching_deals_by_name", return_value=[deal]), \
         patch.object(er, "_account_domains", return_value=set()), \
         patch.object(er, "_deal_domains", return_value={"gabriel.co.in"}), \
         patch("api.icp.entity_resolver.zoho_db.find_deals_by_account_ids", return_value=[deal]), \
         patch("api.icp.entity_resolver.zoho_db.get_account", return_value=account_2):
        result = er.resolve("Gabriel Auto")

    assert set(result.matched_account_ids) == {1, 2}
    confirmed_flags = {d.account_id: d.confirmed for d in result.duplicate_accounts}
    assert confirmed_flags == {1: True, 2: True}


def test_resolve_confirms_fuzzy_account_via_matching_email_domain():
    """The real "Sobha"/"Shobha Limited" case: a genuinely misspelled
    duplicate must still be confirmed and pooled, via domain evidence."""
    accounts = [
        _account(10, "Sobha Ltd."),
        _account(11, "Shobha Limited"),
    ]
    deals_by_account = {
        10: [],
        11: [_deal(1101, "Shobha Real Estate - Cost Visibility", account_id=11, modified_time=datetime(2026, 7, 25))],
    }
    domains_by_account = {10: set(), 11: {"sobha.com"}}

    with patch.object(er, "find_matching_accounts", return_value=accounts), \
         patch.object(er, "find_matching_deals_by_name", return_value=[]), \
         patch.object(er, "_account_domains", side_effect=lambda aid: domains_by_account[aid]), \
         patch("api.icp.entity_resolver.zoho_db.find_deals_by_account_ids",
               side_effect=lambda ids: [d for i in ids for d in deals_by_account.get(i, [])]):
        result = er.resolve("Sobha")

    assert set(result.matched_account_ids) == {10, 11}
    assert result.matched_deal_ids == [1101]
    assert all(d.confirmed for d in result.duplicate_accounts)


def test_resolve_reports_no_entity_found_when_nothing_matches():
    with patch.object(er, "find_matching_accounts", return_value=[]), \
         patch.object(er, "find_matching_deals_by_name", return_value=[]):
        result = er.resolve("Totally Unknown Company Xyz")

    assert result.entity_resolved is False
    assert result.matched_account_ids == []
    assert "no account or potential record" in result.contracting_entity_note.lower()


def test_most_recent_deal_picks_latest_modified_time():
    deals = [
        _deal(1, "A", modified_time=datetime(2024, 1, 1)),
        _deal(2, "B", modified_time=datetime(2026, 6, 1)),
        _deal(3, "C", modified_time=datetime(2025, 1, 1)),
    ]

    assert er.most_recent_deal(deals)["id"] == 2


def test_most_recent_deal_handles_none_modified_time():
    deals = [_deal(1, "A", modified_time=None), _deal(2, "B", modified_time=datetime(2026, 1, 1))]

    assert er.most_recent_deal(deals)["id"] == 2


def test_most_recent_deal_empty_list_returns_none():
    assert er.most_recent_deal([]) is None


def test_looks_like_group_or_holdco_flags_group_and_holding_suffixes():
    assert er._looks_like_group_or_holdco("Piramal Group") is True
    assert er._looks_like_group_or_holdco("XYZ Holdings Limited") is True
    assert er._looks_like_group_or_holdco("ABC Holding Pvt Ltd") is True
    assert er._looks_like_group_or_holdco("Acme Ventures") is True


def test_looks_like_group_or_holdco_does_not_flag_ordinary_entities():
    # Regression guard: ordinary, perfectly scoreable single legal entities
    # must not be flagged just because they contain an industrial/generic
    # word or a group-ish-sounding-but-not-actually-a-holdco word.
    assert er._looks_like_group_or_holdco("Sobha Limited") is False
    assert er._looks_like_group_or_holdco("Piramal Enterprises Limited") is False
    assert er._looks_like_group_or_holdco("Reliance Industries") is False
    assert er._looks_like_group_or_holdco("Sula Wines") is False


def test_resolve_fires_gate_1_when_queried_group_name_has_no_distinct_opco():
    """Scenario 6 (icp-skill.md): "user names a group, not an entity ->
    Gate 1. Never sum subsidiaries." When the only account found is itself
    titled as the group (no more specific operating subsidiary resolved),
    this must flip `entity_resolved` to False so orchestrator.py's Gate 1
    stop actually fires -- a soft caveat alone lets the run proceed and
    score the group as if it were a real contracting entity, exactly the
    failure mode Gate 1 exists to prevent."""
    accounts = [_account(1, "Piramal Group")]
    deals_by_account = {1: [_deal(501, "Piramal Group - Cost Study", account_id=1, modified_time=datetime(2026, 2, 1))]}

    with patch.object(er, "find_matching_accounts", return_value=accounts), \
         patch.object(er, "find_matching_deals_by_name", return_value=[]), \
         patch.object(er, "_account_domains", return_value=set()), \
         patch("api.icp.entity_resolver.zoho_db.find_deals_by_account_ids",
               side_effect=lambda ids: [d for i in ids for d in deals_by_account.get(i, [])]):
        result = er.resolve("Piramal Group")

    note_lower = result.contracting_entity_note.lower()
    assert "group/holding" in note_lower
    assert "never sum revenues" in note_lower
    assert result.entity_resolved is False


def test_resolve_does_not_fire_gate_1_when_a_distinct_opco_is_found_for_a_holdco_query():
    """Scenario 7: "holdco named, opco is the buyer -> score the opco, note
    the relationship." Here the queried name reads as a holdco, but the
    account actually resolved is a meaningfully more specific name (a real
    operating subsidiary) -- this must NOT fire Gate 1, just note the
    relationship, per scenario 7."""
    accounts = [_account(1, "Piramal Pharma Solutions Limited")]
    deals_by_account = {1: [_deal(502, "Piramal Pharma - Cost Study", account_id=1, modified_time=datetime(2026, 2, 1))]}

    # Domain confirmation (§0.1 step 4) is what promotes this fuzzy-similar
    # candidate to "confirmed" -- without it the account would never even
    # be scored, regardless of the group/holdco check under test here.
    with patch.object(er, "find_matching_accounts", return_value=accounts), \
         patch.object(er, "find_matching_deals_by_name", return_value=[]), \
         patch.object(er, "_account_domains", return_value={"piramalpharma.com"}), \
         patch("api.icp.entity_resolver.zoho_db.find_deals_by_account_ids",
               side_effect=lambda ids: [d for i in ids for d in deals_by_account.get(i, [])]):
        result = er.resolve("Piramal Group")

    note_lower = result.contracting_entity_note.lower()
    assert "group/holding" in note_lower
    assert "do not sum revenues" in note_lower
    assert result.entity_resolved is True
    assert result.contracting_entity_name == "Piramal Pharma Solutions Limited"


def test_resolve_does_not_flag_group_note_for_ordinary_single_entity():
    accounts = [_account(1, "Sobha Limited")]
    deals_by_account = {1: [_deal(601, "Sobha - Cost Study", account_id=1, modified_time=datetime(2026, 2, 1))]}

    with patch.object(er, "find_matching_accounts", return_value=accounts), \
         patch.object(er, "find_matching_deals_by_name", return_value=[]), \
         patch.object(er, "_account_domains", return_value=set()), \
         patch("api.icp.entity_resolver.zoho_db.find_deals_by_account_ids",
               side_effect=lambda ids: [d for i in ids for d in deals_by_account.get(i, [])]):
        result = er.resolve("Sobha Limited")

    assert "group/holding" not in result.contracting_entity_note.lower()
    assert result.entity_resolved is True


def test_has_distinct_sibling_accounts_true_for_different_sounding_confirmed_accounts():
    accounts = [_account(1, "Piramal Pharma"), _account(2, "Piramal Realty")]
    assert er._has_distinct_sibling_accounts(accounts) is True


def test_has_distinct_sibling_accounts_false_for_spelling_variants_only():
    accounts = [_account(1, "Sobha Ltd."), _account(2, "Shobha Limited")]
    assert er._has_distinct_sibling_accounts(accounts) is False


def test_has_distinct_sibling_accounts_false_for_single_account():
    assert er._has_distinct_sibling_accounts([_account(1, "Sobha Limited")]) is False

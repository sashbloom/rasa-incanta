"""Fixtures for the ICP bot's copied tests.

1. Tracing is forced off, as in the ICP bot's own conftest, so no test sends a real trace.
2. Every Zoho and Setu read returns nothing unless a test patches it. Some of the ICP bot's tests
   leave a lookup unpatched (e.g. `practus_history.classify()` -> `zoho_db.find_all_won_deals()`,
   `build_crm_structured()` -> `zoho_db.get_reachouts()`), which only passed there because the
   developers' own .env held real credentials, so those calls quietly queried the live database.
   Here they are hermetic; a test's own @patch still overrides these defaults.
"""
import pytest

_ZOHO_READS = {
    "find_accounts_by_name": [], "get_account": None, "list_all_accounts": [], "list_all_deal_names": [],
    "find_deals_by_account_ids": [], "get_deal": None, "get_deal_history": [], "get_reachouts": [],
    "find_won_deals_by_account_ids": [], "find_won_deals_by_industry": [], "find_all_won_deals": [],
    "find_lost_deals_by_account_ids": [], "find_user_by_name_fragment": [], "get_sales_orders_by_account": [],
    "get_team_structure_by_so": [],
}
_SETU_READS = {
    "fetch_ep_el_tl_roster": [], "fetch_skill_profiles": {}, "fetch_resume_text": {}, "fetch_named_clients": {},
    "fetch_partner_profiles": [], "fetch_case_studies": [],
}


@pytest.fixture(autouse=True)
def _disable_langfuse_tracing_by_default(monkeypatch):
    from api.icp import tracing

    monkeypatch.setattr(tracing, "TRACING_ENABLED", False)


@pytest.fixture(autouse=True)
def _no_live_database_reads(monkeypatch):
    from api.icp import setu_db, zoho_db

    for module, reads in ((zoho_db, _ZOHO_READS), (setu_db, _SETU_READS)):
        for name, empty in reads.items():
            monkeypatch.setattr(module, name, lambda *a, _empty=empty, **k: _empty.copy() if hasattr(_empty, "copy") else _empty)

"""Setu case studies as the capability signal (recorded rows, no live calls)."""
import json
from datetime import date
from pathlib import Path

from api.engine.capability import match_case_studies, query_text
from api.engine.context import build_card
from api.sources.setu import fetch_case_studies, row_to_case_study
from api.sources.zoho import row_to_deal
from tests.fakes import load, settings

TODAY = date(2026, 9, 24)
ROWS = json.loads((Path(__file__).parent / "fixtures/setu/case_studies.json").read_text())


class FakeSetu:
    def __init__(self, rows=ROWS, error=None):
        self.rows, self.error, self.executed, self.read_only = rows, error, [], True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return self

    def execute(self, sql, params=None):
        if self.error:
            raise self.error
        self.executed.append(sql)

    def fetchall(self):
        return list(self.rows)


def setu_settings():
    return settings(setu_pghost="setu.example", setu_pguser="reader", setu_pgpassword="x", setu_pgdatabase="wisible_data")


def corpus():
    return fetch_case_studies(setu_settings(), connect_fn=lambda s: FakeSetu()).case_studies


def northwind():
    return row_to_deal(load("zoho/deals.json")[0])


def blue_harbour():
    return row_to_deal(load("zoho/deals.json")[1])


# ---------------------------------------------------------------- the source

def test_reads_case_studies_read_only_and_cleans_them():
    conn = FakeSetu()
    result = fetch_case_studies(setu_settings(), connect_fn=lambda s: conn)
    assert result.ok and [cs.name for cs in result.case_studies] == [
        "Patisserie & Bakes", "Steel Major", "F-Mart Mobiles", "Northwind Foods Treasury"]  # untitled chunk dropped
    assert "source_type = 'case_study'" in conn.executed[0]
    treasury = result.case_studies[3]
    assert treasury.industry is None and treasury.service_line is None  # "nan" means missing


def test_not_configured_and_failures_are_reported_not_raised():
    assert "not configured" in fetch_case_studies(settings()).error
    failed = fetch_case_studies(setu_settings(), connect_fn=lambda s: FakeSetu(error=OSError("host setu.example")))
    assert not failed.ok and "Setu read failed (OSError)" in failed.error and "setu.example" not in failed.error


def test_untitled_rows_are_skipped():
    assert row_to_case_study({"entity_name": None, "content": "x"}) is None


# ---------------------------------------------------------------- matching (the ICP bot's P2 scoring)

def test_same_industry_ranks_first_and_company_name_counts_as_a_keyword():
    matches = match_case_studies(northwind(), corpus())
    assert matches[0].case.name == "Patisserie & Bakes"  # Food Processing = the deal's "Food processing"
    assert matches[0].industry_match and matches[0].score >= 2
    treasury = next(m for m in matches if m.case.name == "Northwind Foods Treasury")
    assert not treasury.industry_match and "northwind" in treasury.keyword_hits


def test_geography_alone_never_counts():
    # "Steel Major" mentions India and Northwind is India SBU, but shares no industry or keyword.
    assert "Steel Major" not in [m.case.name for m in match_case_studies(northwind(), corpus())]


def test_a_missing_industry_never_matches_a_missing_industry():
    deal = row_to_deal(dict(load("zoho/deals.json")[0], industry_type="Others", deal_name="Acme - Review",
                            _rel_account_name="Acme"))
    assert match_case_studies(deal, corpus()) == []  # "Others" vs "nan" is not a match


def test_query_text_leads_with_name_and_company():
    assert query_text(northwind()).startswith("Northwind Foods - Working capital. Northwind Foods Pvt Ltd. ")


def test_at_most_three_matches():
    assert len(match_case_studies(northwind(), corpus() * 3)) == 3


# ---------------------------------------------------------------- the card

def test_matches_become_setu_facts_and_clear_the_gap():
    card = build_card(northwind(), TODAY, case_studies=corpus())
    facts = card.capability["facts"]
    assert facts[0]["id"] == "setu.case_1" and facts[0]["source"] == "setu" and facts[0]["label"] == "Patisserie & Bakes"  # the chip names the case
    assert facts[0]["value"].startswith("Patisserie & Bakes (Food Processing, Strategic Finance Transformation). "
                                        "Matched on same industry")
    assert "Problem: rapid multi-city expansion" in facts[0]["value"]
    assert "no_setu_match" not in card.gaps
    assert card.capability["case_studies"][0]["industry_match"] is True


def test_no_match_or_no_setu_keeps_the_gap():
    assert "no_setu_match" in build_card(blue_harbour(), TODAY, case_studies=[]).gaps
    assert "no_setu_match" in build_card(northwind(), TODAY, case_studies=None).gaps  # Setu unavailable


def test_retail_deal_finds_the_retail_case():
    card = build_card(blue_harbour(), TODAY, case_studies=corpus())
    assert [c["name"] for c in card.capability["case_studies"]] == ["F-Mart Mobiles"]


def test_a_run_uses_setu_once_and_a_setu_outage_does_not_fail_it(migrated):
    from api.db import get_sessionmaker
    from api.engine.run import run_week
    from api.models import ContextCard
    from tests.fakes import NOW, FakeClaude, claude_response
    from tests.test_run import zoho_fetch

    calls = []

    def fetch_setu(s):
        calls.append(1)
        return fetch_case_studies(setu_settings(), connect_fn=lambda _: FakeSetu())

    def setu_down(s):
        return fetch_case_studies(setu_settings(), connect_fn=lambda _: FakeSetu(error=OSError()))

    reply = load("claude/nba_reply.json")
    with get_sessionmaker()() as s:
        run = run_week(s, settings(), now=NOW, fetch=zoho_fetch(), fetch_setu=fetch_setu,
                       llm_client=FakeClaude(claude_response(reply)))
        assert run.stats["sources"]["setu"] == "ok" and calls == [1]
        cards = s.query(ContextCard).filter(ContextCard.run_id == run.id).all()
        assert all(c.capability.get("facts") for c in cards)  # both deals found proof

        down = run_week(s, settings(), now=NOW, fetch=zoho_fetch(), fetch_setu=setu_down,
                        llm_client=FakeClaude(claude_response(reply)))
        assert down.status == "succeeded" and "Setu read failed" in down.stats["sources"]["setu"]


def test_a_common_term_alone_is_not_proof():
    from api.sources.setu import CaseStudy

    common = [CaseStudy(name=f"Case {i}", content="A finance transformation programme.") for i in range(3)]
    rare = CaseStudy(name="Treasury case", content="Treasury automation for a refinery.")
    deal = row_to_deal(dict(load("zoho/deals.json")[0], industry_type="Others",
                            deal_name="Acme - Finance Transformation", services="Treasury Automation"))
    names = [m.case.name for m in match_case_studies(deal, common + [rare])]
    assert names == ["Treasury case"]  # "transformation" is in 3 of 4 cases; "treasury" is in 1

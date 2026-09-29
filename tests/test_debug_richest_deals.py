"""GET /api/debug/richest-deals: the best-documented deals, spread across all three boards,
gated behind X-Debug-Key like every other debug route."""
from unittest.mock import patch

from api.sources.setu import CaseStudy, SetuResult
from api.sources.zoho import Reachout, ZohoDeal, ZohoResult


def deal(zoho_id, stage, *, account=None, **overrides):
    fields = {"zoho_id": zoho_id, "name": f"Deal {zoho_id}", "stage": stage,
             "account_name": account or f"Company {zoho_id}"}
    fields.update(overrides)
    return ZohoDeal(**fields)


def reachouts_for(zoho_id, count):
    return [Reachout(deal_zoho_id=zoho_id) for _ in range(count)]


def zoho_scenario():
    """6 Prospect deals, 1 Pre-Pipeline deal, 10 Pipeline deals -- 17 total, none with a matching
    industry or a filled field, so reachout count alone drives the score."""
    prospect_counts = [16, 15, 14, 13, 12, 1]  # P6 is the weak one
    pipeline_counts = [100, 90, 80, 70, 60, 9, 8, 7, 6, 2]  # R6-R9 beat P6; R10 does not

    deals, reachouts = [], {}
    for i, n in enumerate(prospect_counts, 1):
        zid = f"p{i}"
        deals.append(deal(zid, "Prospect"))
        reachouts[zid] = reachouts_for(zid, n)
    deals.append(deal("q1", "Need Identification"))
    reachouts["q1"] = reachouts_for("q1", 5)
    for i, n in enumerate(pipeline_counts, 1):
        zid = f"r{i}"
        deals.append(deal(zid, "Qualified Prospect"))
        reachouts[zid] = reachouts_for(zid, n)
    return ZohoResult(deals=deals, reachouts=reachouts)


def test_no_key_is_a_404(client):
    assert client.get("/api/debug/richest-deals").status_code == 404


def test_returns_503_when_zoho_is_unreachable(client, debug_key):
    with patch("api.main.fetch_deals", return_value=ZohoResult(error="Zoho Postgres is not configured")):
        response = client.get("/api/debug/richest-deals", headers={"X-Debug-Key": debug_key})
    assert response.status_code == 503 and "Zoho" in response.json()["detail"]


def test_top_15_are_spread_across_all_three_boards_with_a_5_per_board_cap(client, debug_key):
    with patch("api.main.fetch_deals", return_value=zoho_scenario()), \
         patch("api.main.fetch_case_studies", return_value=SetuResult(case_studies=[])):
        response = client.get("/api/debug/richest-deals", headers={"X-Debug-Key": debug_key})

    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 15

    boards = {r["board"] for r in rows}
    assert boards == {"prospect", "pre_pipeline", "pipeline"}  # all three represented

    by_board: dict[str, list[dict]] = {}
    for r in rows:
        by_board.setdefault(r["board"], []).append(r)
    assert [r["deal_name"] for r in by_board["prospect"]] == [f"Deal p{i}" for i in range(1, 6)]  # top 5, not p6
    assert [r["deal_name"] for r in by_board["pre_pipeline"]] == ["Deal q1"]  # the only one there is
    # pipeline's own top 5, plus r6-r9 backfilled ahead of p6 (score 1) and r10 (score 2)
    assert {r["deal_name"] for r in by_board["pipeline"]} == {f"Deal r{i}" for i in list(range(1, 6)) + [6, 7, 8, 9]}
    assert "Deal p6" not in {r["deal_name"] for r in rows}
    assert "Deal r10" not in {r["deal_name"] for r in rows}

    # richest first within a board
    assert [r["outreach_count"] for r in by_board["prospect"]] == [16, 15, 14, 13, 12]


def test_richness_fields_reflect_outreach_industry_match_and_filled_fields(client, debug_key):
    rich = deal("d1", "Prospect", account="Sula Wines", industry="Wine", business_area="Growth",
               problem_statements=["Margin pressure."], raw={"contact_id": "c-1"})
    thin = deal("d2", "Prospect", account="Blue Harbour", industry=None)
    d1_reachouts = [Reachout(deal_zoho_id="d1", person="Priya Shah"), Reachout(deal_zoho_id="d1", person="Priya Shah"),
                    Reachout(deal_zoho_id="d1", person="Arjun Menon")]
    zoho = ZohoResult(deals=[rich, thin], reachouts={"d1": d1_reachouts, "d2": []})
    setu = SetuResult(case_studies=[CaseStudy(name="Wine Case", content="x", industry="Wine")])

    with patch("api.main.fetch_deals", return_value=zoho), patch("api.main.fetch_case_studies", return_value=setu):
        response = client.get("/api/debug/richest-deals", headers={"X-Debug-Key": debug_key})

    by_name = {r["company"]: r for r in response.json()}
    assert by_name["Sula Wines"] == {"company": "Sula Wines", "deal_name": "Deal d1", "stage": "Prospect",
                                     "board": "prospect", "outreach_count": 3, "contact_name": "Priya Shah",
                                     "outreach_has_contact": True, "setu_industry_match": True,
                                     "filled_field_count": 4}
    assert by_name["Blue Harbour"]["setu_industry_match"] is False
    assert by_name["Blue Harbour"]["filled_field_count"] == 0
    assert by_name["Blue Harbour"]["contact_name"] is None and by_name["Blue Harbour"]["outreach_has_contact"] is False


def test_contact_name_is_the_most_frequent_usable_name_junk_excluded(client, debug_key):
    reachouts = [
        Reachout(deal_zoho_id="d1", person="priya.shah@northwindfoods.example"),  # email in the name field: excluded
        Reachout(deal_zoho_id="d1", person="NA"),  # placeholder: excluded
        Reachout(deal_zoho_id="d1", person="Arjun Menon"),
        Reachout(deal_zoho_id="d1", person="Priya Shah"),
        Reachout(deal_zoho_id="d1", person="Priya Shah"),
    ]
    zoho = ZohoResult(deals=[deal("d1", "Prospect")], reachouts={"d1": reachouts})

    with patch("api.main.fetch_deals", return_value=zoho), \
         patch("api.main.fetch_case_studies", return_value=SetuResult(case_studies=[])):
        response = client.get("/api/debug/richest-deals", headers={"X-Debug-Key": debug_key})

    row = response.json()[0]
    assert row["contact_name"] == "Priya Shah" and row["outreach_has_contact"] is True
    assert row["outreach_count"] == 5  # the count is raw entries; junk filtering only affects the name pick


def test_a_deal_with_only_junk_names_has_no_contact(client, debug_key):
    reachouts = [Reachout(deal_zoho_id="d1", person="NA"), Reachout(deal_zoho_id="d1", person="  ")]
    zoho = ZohoResult(deals=[deal("d1", "Prospect")], reachouts={"d1": reachouts})

    with patch("api.main.fetch_deals", return_value=zoho), \
         patch("api.main.fetch_case_studies", return_value=SetuResult(case_studies=[])):
        response = client.get("/api/debug/richest-deals", headers={"X-Debug-Key": debug_key})

    row = response.json()[0]
    assert row["contact_name"] is None and row["outreach_has_contact"] is False and row["outreach_count"] == 2


def test_a_setu_outage_does_not_fail_the_endpoint_it_just_means_no_industry_matches(client, debug_key):
    zoho = ZohoResult(deals=[deal("d1", "Prospect", industry="Wine")], reachouts={})
    with patch("api.main.fetch_deals", return_value=zoho), \
         patch("api.main.fetch_case_studies", return_value=SetuResult(error="Setu is down")):
        response = client.get("/api/debug/richest-deals", headers={"X-Debug-Key": debug_key})

    assert response.status_code == 200
    assert response.json()[0]["setu_industry_match"] is False

"""POST /api/icp/import: load ICP scores made elsewhere, skipping companies scored in the last 28 days."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from api.db import get_sessionmaker
from api.engine import icp_signal
from api.models import CompanyIcp

NOW = datetime.now(timezone.utc)


def entry(company, days_ago=2, verdict="Pursue", right_to_win="Strong operations fit"):
    return {"company": company, "verdict": verdict, "right_to_win": right_to_win,
            "scored_at": (NOW - timedelta(days=days_ago)).isoformat()}


def prathams(company_name, score_date="2026-09-20", **extra):
    return {"company_name": company_name, "verdict": "Pursue", "right_to_win": "Strong fit",
            "score_date": score_date, **extra}


def test_pratham_format_is_mapped_and_extra_fields_are_ignored(client, debug_key):
    body = [prathams("Acme Foods", gate_flag="G7", anything_else={"x": 1}), prathams("Northwind", "2026-09-22T10:00:00Z")]
    assert post(client, body, debug_key).json() == {"imported": 2, "skipped": 0}
    acme = next(r for r in rows() if r.company_name == "Acme Foods")
    assert acme.computed_at.date().isoformat() == "2026-09-20" and "gate_flag" not in str(acme.result)


def post(client, body, key):
    return client.post("/api/icp/import", json=body, headers={"X-Debug-Key": key})


def rows():
    with get_sessionmaker()() as s:
        return list(s.scalars(select(CompanyIcp)))


def test_no_key_or_a_wrong_key_is_a_404_and_imports_nothing(client, debug_key):
    assert client.post("/api/icp/import", json=[entry("Acme")]).status_code == 404
    assert post(client, [entry("Acme")], "wrong").status_code == 404
    assert rows() == []


def test_scores_are_imported_and_counted(client, debug_key):
    response = post(client, [entry("Acme Foods Pvt Ltd"), entry("Northwind", verdict="Pass")], debug_key)
    assert response.status_code == 200 and response.json() == {"imported": 2, "skipped": 0}
    acme = next(r for r in rows() if r.company_name == "Acme Foods Pvt Ltd")
    assert acme.status == "scored" and acme.company_key == icp_signal.company_key("Acme Foods Limited")
    assert acme.account_fit["recommendation"] == "Pursue"
    assert "Strong operations fit" in acme.account_fit["facts"][0]["value"]


def test_a_company_scored_in_the_last_28_days_is_skipped(client, debug_key):
    post(client, [entry("Acme Foods")], debug_key)
    again = post(client, [entry("ACME FOODS Pvt Ltd", verdict="Pass"), entry("Northwind")], debug_key)
    assert again.json() == {"imported": 1, "skipped": 1}
    assert len([r for r in rows() if r.company_key == icp_signal.company_key("Acme Foods")]) == 1  # not overwritten


def test_a_repeat_inside_one_request_is_skipped(client, debug_key):
    assert post(client, [entry("Acme"), entry("Acme Ltd")], debug_key).json() == {"imported": 1, "skipped": 1}


def test_a_score_older_than_28_days_does_not_block_a_new_one(migrated):
    with get_sessionmaker()() as s:
        icp_signal.import_scores(s, [{**entry("Acme", days_ago=40), "scored_at": NOW - timedelta(days=40)}], NOW, 28)
        imported, skipped = icp_signal.import_scores(s, [{**entry("Acme"), "scored_at": NOW}], NOW, 28)
    assert (imported, skipped) == (1, 0)


def test_an_imported_score_is_reused_by_a_run_even_with_exa_on(migrated):
    with get_sessionmaker()() as s:
        icp_signal.import_scores(s, [{**entry("Acme"), "scored_at": NOW}], NOW, 28)
        assert icp_signal.fresh_icp(s, icp_signal.company_key("Acme"), NOW, 28, require_exa=True) is not None


def test_a_malformed_body_is_rejected_whole(client, debug_key):
    bad = [entry("Acme"), {"company": "", "verdict": "Pursue", "scored_at": "not a date"}]
    assert post(client, bad, debug_key).status_code == 422
    assert rows() == []

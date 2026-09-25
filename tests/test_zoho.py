import pytest

from datetime import datetime, timezone

from api.sources.zoho import (
    build_deals_query,
    clean_list,
    clean_text,
    fetch_deals,
    in_scope_stage_keys,
    row_to_deal,
)
from tests.fakes import FakeConnection, load, settings, zoho_connect


def test_placeholders_become_missing():
    assert clean_text("  Others ") is None
    assert clean_text("Unidentified EL") is None
    assert clean_text("  Pune,   Maharashtra ") == "Pune, Maharashtra"


def test_multi_select_values_in_every_shape():
    assert clean_list(["A. Mehta", "Others"]) == ["A. Mehta"]
    assert clean_list('["A. Mehta", "R. Iyer"]') == ["A. Mehta", "R. Iyer"]
    assert clean_list("R. Iyer; Unidentified EL") == ["R. Iyer"]
    assert clean_list([{"name": "A. Mehta", "id": "1"}]) == ["A. Mehta"]
    assert clean_list(None) == []


def test_recorded_row_maps_to_a_typed_deal():
    deal = row_to_deal(load("zoho/deals.json")[0])
    assert deal.zoho_id == "598723000011234001"
    assert deal.name == "Northwind Foods - Working capital"
    assert deal.account_name == "Northwind Foods Pvt Ltd"
    assert deal.contact_name == "Priya Shah"
    assert deal.owner_name == "Rao"
    assert deal.ep_involved == ["A. Mehta"]
    assert deal.el_involved == ["R. Iyer"]
    assert deal.amount == 1850000.0
    # Proposal Sent is entered on date_proposal_sent
    assert deal.stage_entered_at == datetime(2026, 8, 21, tzinfo=timezone.utc)
    assert deal.modified_at.tzinfo is not None
    assert deal.problem_statements == ["Receivable days rose from 62 to 81 in two quarters"]
    assert deal.raw["amount"] == "1850000.00"


def test_blank_contact_and_placeholders_on_second_row():
    deal = row_to_deal(load("zoho/deals.json")[1])
    assert deal.contact_name is None
    assert deal.city_state is None
    assert deal.ep_involved == []
    assert deal.problem_statements == []
    assert deal.stage_entered_at == datetime(2026, 7, 1, tzinfo=timezone.utc)


def test_query_joins_only_what_the_copy_has():
    columns = {}
    for row in load("zoho/columns.json"):
        columns.setdefault(row["table_name"], set()).add(row["column_name"])
    sql = build_deals_query(columns)
    assert "LEFT JOIN public.users u ON u.id = d.owner_id" in sql
    assert "LEFT JOIN public.accounts a ON a.id = d.account_id" in sql
    assert "concat_ws(' ', c.first_name, c.last_name) AS _rel_contact_name" in sql
    assert "is_deleted" not in sql

    bare = build_deals_query({"deals": {"id", "stage", "deal_name", "contact_name", "is_deleted"}})
    assert "JOIN" not in bare
    assert "NOT coalesce(d.is_deleted, false)" in bare


def test_query_is_a_single_select():
    sql = build_deals_query({"deals": {"id", "stage"}})
    assert sql.lstrip().upper().startswith("SELECT")
    for word in ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER"):
        assert word not in sql.upper()


def test_stage_filter_covers_all_six_stages_and_the_typo():
    keys = in_scope_stage_keys()
    assert "proposal sent" in keys and "negetiated proposal sent" in keys
    assert "client won" not in keys and len(keys) == 7


def test_fetch_reads_recorded_rows():
    conn = FakeConnection()
    result = fetch_deals(settings(), connect_fn=zoho_connect(conn))
    assert result.ok
    assert [d.name for d in result.deals] == ["Northwind Foods - Working capital", "Blue Harbour Retail - Cost audit"]
    sql, params = conn.executed[1]
    assert "FROM public.deals d" in sql
    assert params == {"stages": in_scope_stage_keys()}


def test_fetch_without_credentials_reports_instead_of_raising():
    result = fetch_deals(settings(zoho_pg_host=""), connect_fn=zoho_connect(FakeConnection()))
    assert not result.ok and "not configured" in result.error


def test_fetch_failure_reports_instead_of_raising():
    def boom(_settings):
        raise OSError('connection to "zoho.example" as user "ri_reader" refused')

    result = fetch_deals(settings(), connect_fn=boom)
    assert not result.ok and "OSError" in result.error
    assert "zoho.example" not in result.error and "ri_reader" not in result.error  # no host or login leaks


def test_fetch_without_a_deals_table():
    result = fetch_deals(settings(), connect_fn=zoho_connect(FakeConnection(columns=[])))
    assert not result.ok and "public.deals" in result.error


@pytest.mark.parametrize("overrides, expected", [
    ({"zoho_db_url": "postgresql://r:x@db.example/zoho_data?sslmode=require"},
     {"conninfo": "postgresql://r:x@db.example/zoho_data?sslmode=require"}),
    ({}, {"host": "zoho.example", "user": "ri_reader", "password": "x", "port": 5432, "sslmode": "require",
          "dbname": "zoho_data"}),
])
def test_connect_hands_psycopg_the_url_or_the_parts(monkeypatch, overrides, expected):
    import api.sources.zoho as zoho

    seen = {}

    class Conn:
        read_only = False

    def fake_connect(**kwargs):
        seen.update(kwargs)
        return Conn()

    monkeypatch.setattr(zoho.psycopg, "connect", fake_connect)
    conn = zoho.connect(settings(**overrides))
    assert {k: seen[k] for k in expected} == expected
    assert conn.read_only is True  # read-only whichever way we connect

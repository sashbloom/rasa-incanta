"""The deal's outreach log (Zoho reachout_tracker) as the conversation signal."""
from datetime import date

from api.engine.context import build_card, conversation_signal
from api.sources.zoho import Reachout, build_reachout_query, fetch_deals, row_to_deal, row_to_reachout
from tests.fakes import FakeConnection, load, settings, zoho_connect

TODAY = date(2026, 9, 24)


def fetch(**conn_args):
    return fetch_deals(settings(), connect_fn=zoho_connect(FakeConnection(**conn_args)))


def test_outreach_rows_come_back_by_deal_newest_first():
    result = fetch()
    assert result.ok and result.reachout_error is None
    northwind = result.reachouts["598723000011234001"]
    assert [r.on for r in northwind] == [date(2026, 9, 9), date(2026, 7, 14)]
    first = northwind[0]
    assert (first.medium, first.person, first.designation, first.role, first.spoc) ==         ("Teams", "Priya Shah", "Chief Financial Officer", "Customer", "Rao")
    assert first.email == "priya.shah@northwindfoods.example"  # kept for matching mail later
    assert result.reachouts["598723000011234002"][0].remarks is None  # blank remarks become None


def test_outreach_query_is_only_for_the_pulled_deals():
    conn = FakeConnection()
    fetch_deals(settings(), connect_fn=zoho_connect(conn))
    sql, params = conn.executed[2]
    assert "FROM public.reachout_tracker r" in sql and "LEFT JOIN public.users u ON u.id = r.practus_spoc_id" in sql
    assert sorted(params["ids"]) == [598723000011234001, 598723000011234002]


def test_query_uses_only_existing_columns():
    assert build_reachout_query({"deals": {"id"}}) is None  # no outreach table at all
    sql = build_reachout_query({"reachout_tracker": {"deal_id", "client_contact_name"}})
    assert "r.client_contact_name AS person" in sql and "reachout_date" not in sql and "JOIN" not in sql


def test_a_broken_outreach_log_never_fails_the_deals():
    result = fetch(reachout_error=RuntimeError('column "remarks" at host zoho.example'))
    assert result.ok and len(result.deals) == 2 and result.reachouts == {}
    assert "outreach log read failed (RuntimeError)" in result.reachout_error
    assert "zoho.example" not in result.reachout_error


def test_missing_outreach_table_is_reported_not_raised():
    cols = [c for c in load("zoho/columns.json") if c["table_name"] != "reachout_tracker"]
    result = fetch(columns=cols)
    assert result.ok and result.reachouts == {} and "no reachout_tracker table" in result.reachout_error


def test_an_email_without_an_at_sign_is_dropped():
    assert row_to_reachout({"deal_id": 1, "email": "not-an-email"}).email is None


# ---------------------------------------------------------------- the conversation signal

def northwind_card():
    reachouts = fetch().reachouts["598723000011234001"]
    return build_card(row_to_deal(load("zoho/deals.json")[0]), TODAY, reachouts=reachouts)


def test_a_teams_meeting_becomes_a_sourced_fact_and_clears_no_call_logged():
    card = northwind_card()
    facts = card.conversation["facts"]
    assert facts[0] == {
        "id": "zoho.reachout_1", "source": "zoho", "label": "Outreach log", "date": "2026-09-09",
        "value": "Teams meeting with Priya Shah (Chief Financial Officer, customer), led by Rao. "
                 "Notes: Asked for a phased approach; board review of working capital in October.",
    }
    assert facts[1]["value"].startswith("Contact logged with Priya Shah")  # medium "Others"
    assert card.conversation["last_touch"] == "2026-09-09" and card.conversation["touches"] == 2
    assert "no_call_logged" not in card.gaps and "no_mail" in card.gaps  # no email-channel entry


def test_the_contact_field_stays_empty_as_agreed():
    card = northwind_card()
    assert "no_contact" in card.gaps and card.stakeholder == {}  # the mirror has no Contacts module


def test_email_addresses_never_reach_the_card():
    text = str(northwind_card().facts())
    assert "@" not in text and "northwindfoods.example" not in text


def test_notes_are_trimmed_to_an_excerpt():
    long = Reachout(deal_zoho_id="1", on=date(2026, 9, 1), medium="Phone", person="A", remarks="word " * 200)
    value = conversation_signal([long])[0]["facts"][0]["value"]
    assert value.endswith("...") and len(value) < 400


def test_only_calls_and_meetings_clear_no_call_logged():
    others = Reachout(deal_zoho_id="1", on=date(2026, 9, 1), medium="Others", person="A")
    email = Reachout(deal_zoho_id="1", on=date(2026, 9, 2), medium="Email", person="A")
    signal, gaps = conversation_signal([others, email])
    assert [g.value for g in gaps] == ["no_call_logged"]  # the email clears no_mail, not the call gap
    assert signal["facts"][0]["value"].startswith("Email with A")


def test_no_outreach_means_no_conversation_signal():
    signal, gaps = conversation_signal([])
    assert signal == {} and [g.value for g in gaps] == ["no_call_logged", "no_mail"]


def test_at_most_five_touches_become_facts():
    many = [Reachout(deal_zoho_id="1", on=date(2026, 9, d), medium="Teams", person="A") for d in range(1, 9)]
    signal, _ = conversation_signal(many)
    assert len(signal["facts"]) == 5 and signal["facts"][0]["date"] == "2026-09-08" and signal["touches"] == 8


def test_contact_details_typed_into_any_field_are_scrubbed():
    from api.engine.context import scrub

    odd = Reachout(deal_zoho_id="1", on=date(2026, 9, 1), medium="Teams", person="priya.shah@northwind.example",
                   designation="CFO", remarks="Call her on +91 98200 12345 or 022-2345-6789 before Friday")
    value = conversation_signal([odd])[0]["facts"][0]["value"]
    assert "@" not in value and "98200" not in value and "2345-6789" not in value
    assert "[email removed]" in value and value.count("[phone removed]") == 2
    # Money and dates are not phone numbers.
    assert scrub("INR 1,850,000 due 2026-09-21, 34 days") == "INR 1,850,000 due 2026-09-21, 34 days"

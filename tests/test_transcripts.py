"""POST /api/transcripts/upload and GET /api/transcripts: past meetings fed in by hand, stored as webhook
meetings are (source "upload"), matched to deals the same way, and gated by X-Debug-Key."""
import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import undefer

from api import transcripts
from api.db import get_sessionmaker
from api.engine.run import run_week
from api.models import Meeting
from api.sources.readai import recent_meetings
from api.sources.zoho import fetch_deals
from tests.fakes import NOW, FakeClaude, FakeConnection, claude_response, load, settings, zoho_connect

TRANSCRIPT = "Priya: We need the plan phased by quarter. Rao: Understood, we will send a one-pager. " * 40


def entry(title="Northwind Foods: working capital review", date="2026-08-12", **kw):
    return {"title": title, "date": date, "transcript": TRANSCRIPT,
            "participants": ["Priya Shah <priya.shah@northwindfoods.example>", "Rao <rao@roibypractus.com>"],
            "action_items": ["Send phased plan", "Book CFO call"], **kw}


@pytest.fixture(autouse=True)
def zoho_for_matching(monkeypatch):
    """Matching reads Zoho (company names, plus the email domains on each outreach log): the recorded rows."""
    monkeypatch.setattr(transcripts, "fetch_deals", lambda _app: fetch_deals(settings(), connect_fn=zoho_connect(FakeConnection())))


def upload(client, body, key, content_type="application/json"):
    data = json.dumps(body) if content_type == "application/json" else body
    return client.post("/api/transcripts/upload", content=data, headers={"X-Debug-Key": key, "Content-Type": content_type})


def stored():
    with get_sessionmaker()() as s:
        return list(s.scalars(select(Meeting).options(undefer(Meeting.transcript)).order_by(Meeting.start_time)))


# ---------------------------------------------------------------- the gate

def test_no_key_or_a_wrong_key_is_a_404_and_stores_nothing(client, debug_key):
    assert client.post("/api/transcripts/upload", json=[entry()]).status_code == 404
    assert upload(client, [entry()], "wrong").status_code == 404
    assert client.get("/api/transcripts").status_code == 404
    assert client.get("/api/transcripts", headers={"X-Debug-Key": "wrong"}).status_code == 404
    assert stored() == []


# ---------------------------------------------------------------- JSON

def test_a_json_array_is_stored_like_a_webhook_meeting_flagged_upload(client, debug_key):
    response = upload(client, [entry()], debug_key)
    assert response.status_code == 200
    body = response.json()
    assert (body["stored"], body["duplicates"], body["matched"], body["unmatched"]) == (1, 0, 1, 0)
    [meeting] = stored()
    assert meeting.source == "upload" and meeting.title == "Northwind Foods: working capital review"
    assert meeting.start_time.date().isoformat() == "2026-08-12"
    assert meeting.participants == [{"name": "Priya Shah", "email": "priya.shah@northwindfoods.example"},
                                    {"name": "Rao", "email": "rao@roibypractus.com"}]
    assert meeting.participant_domains == ["northwindfoods.example", "roibypractus.com"]  # as the webhook computes them
    assert meeting.action_items == ["Send phased plan", "Book CFO call"] and meeting.transcript.startswith("Priya:")
    assert meeting.owner == {} and meeting.key_questions == [] and meeting.topics == []


def test_a_meeting_matches_by_the_company_in_its_title(client, debug_key):
    row = upload(client, [entry(participants=["someone@gmail.com"])], debug_key).json()["meetings"][0]
    assert row["matched_deals"] == ["Northwind Foods - Working capital"]


def test_a_meeting_matches_by_participant_email_domain_when_the_title_names_no_company(client, debug_key):
    row = upload(client, [entry(title="Quarterly catch-up")], debug_key).json()["meetings"][0]
    assert row["matched_deals"] == ["Northwind Foods - Working capital"]  # priya.shah@northwindfoods.example is on the outreach log


def test_a_meeting_about_nobody_we_know_matches_nothing(client, debug_key):
    body = upload(client, [entry(title="Internal planning", participants=["a@roibypractus.com", "b@gmail.com"])], debug_key).json()
    assert (body["stored"], body["matched"], body["unmatched"]) == (1, 0, 1)
    assert body["meetings"][0]["matched_deals"] == []


def test_counts_cover_a_mixed_upload(client, debug_key):
    body = upload(client, [entry(), entry(title="Blue Harbour Retail kickoff", date="2026-08-20", participants=[]),
                           entry(title="Internal planning", date="2026-08-25", participants=[])], debug_key).json()
    assert (body["stored"], body["matched"], body["unmatched"]) == (3, 2, 1)


def test_uploading_the_same_meeting_twice_stores_it_once(client, debug_key):
    upload(client, [entry()], debug_key)
    again = upload(client, [entry(), entry(title="Blue Harbour Retail kickoff")], debug_key).json()
    assert (again["stored"], again["duplicates"]) == (1, 1)
    assert [m["status"] for m in again["meetings"]] == ["already stored", "stored"]
    assert len(stored()) == 2


def test_a_single_object_or_a_meetings_wrapper_is_accepted(client, debug_key):
    assert upload(client, entry(), debug_key).json()["stored"] == 1
    assert upload(client, {"meetings": [entry(title="Blue Harbour Retail kickoff")]}, debug_key).json()["stored"] == 1


def test_participants_can_be_dicts_or_plain_strings(client, debug_key):
    upload(client, [entry(participants=[{"name": "Priya", "email": "Priya@NorthwindFoods.example"}, "arjun@blueharbour.example", "Just A Name"])], debug_key)
    [meeting] = stored()
    assert meeting.participant_domains == ["blueharbour.example", "northwindfoods.example"]
    assert {"name": "Just A Name", "email": None} in meeting.participants


@pytest.mark.parametrize("bad", [
    [{"title": "No transcript", "date": "2026-08-12"}],
    [{"title": "", "date": "2026-08-12", "transcript": "x"}],
    [{"title": "Bad date", "date": "last Tuesday", "transcript": "x"}],
    [],
    "not json at all",
])
def test_a_malformed_upload_is_rejected_whole(client, debug_key, bad):
    response = client.post("/api/transcripts/upload", content=bad if isinstance(bad, str) else json.dumps(bad),
                           headers={"X-Debug-Key": debug_key, "Content-Type": "application/json"})
    assert response.status_code == 422 and response.json()["detail"]
    assert stored() == []


def test_one_bad_meeting_stores_none_of_the_batch(client, debug_key):
    response = upload(client, [entry(), {"title": "Broken", "date": "2026-08-12"}], debug_key)
    assert response.status_code == 422 and "transcript" in response.json()["detail"]
    assert stored() == []


# ---------------------------------------------------------------- plain text file

TEXT_FILE = """Title: Northwind Foods: working capital review
Date: 2026-08-12
Participants: Priya Shah <priya.shah@northwindfoods.example>; Rao <rao@roibypractus.com>
Action items: Send phased plan; Book CFO call
---
Priya: We need the plan phased by quarter.
Rao: Understood.
"""


def test_a_plain_text_file_is_one_meeting(client, debug_key):
    response = upload(client, TEXT_FILE, debug_key, content_type="text/plain")
    assert response.status_code == 200 and response.json()["stored"] == 1 and response.json()["matched"] == 1
    [meeting] = stored()
    assert meeting.transcript == "Priya: We need the plan phased by quarter.\nRao: Understood."
    assert meeting.action_items == ["Send phased plan", "Book CFO call"] and meeting.source == "upload"
    assert meeting.participant_domains == ["northwindfoods.example", "roibypractus.com"]


def test_a_text_file_without_the_divider_or_the_title_is_rejected(client, debug_key):
    assert upload(client, "Title: x\nDate: 2026-08-12\njust talk", debug_key, content_type="text/plain").status_code == 422
    assert upload(client, "Date: 2026-08-12\n---\ntalk", debug_key, content_type="text/plain").status_code == 422
    assert stored() == []


def test_another_content_type_is_rejected(client, debug_key):
    assert upload(client, "<xml/>", debug_key, content_type="application/xml").status_code == 422


# ---------------------------------------------------------------- the listing

def test_the_listing_shows_what_is_stored_and_what_matched(client, debug_key):
    upload(client, [entry(), entry(title="Internal planning", date="2026-08-25", participants=["a@gmail.com"])], debug_key)
    body = client.get("/api/transcripts", headers={"X-Debug-Key": debug_key}).json()
    assert (body["total"], body["matched"], body["unmatched"]) == (2, 1, 1)
    assert [m["title"] for m in body["meetings"]] == ["Internal planning", "Northwind Foods: working capital review"]  # newest first
    northwind = body["meetings"][1]
    assert northwind["matched_deals"] == ["Northwind Foods - Working capital"] and northwind["source"] == "upload"
    assert northwind["transcript_chars"] > 0
    assert "transcript" not in northwind  # the text itself is never listed


def test_the_listing_can_be_narrowed_by_source(client, debug_key):
    upload(client, [entry()], debug_key)
    with get_sessionmaker()() as s:
        s.add(Meeting(meeting_id="read-1", title="Webhook call", participants=[], participant_domains=[], source="webhook"))
        s.commit()
    key = {"X-Debug-Key": debug_key}
    assert client.get("/api/transcripts", headers=key).json()["total"] == 2
    only = client.get("/api/transcripts?source=upload", headers=key).json()
    assert only["total"] == 1 and only["meetings"][0]["source"] == "upload"


def test_with_zoho_down_matching_falls_back_to_stored_deals_by_name_and_says_so(client, debug_key, monkeypatch):
    with get_sessionmaker()() as s:  # a run stores the deals locally
        run_week(s, settings(), now=NOW, fetch=lambda st: fetch_deals(st, connect_fn=zoho_connect(FakeConnection())),
                 llm_client=FakeClaude(claude_response(load("claude/nba_reply.json"))))
    monkeypatch.setattr(transcripts, "fetch_deals", lambda s: (_ for _ in ()).throw(RuntimeError("Zoho down")))
    body = upload(client, [entry(participants=[])], debug_key).json()
    assert body["matching"] == "local deals, company names only" and body["matched"] == 1
    domain_only = upload(client, [entry(title="Quarterly catch-up")], debug_key).json()
    assert domain_only["matched"] == 0  # without Zoho there are no outreach-log domains to match on


# ---------------------------------------------------------------- a card uses it

def test_an_uploaded_meeting_reaches_the_deal_card_as_an_excerpt_not_a_transcript(client, debug_key):
    upload(client, [entry(date=NOW.date().isoformat())], debug_key)
    with get_sessionmaker()() as s:
        [record] = recent_meetings(s, NOW)
        assert record.source == "upload" and len(record.transcript_excerpt) == 2000  # only the opening is read back
        run_week(s, settings(), now=NOW, fetch=lambda st: fetch_deals(st, connect_fn=zoho_connect(FakeConnection())),
                 llm_client=FakeClaude(claude_response(dict(load("claude/nba_reply.json"), evidence_ids=["zoho.stage"]))),
                 deal_zoho_id="598723000011234001")
        from api.models import ContextCard
        facts = [f for c in s.scalars(select(ContextCard)) for f in (c.conversation or {}).get("facts", [])
                 if f["source"] == "readai"]
    assert len(facts) == 1
    value = facts[0]["value"]
    assert "Transcript excerpt: Priya: We need the plan phased" in value and "Action items: Send phased plan" in value
    assert len(value) < 700  # an excerpt, never the whole transcript


def test_a_summary_beats_an_excerpt(client, debug_key):
    upload(client, [entry(date=NOW.date().isoformat(), summary="CFO wants the plan phased by quarter.")], debug_key)
    with get_sessionmaker()() as s:
        run_week(s, settings(), now=NOW, fetch=lambda st: fetch_deals(st, connect_fn=zoho_connect(FakeConnection())),
                 llm_client=FakeClaude(claude_response(dict(load("claude/nba_reply.json"), evidence_ids=["zoho.stage"]))),
                 deal_zoho_id="598723000011234001")
        from api.models import ContextCard
        value = next(f["value"] for c in s.scalars(select(ContextCard)) for f in (c.conversation or {}).get("facts", [])
                     if f["source"] == "readai")
    assert "Summary: CFO wants the plan phased" in value and "Transcript excerpt" not in value

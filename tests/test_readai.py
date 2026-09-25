"""The Read.ai webhook: signature (raw or base64 key), dedupe, storage, and linking to deals."""
import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from api.db import get_sessionmaker
from api.engine.linking import deal_identity, meeting_belongs
from api.models import Meeting
from api.sources.readai import meeting_fields, recent_meetings, verify_signature
from api.sources.zoho import Reachout, row_to_deal
from tests.fakes import load

# Read.ai's documented example payload (as used by the ICP bot's own tests), with a client domain.
PAYLOAD = {
    "session_id": "SESSIONID", "trigger": "meeting_end", "title": "Northwind Foods working capital review",
    "start_time": "2026-09-18T05:00:00Z", "end_time": "2026-09-18T06:00:00Z",
    "participants": [
        {"name": "Priya Shah", "first_name": "Priya", "last_name": "Shah", "email": "Priya.Shah@NorthwindFoods.example"},
        {"name": "Rao", "email": "rao@roibypractus.com"},
        {"name": "Guest", "email": None},
    ],
    "owner": {"name": "Rao", "email": "rao@roibypractus.com"},
    "summary": "CFO wants a phased plan; board meets in October.",
    "action_items": [{"text": "Send phased plan"}, {"text": "Share a receivables case"}],
    "key_questions": [{"text": "What does phase one cost?"}], "topics": [{"text": "Working capital"}],
    "report_url": "https://app.read.ai/analytics/meetings/SESSIONID",
    "chapter_summaries": [{"title": "Scope", "description": "Phasing", "topics": [{"text": "Working capital"}]}],
    "transcript": {"speaker_blocks": [{"start_time": "1", "end_time": "2", "speaker": {"name": "Priya"}, "words": "secret words"}],
                   "speakers": [{"name": "Priya"}]},
    "platform_meeting_id": "abc-defg-hij", "platform": "meet", "request_id": "REQ-1",
}
KEY_B64 = "GHx4YT/StkcArjYPypjFG48FbZvgzBuDBOz5pCecbro="  # the ICP bot's test key (base64)
KEY_RAW = "a-raw-signing-key-from-read-ai"


def sign(body: bytes, key: bytes) -> str:
    return hmac.new(key, body, hashlib.sha256).hexdigest()


def body(**overrides) -> bytes:
    return json.dumps({**PAYLOAD, **overrides}).encode()


# ---------------------------------------------------------------- signature

def test_base64_key_is_decoded_like_the_icp_bot():
    raw = body()
    assert verify_signature(raw, sign(raw, base64.b64decode(KEY_B64)), KEY_B64)


def test_raw_key_is_used_as_is():
    raw = body()
    assert verify_signature(raw, sign(raw, KEY_RAW.encode()), KEY_RAW)
    assert verify_signature(raw, "sha256=" + sign(raw, KEY_RAW.encode()), KEY_RAW)  # tolerated prefix
    assert verify_signature(raw, sign(raw, KEY_RAW.encode()).upper(), KEY_RAW)


def test_tampered_body_wrong_key_and_unset_key_are_refused():
    raw = body()
    good = sign(raw, KEY_RAW.encode())
    assert not verify_signature(raw.replace(b"phased", b"Phased"), good, KEY_RAW)
    assert not verify_signature(raw, good, "some-other-key")
    assert not verify_signature(raw, good, "") and not verify_signature(raw, None, KEY_RAW)
    assert not verify_signature(raw, "not hex \xff", KEY_RAW)


# ---------------------------------------------------------------- the endpoint

@pytest.fixture()
def webhook(client, monkeypatch):
    from api.config import get_settings

    monkeypatch.setenv("READAI_WEBHOOK_SECRET", KEY_RAW)
    get_settings.cache_clear()

    def post(raw: bytes, signature: str | None = "auto"):
        headers = {"content-type": "application/json"}
        if signature == "auto":
            signature = sign(raw, KEY_RAW.encode())
        if signature is not None:
            headers["X-Read-Signature"] = signature
        return client.post("/api/webhooks/readai", content=raw, headers=headers)
    return post


def meetings_count():
    with get_sessionmaker()() as s:
        return s.scalar(select(func.count()).select_from(Meeting))


@pytest.mark.parametrize("prefix", ["", "/reports/rasa-incanta"])
def test_a_signed_meeting_is_stored_without_its_transcript(client, monkeypatch, prefix):
    from api.config import get_settings

    monkeypatch.setenv("READAI_WEBHOOK_SECRET", KEY_RAW)
    get_settings.cache_clear()
    raw = body()
    response = client.post(prefix + "/api/webhooks/readai", content=raw, headers={"X-Read-Signature": sign(raw, KEY_RAW.encode())})
    assert response.status_code == 202
    with get_sessionmaker()() as s:
        m = s.scalar(select(Meeting))
        assert m.meeting_id == "SESSIONID" and m.title.startswith("Northwind")
        assert m.participant_domains == ["northwindfoods.example", "roibypractus.com"]
        assert m.action_items == ["Send phased plan", "Share a receivables case"]
        assert "secret words" not in json.dumps({c.name: str(getattr(m, c.name)) for c in Meeting.__table__.columns})


def test_unsigned_forged_or_unconfigured_deliveries_get_401(client, webhook, monkeypatch):
    from api.config import get_settings

    raw = body()
    assert webhook(raw, signature=None).status_code == 401
    assert webhook(raw, signature=sign(raw, b"forged")).status_code == 401
    monkeypatch.delenv("READAI_WEBHOOK_SECRET")
    get_settings.cache_clear()
    assert webhook(raw, signature=sign(raw, KEY_RAW.encode())).status_code == 401  # unset key refuses
    assert meetings_count() == 0


def test_duplicates_and_meeting_start_are_acknowledged_not_stored(webhook):
    assert webhook(body()).status_code == 202
    assert webhook(body()).status_code == 204  # same request_id: Read.ai retrying
    assert webhook(body(session_id="S2", request_id="REQ-2", trigger="meeting_start")).status_code == 204
    assert meetings_count() == 1


def test_a_redelivered_meeting_updates_in_place(webhook):
    webhook(body())
    webhook(body(request_id="REQ-3", summary="Updated summary"))
    with get_sessionmaker()() as s:
        assert s.scalar(select(func.count()).select_from(Meeting)) == 1
        assert s.scalar(select(Meeting)).summary == "Updated summary"


def test_malformed_signed_payload_is_a_500_so_read_ai_retries(webhook):
    assert webhook(b'{"trigger": "meeting_end"}').status_code == 500  # no session_id
    assert webhook(b"not json").status_code == 500


def test_oversized_body_is_refused(webhook):
    assert webhook(b" " * (10 * 1024 * 1024 + 1)).status_code == 413


# ---------------------------------------------------------------- linking meetings to deals

def northwind(reachout_email="priya.shah@northwindfoods.example"):
    deal = row_to_deal(load("zoho/deals.json")[0])
    return deal, [Reachout(deal_zoho_id=deal.zoho_id, email=reachout_email)]


def stored(**overrides):
    from api.sources.readai import MeetingRecord

    f = meeting_fields({**PAYLOAD, **overrides})
    return MeetingRecord(f["meeting_id"], f["title"], f["start_time"], f["summary"], tuple(f["participants"]),
                         tuple(f["participant_domains"]), tuple(f["action_items"]), (), ())


def test_a_meeting_links_by_participant_domain_or_company_in_title():
    deal, reachouts = northwind()
    ident = deal_identity(deal, reachouts)
    assert ident.domains == {"northwindfoods.example"}
    assert meeting_belongs(ident, stored(title="Weekly sync"))  # domain match
    no_domain = deal_identity(deal, [])
    assert meeting_belongs(no_domain, stored(participants=[]))  # "Northwind Foods" in the title
    assert not meeting_belongs(no_domain, stored(participants=[], title="Northwinds of change offsite"))  # whole words only


def test_practus_and_free_mail_domains_never_identify_a_company():
    deal, _ = northwind()
    ident = deal_identity(deal, [Reachout(deal_zoho_id="1", email="x@gmail.com"),
                                 Reachout(deal_zoho_id="1", email="rao@roibypractus.com")])
    assert ident.domains == frozenset()
    other = stored(title="Internal review", participants=[{"name": "Rao", "email": "rao@roibypractus.com"}])
    assert not meeting_belongs(ident, other)  # a Practus-only meeting is nobody's client meeting


def test_recent_meetings_respects_the_180_day_window(migrated):
    with get_sessionmaker()() as s:
        s.add(Meeting(**meeting_fields(PAYLOAD)))
        s.add(Meeting(**meeting_fields({**PAYLOAD, "session_id": "OLD", "request_id": "OLD", "start_time": "2025-01-01T00:00:00Z"})))
        s.commit()
        ids = [m.meeting_id for m in recent_meetings(s, datetime(2026, 9, 24, tzinfo=timezone.utc))]
    assert ids == ["SESSIONID"]

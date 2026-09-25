"""Read.ai meetings, delivered by Read.ai's signed workspace webhook (trigger `meeting_end`).

Adapted from the ICP bot (`readai_webhook.py`, `readai_common.py`, `meetings_store.py`). Read.ai
signs each delivery with HMAC-SHA256 over the raw body; the hex digest arrives in
`X-Read-Signature`. The ICP bot base64-decodes the signing key first; the key Read.ai shows can
also be used as-is, so both are tried, each compared in constant time over bytes. An unset key
refuses every delivery: a signature is the only thing that makes a public webhook trustworthy.

Stored per meeting: title, times, platform, report link, summary, participants (with their
email domains, for matching to deals), action items, key questions, topics and chapter summaries.
Transcripts are deliberately not stored: nothing uses them, and only excerpts ever go to a model.

Read.ai retries a failed delivery up to 6 times and dedupes on `request_id`; duplicates and
`meeting_start` payloads are acknowledged without storing anything.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.models import Meeting

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 10 * 1024 * 1024  # a meeting with a long transcript is well under this


class WebhookRejected(RuntimeError):
    """Unverifiable delivery: the route answers 401, never 2xx."""


def _candidate_keys(secret: str) -> list[bytes]:
    """The key as given, and its base64 decoding when it is valid base64 (the ICP bot's format)."""
    secret = secret.strip()
    keys = [secret.encode()]
    try:
        decoded = base64.b64decode(secret, validate=True)
        if decoded and decoded not in keys:
            keys.append(decoded)
    except (binascii.Error, ValueError):
        pass
    return keys


def verify_signature(raw_body: bytes, signature_header: str | None, secret: str) -> bool:
    if not secret or not secret.strip() or not signature_header:
        return False
    given = signature_header.strip().lower()
    if given.startswith("sha256="):
        given = given[len("sha256="):]
    for key in _candidate_keys(secret):
        expected = hmac.new(key, raw_body, hashlib.sha256).hexdigest()
        if hmac.compare_digest(expected.encode(), given.encode("utf-8", "replace")):
            return True
    return False


def _parse_time(value) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def email_domain(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    return email.rsplit("@", 1)[1].strip().lower() or None


def _people(items) -> list[dict]:
    people = []
    for p in items or []:
        if isinstance(p, dict):
            people.append({"name": p.get("name") or " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x) or None,
                           "email": (p.get("email") or "").strip().lower() or None})
    return people


def _texts(items) -> list[str]:
    return [t for t in ((i.get("text") if isinstance(i, dict) else i) for i in items or []) if t]


def meeting_fields(payload: dict) -> dict:
    """The stored fields of one `meeting_end` payload (Read.ai's documented shape)."""
    participants = _people(payload.get("participants"))
    owner = _people([payload.get("owner")])[0] if isinstance(payload.get("owner"), dict) else {}
    domains = sorted({d for d in (email_domain(p["email"]) for p in [*participants, owner]) if d})
    return {
        "meeting_id": str(payload["session_id"]),
        "request_id": payload.get("request_id"),
        "title": payload.get("title"),
        "start_time": _parse_time(payload.get("start_time")),
        "end_time": _parse_time(payload.get("end_time")),
        "platform": payload.get("platform"),
        "report_url": payload.get("report_url"),
        "summary": payload.get("summary"),
        "owner": owner,
        "participants": participants,
        "participant_domains": domains,
        "action_items": _texts(payload.get("action_items")),
        "key_questions": _texts(payload.get("key_questions")),
        "topics": _texts(payload.get("topics")),
        "chapter_summaries": [{"title": c.get("title"), "description": c.get("description")}
                              for c in payload.get("chapter_summaries") or [] if isinstance(c, dict)],
        "source": "webhook",
    }


def handle_delivery(session: Session, raw_body: bytes, signature_header: str | None, secret: str) -> str:
    """Verify, dedupe and store one delivery. Returns "stored", "duplicate" or "ignored";
    raises WebhookRejected for anything unverifiable, ValueError for a malformed payload."""
    if not secret or not secret.strip():
        raise WebhookRejected("READAI_WEBHOOK_SECRET is not configured.")
    if not signature_header:
        raise WebhookRejected("Missing X-Read-Signature header.")
    if not verify_signature(raw_body, signature_header, secret):
        raise WebhookRejected("Signature verification failed.")

    payload = json.loads(raw_body)
    if not isinstance(payload, dict) or not payload.get("session_id"):
        raise ValueError("Payload has no session_id.")
    if payload.get("trigger") == "meeting_start":
        return "ignored"  # only metadata exists before the report; the meeting_end delivery follows
    request_id = payload.get("request_id")
    if request_id and session.scalar(select(Meeting.id).where(Meeting.request_id == request_id).limit(1)):
        return "duplicate"

    fields = meeting_fields(payload)
    meeting = session.scalar(select(Meeting).where(Meeting.meeting_id == fields["meeting_id"]))
    if meeting is None:
        meeting = Meeting(**fields)
        session.add(meeting)
    else:  # a later delivery for the same meeting (e.g. a regenerated report) replaces the fields
        for key, value in fields.items():
            setattr(meeting, key, value)
    session.commit()
    return "stored"


@dataclass(frozen=True)
class MeetingRecord:
    """A stored meeting, detached from the session, for building cards in a run."""
    meeting_id: str
    title: str | None
    start_time: datetime | None
    summary: str | None
    participants: tuple[dict, ...]
    participant_domains: tuple[str, ...]
    action_items: tuple[str, ...]
    key_questions: tuple[str, ...]
    topics: tuple[str, ...]


def recent_meetings(session: Session, now: datetime, since_days: int = 180) -> list[MeetingRecord]:
    """Meetings from the last `since_days`, newest first (the ICP bot's window)."""
    cutoff = now - timedelta(days=since_days)
    rows = session.scalars(select(Meeting).order_by(Meeting.start_time.desc()))
    out = []
    for m in rows:
        started = m.start_time if (m.start_time is None or m.start_time.tzinfo) else m.start_time.replace(tzinfo=timezone.utc)
        if started is not None and started < cutoff:
            continue
        out.append(MeetingRecord(m.meeting_id, m.title, started, m.summary, tuple(m.participants or []),
                                 tuple(m.participant_domains or []), tuple(m.action_items or []),
                                 tuple(m.key_questions or []), tuple(m.topics or [])))
    return out

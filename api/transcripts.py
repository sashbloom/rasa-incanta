"""Past meetings, fed in by hand. Read.ai's webhook only reaches forward from the day it went live, so call
history before that comes in here: a JSON array of meetings, or one meeting as a plain text file.

They are stored in `meetings`, the same shape as webhook meetings, with `source = "upload"` and the
transcript kept alongside. A deal's card only ever carries the meeting's summary (or, with none, a short
excerpt of the transcript's opening): a whole transcript never goes to a model (CLAUDE.md rule 7).

Matching a meeting to a deal is the webhook's own (`engine.linking.meeting_belongs`): the company named in
the title, or a participant's company email domain. Domains come from the people on each deal's Zoho
outreach log, so the live Zoho read is tried first; if Zoho cannot be reached, deals already stored locally
are matched on company name alone and the response says so.

Plain text file layout: `Key: value` lines (Title, Date, Participants, Action items, Summary), then a line of
`---`, then the transcript. Participants are `Name <email>` separated by commas or semicolons; action items are
separated by semicolons.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.config import Settings
from api.engine.linking import DealIdentity, deal_identity, meeting_belongs
from api.models import Deal, Meeting
from api.sources.readai import email_domain
from api.sources.zoho import ZohoDeal, fetch_deals

logger = logging.getLogger(__name__)

SOURCE = "upload"
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_TRANSCRIPT_CHARS = 1_000_000


class BadUpload(ValueError):
    """An upload that cannot be read: the route answers 422 with this message."""


class UploadedMeeting(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str = Field(min_length=1, max_length=500)
    date: datetime
    participants: list[str | dict] = Field(default_factory=list, max_length=300)
    transcript: str = Field(min_length=1, max_length=MAX_TRANSCRIPT_CHARS)
    action_items: list[str] = Field(default_factory=list, max_length=100)
    summary: str | None = Field(default=None, max_length=5000)


# ---------------------------------------------------------------- reading an upload

_ANGLE = re.compile(r"^(?P<name>.*?)\s*<(?P<email>[^<>\s]+@[^<>\s]+)>\s*$")
_BARE_EMAIL = re.compile(r"^[^\s@<>]+@[^\s@<>]+$")


def person(item: str | dict) -> dict | None:
    """One participant as {name, email}: from "Name <email>", a bare address, a bare name, or a dict."""
    if isinstance(item, dict):
        name = (item.get("name") or " ".join(x for x in (item.get("first_name"), item.get("last_name")) if x) or "").strip()
        email = (item.get("email") or "").strip().lower()
    else:
        text = (item or "").strip()
        if (m := _ANGLE.match(text)):
            name, email = m.group("name").strip().strip('"'), m.group("email").lower()
        elif _BARE_EMAIL.match(text):
            name, email = "", text.lower()
        else:
            name, email = text, ""
    if not name and not email:
        return None
    return {"name": name or None, "email": email or None}


def _split(value: str, separators: str) -> list[str]:
    return [p.strip() for p in re.split(f"[{separators}\\n]", value) if p.strip()]


def parse_text_file(text: str) -> dict:
    """One meeting from the plain text layout in the module docstring."""
    lines = text.replace("\r\n", "\n").lstrip("﻿").split("\n")
    try:
        cut = next(i for i, line in enumerate(lines) if line.strip() == "---")
    except StopIteration:
        raise BadUpload("A text upload needs Title: and Date: lines, then a line with ---, then the transcript.") from None
    head: dict[str, str] = {}
    last = None
    for line in lines[:cut]:
        if (m := re.match(r"^\s*(title|date|participants|action items|summary)\s*:\s*(.*)$", line, re.IGNORECASE)):
            last = m.group(1).lower()
            head[last] = m.group(2).strip()
        elif line.strip() and last:  # a long Participants or Summary line carried over
            head[last] += " " + line.strip()
    transcript = "\n".join(lines[cut + 1:]).strip()
    return {"title": head.get("title", ""), "date": head.get("date", ""), "transcript": transcript,
            "participants": _split(head.get("participants", ""), ",;"),
            "action_items": _split(head.get("action items", ""), ";"), "summary": head.get("summary") or None}


def parse_upload(content_type: str | None, raw: bytes) -> list[UploadedMeeting]:
    """The meetings in an upload: a JSON array (or one object, or {"meetings": [...]}), or a plain text file."""
    kind = (content_type or "").split(";")[0].strip().lower()
    text = raw.decode("utf-8-sig", errors="replace")
    if kind == "application/json" or (not kind and text.lstrip().startswith(("[", "{"))):
        try:
            data: Any = json.loads(text)
        except ValueError as exc:
            raise BadUpload(f"The body is not valid JSON ({exc}).") from None
        if isinstance(data, dict):
            data = data["meetings"] if isinstance(data.get("meetings"), list) else [data]
        if not isinstance(data, list) or not data:
            raise BadUpload("Send a JSON array of meetings, each with title, date and transcript.")
    elif kind in ("text/plain", "text/markdown", ""):
        data = [parse_text_file(text)]
    else:
        raise BadUpload(f"Send application/json or text/plain, not {kind}.")
    try:
        return TypeAdapter(list[UploadedMeeting]).validate_python(data)
    except ValidationError as exc:
        problems = [f"meeting {'.'.join(str(p) for p in e['loc'][:1])}: {'.'.join(str(p) for p in e['loc'][1:])} {e['msg']}".strip()
                    for e in exc.errors()[:8]]
        raise BadUpload("; ".join(problems)) from None


# ---------------------------------------------------------------- storing

def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def meeting_key(entry: UploadedMeeting) -> str:
    """A stable id, so uploading the same meeting twice stores it once."""
    digest = hashlib.sha256(f"{entry.title.strip()}|{_aware(entry.date).isoformat()}|{entry.transcript[:5000]}".encode()).hexdigest()
    return f"upload:{digest[:32]}"


def to_meeting(entry: UploadedMeeting) -> Meeting:
    people = [p for p in (person(i) for i in entry.participants) if p]
    domains = sorted({d for d in (email_domain(p["email"]) for p in people) if d})
    return Meeting(
        meeting_id=meeting_key(entry), request_id=None, title=entry.title.strip(), start_time=_aware(entry.date),
        end_time=None, platform=None, report_url=None, summary=(entry.summary or "").strip() or None, owner={},
        participants=people, participant_domains=domains, action_items=[a.strip() for a in entry.action_items if a.strip()],
        key_questions=[], topics=[], chapter_summaries=[], source=SOURCE, transcript=entry.transcript.strip(),
    )


def store(session: Session, entries: list[UploadedMeeting]) -> list[tuple[Meeting, bool]]:
    """Store each meeting not already stored. Returns (meeting, newly stored) in upload order; a repeat of a
    stored meeting (or of one earlier in the same upload) is left as it is."""
    out: list[tuple[Meeting, bool]] = []
    for entry in entries:
        key = meeting_key(entry)
        existing = session.scalar(select(Meeting).where(Meeting.meeting_id == key))
        if existing is not None:
            out.append((existing, False))
            continue
        meeting = to_meeting(entry)
        session.add(meeting)
        session.flush()  # a repeat later in this upload now finds it
        out.append((meeting, True))
    session.commit()
    return out


# ---------------------------------------------------------------- matching to deals

def deal_identities(session: Session, settings: Settings, fetch=None) -> tuple[list[tuple[str, DealIdentity]], str]:
    """(deal name, identity) for every deal, and how they were built: "zoho" (company names and the email
    domains on each outreach log) or "local deals, company names only" when Zoho cannot be read."""
    fetch = fetch or fetch_deals  # looked up at call time, so tests can replace it
    try:
        zoho = fetch(settings)
    except Exception:
        logger.exception("Zoho read for transcript matching failed")
        zoho = None
    if zoho is not None and zoho.ok and zoho.deals:
        return [(z.name, deal_identity(z, zoho.reachouts.get(z.zoho_id, []))) for z in zoho.deals], "zoho"
    local = list(session.scalars(select(Deal).where(Deal.is_active.is_(True))))
    return ([(d.name, deal_identity(ZohoDeal(zoho_id=d.zoho_id, name=d.name, stage=d.stage, account_name=d.account_name)))
             for d in local], "local deals, company names only")


def matched_deals(meeting: Meeting, identities: list[tuple[str, DealIdentity]]) -> list[str]:
    return sorted({name for name, identity in identities if meeting_belongs(identity, meeting)})


def summarise(meeting: Meeting, matches: list[str], transcript_chars: int | None = None) -> dict:
    return {
        "id": meeting.meeting_id, "title": meeting.title, "date": _aware(meeting.start_time).isoformat() if meeting.start_time else None,
        "source": meeting.source, "participants": [p.get("name") or p.get("email") for p in meeting.participants or []],
        "domains": list(meeting.participant_domains or []), "action_items": len(meeting.action_items or []),
        "has_summary": bool(meeting.summary), "transcript_chars": transcript_chars,
        "matched_deals": matches, "matched": bool(matches),
    }


def store_and_match(session: Session, settings: Settings, entries: list[UploadedMeeting], fetch=None) -> dict:
    stored = store(session, entries)
    identities, how = deal_identities(session, settings, fetch)
    rows = []
    for (meeting, created), entry in zip(stored, entries):
        rows.append({**summarise(meeting, matched_deals(meeting, identities), len(entry.transcript)),
                     "status": "stored" if created else "already stored"})
    return {
        "stored": sum(created for _, created in stored), "duplicates": sum(not created for _, created in stored),
        "matched": sum(r["matched"] for r in rows), "unmatched": sum(not r["matched"] for r in rows),
        "matching": how, "meetings": rows,
    }


def listing(session: Session, settings: Settings, source: str | None = None, limit: int = 500, fetch=None) -> dict:
    """Every stored meeting (newest first) with the deals it matches, so what matched and what did not is visible."""
    query = select(Meeting, func.length(Meeting.transcript)).order_by(Meeting.start_time.desc()).limit(limit)
    if source:
        query = query.where(Meeting.source == source)
    rows = list(session.execute(query))
    identities, how = deal_identities(session, settings, fetch)
    meetings = [summarise(m, matched_deals(m, identities), chars) for m, chars in rows]
    return {"total": len(meetings), "matched": sum(m["matched"] for m in meetings),
            "unmatched": sum(not m["matched"] for m in meetings), "matching": how, "meetings": meetings}

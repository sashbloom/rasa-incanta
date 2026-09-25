"""Adapter: the ICP bot's `meetings_store` read interface, served by our `meetings` table.

`evidence_assembler.gather_meeting_evidence()` calls `find_by_participant_domain(domain,
since_days)` and reads `title`, `start_time` and `summary` from each meeting. Meetings arrive
through our Read.ai webhook (`api/sources/readai.py`) instead of the ICP bot's SQLite store.
"""
from __future__ import annotations

from datetime import datetime, timezone

from api.db import get_sessionmaker
from api.sources.readai import recent_meetings


def find_by_participant_domain(domain: str, since_days: int | None = None) -> list[dict]:
    domain = (domain or "").strip().lower()
    if not domain:
        return []
    with get_sessionmaker()() as session:
        meetings = recent_meetings(session, datetime.now(timezone.utc), since_days=since_days or 36500)
    return [
        {"meeting_id": m.meeting_id, "title": m.title, "summary": m.summary,
         "start_time": m.start_time.isoformat() if m.start_time else None}
        for m in meetings if domain in m.participant_domains
    ]

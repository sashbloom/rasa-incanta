"""Adapter: the ICP bot's `mail_client` interface, served by our delegated Outlook source.

`evidence_assembler.gather_mailbox_evidence()` calls `search_configured_mailboxes(company_name)`
and reads `subject`, `from.emailAddress.address`, `bodyPreview`, `receivedDateTime` and
`_mailbox` from each hit. This returns the same shape from `api/sources/outlook.py`, except that
the sender's address is replaced by their name (or company domain), so no email address reaches
the model. Raises on failure, as the ICP bot's did; the assembler turns that into a data gap.
"""
from __future__ import annotations

import httpx

from api.config import get_settings
from api.db import get_sessionmaker
from api.sources import outlook


def search_configured_mailboxes(query: str, top_per_mailbox: int = 10) -> list[dict]:
    settings = get_settings()
    if not outlook.configured(settings):
        return []
    with httpx.Client(timeout=30.0) as http, get_sessionmaker()() as session:
        token = outlook.access_token(session, settings, http)
        hits = outlook.search_messages(http, token, settings.myrah_mailbox, query, top=top_per_mailbox)
    for message in hits:
        sender = (message.get("from") or {}).get("emailAddress") or {}
        address = sender.get("address") or ""
        sender["address"] = sender.get("name") or (address.rsplit("@", 1)[1] if "@" in address else "unknown")
        message["_mailbox"] = "Myrah's mailbox"
    return hits

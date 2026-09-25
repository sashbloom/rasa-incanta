"""Key points from a deal's recent mail, with the extraction model (LLM_MODEL_EXTRACTION).

Only each mail's subject, date, sender name and Graph's short preview (about 255 characters) go
to the model, never whole bodies (CLAUDE.md rule 7). The model may only restate what a preview
says; a failed call means no key points, and the card falls back to the trimmed preview.
"""
from __future__ import annotations

import logging
from typing import Any

import anthropic
from pydantic import BaseModel

from api.sources.outlook import Mail

logger = logging.getLogger(__name__)

MAX_POINTS_PER_MAIL = 2
MAX_WORDS_PER_POINT = 25

SYSTEM = """You summarise business email previews for a consulting firm's deal team. For each \
numbered mail, give at most two key points: decisions, requests, dates, objections, next steps or \
numbers the preview actually states. Restate only what the preview says; never guess what the rest \
of the mail contains. Each point is at most 20 words. Give a mail no points if its preview says \
nothing substantive (a greeting, a signature, an auto-reply)."""


class MailPoints(BaseModel):
    index: int
    key_points: list[str]


class KeyPoints(BaseModel):
    mails: list[MailPoints]


def _prompt(deal_name: str, mails: list[Mail]) -> str:
    lines = [f"Deal: {deal_name}", ""]
    for i, m in enumerate(mails, 1):
        when = m.received.date().isoformat() if m.received else "date unknown"
        lines.append(f"{i}. \"{m.subject}\" from {m.sender_name or m.sender_domain or 'unknown'}, {when}. "
                     f"Preview: {m.preview[:500]}")
    return "\n".join(lines)


def key_points(client: Any, model: str, deal_name: str, mails: list[Mail]) -> dict[int, list[str]]:
    """{mail index (1-based): points}. Empty on any failure: never raises."""
    if client is None or not mails:
        return {}
    try:
        response = client.messages.parse(model=model, max_tokens=2000, system=SYSTEM,
                                         messages=[{"role": "user", "content": _prompt(deal_name, mails)}],
                                         output_format=KeyPoints)
    except (anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
        logger.warning("Mail key points failed for %s: %s", deal_name, type(exc).__name__)
        return {}
    except Exception:
        logger.exception("Mail key points failed for %s", deal_name)
        return {}
    parsed = getattr(response, "parsed_output", None)
    if getattr(response, "stop_reason", None) == "refusal" or parsed is None:
        return {}
    out: dict[int, list[str]] = {}
    for item in parsed.mails:
        if not 1 <= item.index <= len(mails):
            continue
        points = [" ".join(p.split()) for p in item.key_points if p and p.strip()]
        points = [p for p in points if len(p.split()) <= MAX_WORDS_PER_POINT][:MAX_POINTS_PER_MAIL]
        if points:
            out[item.index] = points
    return out

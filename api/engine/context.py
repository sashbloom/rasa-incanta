"""Build a deal's context card: the five input signals, each carrying the sourced facts
it rests on, plus gap flags for whatever a source could not provide.

Zoho fills `deal_state` (CRM fields), the Zoho contact in `stakeholder` when there is one, and
`conversation` from the deal's outreach log (who was met, how, when, and the notes). Setu fills
`capability` with the closest case studies (see engine/capability.py). ICP logic (copied from the ICP bot), Read.ai, Outlook and Setu arrive in Brick 3; until then
their signals are empty and flagged, so the page and the model both see the gap.

Every fact has a stable id (e.g. "zoho.stage"). A next best action cites fact ids, and
the engine rejects any citation that is not on the card.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo

from api.domain.gaps import Gap
from api.domain.stages import board_for
from api.engine.capability import describe, match_case_studies
from api.sources.setu import CaseStudy
from api.sources.zoho import Reachout, ZohoDeal

SIGNALS = ("account_fit", "stakeholder", "conversation", "capability", "deal_state")

# Outreach channels that count as a logged call or meeting, and as mail.
CALL_MEDIA = {"teams", "in-person", "in person", "phone", "call", "meeting", "video call", "zoom", "google meet"}
MAIL_MEDIA = {"email", "e-mail", "mail"}
MEDIUM_PHRASE = {"teams": "Teams meeting", "in-person": "In-person meeting", "in person": "In-person meeting",
                 "phone": "Phone call", "call": "Call", "email": "Email", "e-mail": "Email"}
MAX_TOUCHES = 5

# Contact details never go on a card or to the model, whichever field they were typed into.
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?<![\w,.])\+?\d(?:[\s().-]*\d){9,}(?![\w,.])")  # 10+ digits; amounts use commas


def scrub(text: str) -> str:
    return _PHONE.sub("[phone removed]", _EMAIL.sub("[email removed]", text))
MAX_NOTE_CHARS = 300  # only excerpts go to the model, never whole notes


@dataclass
class CardContent:
    account_fit: dict = field(default_factory=dict)
    stakeholder: dict = field(default_factory=dict)
    conversation: dict = field(default_factory=dict)
    capability: dict = field(default_factory=dict)
    deal_state: dict = field(default_factory=dict)
    gaps: list[str] = field(default_factory=list)

    def facts(self) -> list[dict]:
        return card_facts({s: getattr(self, s) for s in SIGNALS})


def card_facts(signals: dict[str, dict]) -> list[dict]:
    """All facts across a card's signals, in signal order."""
    return [fact for s in SIGNALS for fact in (signals.get(s) or {}).get("facts", [])]


def _day(on: datetime | date | None, tz: str) -> date | None:
    """The business-timezone day of an instant. Zoho timestamps are instants; taking .date()
    in UTC would put anything from 18:30 to 23:59 UTC on the wrong day in Kolkata."""
    if isinstance(on, datetime):
        return on.astimezone(ZoneInfo(tz)).date()
    return on


def _fact(key: str, label: str, value: str, on: datetime | date | None = None, tz: str = "Asia/Kolkata",
          source: str = "zoho") -> dict:
    day = _day(on, tz)
    return {
        "id": f"{source}.{key}",
        "source": source,
        "label": label,
        "value": scrub(value),
        "date": day.isoformat() if day else None,
    }


def _days_between(start: datetime | None, today: date, tz: str) -> int | None:
    return (today - _day(start, tz)).days if start else None


def _money(amount: float | None, currency: str | None) -> str | None:
    if amount is None:
        return None
    return f"{currency + ' ' if currency else ''}{amount:,.0f}"


def _excerpt(text: str, limit: int = MAX_NOTE_CHARS) -> str:
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "..."


def conversation_signal(reachouts: list[Reachout]) -> tuple[dict, list[Gap]]:
    """The conversation signal from the outreach log, newest first, and the gaps it leaves."""
    recent = sorted(reachouts, key=lambda r: r.on or date.min, reverse=True)[:MAX_TOUCHES]
    facts = []
    for i, r in enumerate(recent, 1):
        medium = (r.medium or "").strip().lower()
        what = MEDIUM_PHRASE.get(medium, "Contact logged")
        who = r.person or "the client"
        about = ", ".join(x for x in (r.designation, (r.role or "").lower() or None) if x)
        value = f"{what} with {who}" + (f" ({about})" if about else "")
        if r.spoc:
            value += f", led by {r.spoc}"
        if r.remarks:
            value += f". Notes: {_excerpt(r.remarks)}"
        if r.replied_on:
            value += f". Client replied {r.replied_on.isoformat()}"
        facts.append(_fact(f"reachout_{i}", "Outreach log", value, r.on))

    media = {(r.medium or "").strip().lower() for r in reachouts}
    gaps = []
    if not media & CALL_MEDIA:
        gaps.append(Gap.NO_CALL_LOGGED)
    if not media & MAIL_MEDIA:
        gaps.append(Gap.NO_MAIL)
    if not facts:
        return {}, gaps
    dated = [r.on for r in reachouts if r.on]
    return {"touches": len(reachouts), "last_touch": max(dated).isoformat() if dated else None, "facts": facts}, gaps


def capability_signal(deal: ZohoDeal, case_studies: list[CaseStudy] | None) -> tuple[dict, list[Gap]]:
    """The closest Setu case studies as citable facts; `None` means Setu was not available."""
    matches = match_case_studies(deal, case_studies or [])
    if not matches:
        return {}, [Gap.NO_SETU_MATCH]
    facts = []
    for i, m in enumerate(matches, 1):
        value = describe(m) + (f" {_excerpt(m.case.content)}" if m.case.content else "")
        facts.append(_fact(f"case_{i}", "Case study", value, source="setu"))
    return {
        "case_studies": [{"name": m.case.name, "industry": m.case.industry, "service_line": m.case.service_line,
                          "industry_match": m.industry_match, "keyword_hits": list(m.keyword_hits),
                          "score": m.score} for m in matches],
        "facts": facts,
    }, []


def build_card(deal: ZohoDeal, today: date, tz: str = "Asia/Kolkata", reachouts: list[Reachout] = (),
               case_studies: list[CaseStudy] | None = None) -> CardContent:
    days_in_stage = _days_between(deal.stage_entered_at, today, tz)
    days_since_touch = _days_between(deal.modified_at, today, tz)

    stage_value = deal.stage + (f", {days_in_stage} days in stage" if days_in_stage is not None else "")
    facts = [_fact("stage", "Stage", stage_value, deal.stage_entered_at, tz)]
    optional = [
        ("account", "Company", deal.account_name, None),
        ("owner", "Owner", deal.owner_name, None),
        ("ep_involved", "EP involved", ", ".join(deal.ep_involved) or None, None),
        ("el_involved", "EL involved", ", ".join(deal.el_involved) or None, None),
        ("industry", "Industry", deal.industry, None),
        ("city_state", "City", deal.city_state, None),
        ("lead_source", "Lead source", deal.lead_source, None),
        ("amount", "Amount", _money(deal.amount, deal.currency), None),
        ("nature_of_potential", "Nature of potential", deal.nature_of_potential, None),
        ("business_area", "Business area", deal.business_area, None),
        ("services", "Services", deal.services, None),
        ("category", "Category", deal.category, None),
        ("client_positioning", "How the client positions us", deal.client_positioning, None),
        ("client_perception", "How we perceive the client", deal.client_perception, None),
        ("proposal_sent", "Proposal sent", _day(deal.proposal_sent_on, tz).isoformat() if deal.proposal_sent_on else None,
         deal.proposal_sent_on),
        ("last_modified", "Last updated in Zoho",
         f"{days_since_touch} days ago" if days_since_touch is not None else None, deal.modified_at),
    ]
    facts += [_fact(k, label, v, on, tz) for k, label, v, on in optional if v]
    facts += [_fact(f"problem_statement_{i}", "Problem statement", p) for i, p in enumerate(deal.problem_statements, 1)]
    facts += [_fact(f"problem_area_{i}", "Problem area", p) for i, p in enumerate(deal.problem_areas, 1)]

    board = board_for(deal.stage)
    deal_state = {
        "stage": deal.stage,
        "board": board.value if board else None,
        "days_in_stage": days_in_stage,
        "last_touch": _day(deal.modified_at, tz).isoformat() if deal.modified_at else None,
        "days_since_touch": days_since_touch,
        "facts": facts,
    }

    conversation, conversation_gaps = conversation_signal(list(reachouts))
    capability, capability_gaps = capability_signal(deal, case_studies)
    gaps = [Gap.NO_ICP, *conversation_gaps, *capability_gaps]  # ICP scoring arrives later in Brick 3
    stakeholder: dict = {}
    if deal.contact_name:
        stakeholder = {
            "contact_name": deal.contact_name,
            "facts": [_fact("contact", "Contact", deal.contact_name)],
        }
    else:
        gaps.append(Gap.NO_CONTACT)

    return CardContent(stakeholder=stakeholder, conversation=conversation, capability=capability,
                       deal_state=deal_state, gaps=[g.value for g in gaps])

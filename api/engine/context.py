"""Build a deal's context card: the five input signals, each carrying the sourced facts
it rests on, plus gap flags for whatever a source could not provide.

- `deal_state`: Zoho CRM fields.
- `conversation`: the deal's Zoho outreach log, its Read.ai meetings and its Outlook mail.
- `capability`: Setu case studies (the ICP bot's P2 match with its Claude re-rank) and SMEs.
- `account_fit` and `stakeholder`: the ICP bot's scoring (engine/icp_signal.py).
- The Zoho contact, when there is one (the mirror has none, so `no_contact` stays).

Every fact has a stable id (e.g. "zoho.stage", "outlook.mail_1", "icp.recommendation"). A next
best action cites fact ids, and the engine rejects any citation that is not on the card. Emails
and phone numbers are scrubbed from every fact, whichever field they were typed into.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo

from api.domain.gaps import Gap
from api.domain.stages import board_for
from api.engine.capability import Capability, describe, match_case_studies
from api.sources.outlook import Mail
from api.sources.readai import MeetingRecord
from api.sources.setu import CaseStudy
from api.sources.zoho import Reachout, ZohoDeal

SIGNALS = ("account_fit", "stakeholder", "conversation", "capability", "deal_state")

# Outreach channels that count as a logged call or meeting, and as mail.
CALL_MEDIA = {"teams", "in-person", "in person", "phone", "call", "meeting", "video call", "zoom", "google meet"}
MAIL_MEDIA = {"email", "e-mail", "mail"}
MEDIUM_PHRASE = {"teams": "Teams meeting", "in-person": "In-person meeting", "in person": "In-person meeting",
                 "phone": "Phone call", "call": "Call", "email": "Email", "e-mail": "Email"}
MAX_TOUCHES = 5
MAX_MEETINGS = 5
MAX_NOTE_CHARS = 300  # only excerpts go to the model, never whole notes, summaries or bodies
MAX_NAMES = 4

# Contact details never go on a card or to the model, whichever field they were typed into.
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?<![\w,.])\+?\d(?:[\s().-]*\d){9,}(?![\w,.])")  # 10+ digits; amounts use commas


def scrub(text: str) -> str:
    return _PHONE.sub("[phone removed]", _EMAIL.sub("[email removed]", text))


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
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "..."


def _short(text: str, limit: int = 40) -> str:
    """A chip-sized label: what the fact is (a subject, a meeting title, a case-study name)."""
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "..."


def _names(people, limit: int = MAX_NAMES) -> str:
    names = [p.get("name") for p in people if isinstance(p, dict) and p.get("name")]
    return ", ".join(names[:limit]) + (f" and {len(names) - limit} more" if len(names) > limit else "")


# ---------------------------------------------------------------- conversation

def _reachout_facts(reachouts: list[Reachout], tz: str) -> list[dict]:
    facts = []
    for i, r in enumerate(sorted(reachouts, key=lambda r: r.on or date.min, reverse=True)[:MAX_TOUCHES], 1):
        what = MEDIUM_PHRASE.get((r.medium or "").strip().lower(), "Contact logged")
        about = ", ".join(x for x in (r.designation, (r.role or "").lower() or None) if x)
        value = f"{what} with {r.person or 'the client'}" + (f" ({about})" if about else "")
        if r.spoc:
            value += f", led by {r.spoc}"
        if r.remarks:
            value += f". Notes: {_excerpt(r.remarks)}"
        if r.replied_on:
            value += f". Client replied {r.replied_on.isoformat()}"
        facts.append(_fact(f"reachout_{i}", "Outreach log", value, r.on, tz))
    return facts


def _meeting_facts(meetings: list[MeetingRecord], tz: str) -> list[dict]:
    facts = []
    ordered = sorted(meetings, key=lambda m: m.start_time or datetime.min.replace(tzinfo=ZoneInfo("UTC")), reverse=True)
    for i, m in enumerate(ordered[:MAX_MEETINGS], 1):
        value = f"Meeting \"{m.title or 'untitled'}\""
        if m.participants:
            value += f" with {_names(m.participants)}"
        if m.summary:
            value += f". Summary: {_excerpt(m.summary)}"
        if m.action_items:
            value += ". Action items: " + "; ".join(_excerpt(a, 120) for a in m.action_items[:3])
        facts.append(_fact(f"meeting_{i}", _short(m.title or "Meeting"), value, m.start_time, tz, source="readai"))
    return facts


def _mail_facts(mails: list[Mail], points: dict[int, list[str]], tz: str) -> list[dict]:
    facts = []
    for i, m in enumerate(mails, 1):
        value = f"\"{m.subject}\" from {m.sender_name or m.sender_domain or 'unknown sender'}"
        if points.get(i):
            value += ". Key points: " + "; ".join(points[i])
        elif m.preview:
            value += f". Preview: {_excerpt(m.preview, 250)}"
        facts.append(_fact(f"mail_{i}", _short(m.subject), value, m.received, tz, source="outlook"))
    return facts


def conversation_signal(reachouts: list[Reachout], meetings: list[MeetingRecord] = (), mails: list[Mail] = (),
                        mail_points: dict[int, list[str]] | None = None,
                        tz: str = "Asia/Kolkata") -> tuple[dict, list[Gap]]:
    """Outreach log, Read.ai meetings and Outlook mail, each newest first, and the gaps left.
    A logged call or meeting on the outreach log, or any Read.ai meeting, clears no_call_logged;
    an email on the outreach log or any Outlook mail clears no_mail."""
    reachouts, meetings, mails = list(reachouts), list(meetings), list(mails)
    facts = _reachout_facts(reachouts, tz) + _meeting_facts(meetings, tz) + _mail_facts(mails, mail_points or {}, tz)

    media = {(r.medium or "").strip().lower() for r in reachouts}
    gaps = []
    if not (media & CALL_MEDIA) and not meetings:
        gaps.append(Gap.NO_CALL_LOGGED)
    if not (media & MAIL_MEDIA) and not mails:
        gaps.append(Gap.NO_MAIL)
    if not facts:
        return {}, gaps

    days = [d for d in (f["date"] for f in facts) if d]
    signal = {"touches": len(reachouts), "last_touch": max(days) if days else None, "facts": facts}
    if meetings:
        latest = max(meetings, key=lambda m: m.start_time or datetime.min.replace(tzinfo=ZoneInfo("UTC")))
        signal["meetings"] = len(meetings)
        signal["last_meeting"] = {"date": (_day(latest.start_time, tz) or date.min).isoformat() if latest.start_time else None,
                                  "title": latest.title}
    if mails:
        signal["mails"] = len(mails)
        signal["last_mail"] = {"date": _day(mails[0].received, tz).isoformat() if mails[0].received else None,
                               "subject": mails[0].subject, "key_points": (mail_points or {}).get(1, [])}
    return signal, gaps


# ---------------------------------------------------------------- capability

def capability_signal(deal: ZohoDeal, case_studies: list[CaseStudy] | None,
                      capability: Capability | None = None) -> tuple[dict, list[Gap]]:
    """Case studies (and SMEs) as citable facts. With `capability` (the re-ranked result) its picks
    are used; otherwise the strict deterministic match over `case_studies`. `None` for both means
    Setu was not available."""
    if capability is None:
        matches = match_case_studies(deal, case_studies or [])
        cases = [{"name": m.case.name, "industry": m.case.industry, "service_line": m.case.service_line,
                  "content": m.case.content, "text": describe(m), "industry_match": m.industry_match,
                  "keyword_hits": list(m.keyword_hits), "score": m.score} for m in matches]
        smes, reranked = [], False
    else:
        cases = [{**c, "text": f"{c['name']}" + (f" ({', '.join(x for x in (c.get('industry'), c.get('service_line')) if x)})"
                                                 if c.get("industry") or c.get("service_line") else "")
                  + f". Why it fits: {c['why']}"} for c in capability.cases]
        smes, reranked = capability.smes, capability.reranked

    facts = [_fact(f"case_{i}", _short(c["name"]), c["text"] + (f" {_excerpt(c['content'])}" if c.get("content") else ""),
                   source="setu") for i, c in enumerate(cases, 1)]
    facts += [_fact(f"sme_{i}", f"SME {s['name']}", f"{s['name']}" + (f" ({s['grade']})" if s.get("grade") else "")
                    + (f": {s['why']}" if s.get("why") else ""), source="setu") for i, s in enumerate(smes, 1)]
    if not cases:
        return ({"smes": smes, "facts": facts} if facts else {}), [Gap.NO_SETU_MATCH]
    return {
        "case_studies": [{k: v for k, v in c.items() if k not in ("content", "text")} for c in cases],
        "smes": smes, "reranked": reranked, "facts": facts,
    }, []


# ---------------------------------------------------------------- the card

def build_card(deal: ZohoDeal, today: date, tz: str = "Asia/Kolkata", reachouts: list[Reachout] = (),
               case_studies: list[CaseStudy] | None = None, *, meetings: list[MeetingRecord] = (),
               mails: list[Mail] = (), mail_points: dict[int, list[str]] | None = None,
               capability: Capability | None = None, icp: tuple[dict, dict, str] | None = None) -> CardContent:
    """`icp` is (account_fit, stakeholder, status) from engine/icp_signal.py, or None when the
    company has no ICP scoring yet."""
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

    conversation, conversation_gaps = conversation_signal(list(reachouts), list(meetings), list(mails), mail_points, tz)
    capability_sig, capability_gaps = capability_signal(deal, case_studies, capability)

    account_fit, icp_stakeholder, icp_status = icp if icp else ({}, {}, None)
    gaps = ([] if icp_status == "scored" else [Gap.NO_ICP]) + conversation_gaps + capability_gaps

    stakeholder_facts = list(icp_stakeholder.get("facts", []))
    stakeholder: dict = {k: v for k, v in icp_stakeholder.items() if k != "facts"}
    if deal.contact_name:
        stakeholder["contact_name"] = deal.contact_name
        stakeholder_facts.insert(0, _fact("contact", "Contact", deal.contact_name))
    else:
        gaps.append(Gap.NO_CONTACT)
    if stakeholder_facts:
        stakeholder["facts"] = stakeholder_facts

    return CardContent(account_fit=dict(account_fit), stakeholder=stakeholder, conversation=conversation,
                       capability=capability_sig, deal_state=deal_state, gaps=[g.value for g in gaps])

"""Persona research on the person a deal's team has met, added to the `stakeholder` signal.

The contact is taken from the deal's Zoho outreach log only (CLAUDE.md rule 3: never a different or
more senior person): the most recent touch with a client-side "Customer", else the most recent
touch with anyone named. api/persona/ researches them with Exa and fills the ICP bot's persona
schema; a few short, sourced facts from it go on the card (role, observed facts, one conversation
hook). Inferred bullets never do: an NBA must rest on facts.

Results are cached per contact (name and company) for PERSONA_CACHE_DAYS (8 weeks). Rows in
`contact_persona` are append-only; `ok` and `no_material` are reused, failures retry next run.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.engine.context import scrub
from api.engine.icp_signal import company_key
from api.models import ContactPersona
from api.sources.zoho import Reachout

REUSABLE = ("ok", "no_material")
MAX_OBSERVED = 2
MAX_CHARS = 300
_NOT_A_NAME = {"na", "n/a", "nil", "none", "-", "client", "the client", "team", "tbd", "unknown"}


@dataclass(frozen=True)
class Contact:
    name: str
    designation: str | None
    company: str

    @property
    def key(self) -> str:
        return contact_key(self.name, self.company)


def contact_key(name: str, company: str) -> str:
    return f"{' '.join(re.sub(r'[^a-z ]', ' ', name.lower()).split())}|{company_key(company)}"


def _usable_name(person: str | None) -> str | None:
    name = " ".join((person or "").split())
    if not name or name.lower() in _NOT_A_NAME or not re.search(r"[A-Za-z]{2}", name):
        return None
    if scrub(name) != name:  # an email address or phone number typed into the name field
        return None
    return name


def primary_contact(reachouts: list[Reachout], company: str) -> Contact | None:
    """The person to research for this deal, from its outreach log, or None."""
    named = [r for r in reachouts if _usable_name(r.person)]
    if not named or not company:
        return None
    newest = sorted(named, key=lambda r: r.on or date.min, reverse=True)
    pick = next((r for r in newest if (r.role or "").strip().lower() == "customer"), newest[0])
    designation = scrub(" ".join(pick.designation.split())) if pick.designation else None
    return Contact(name=_usable_name(pick.person), designation=designation or None, company=company)


def fresh_persona(session: Session, key: str, now: datetime, days: int) -> ContactPersona | None:
    """The newest reusable research on this contact younger than `days`, if any."""
    cutoff = now - timedelta(days=days)
    for row in session.scalars(select(ContactPersona).where(ContactPersona.contact_key == key)
                               .order_by(ContactPersona.computed_at.desc())):
        computed = row.computed_at if row.computed_at.tzinfo else row.computed_at.replace(tzinfo=timezone.utc)
        if computed < cutoff:
            return None
        if row.status in REUSABLE:
            return row
    return None


def _trim(text: str, limit: int = MAX_CHARS) -> str:
    text = " ".join(scrub(text or "").split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "..."


def facts_from(persona: dict, contact_name: str, computed_at: datetime) -> list[dict]:
    """The stakeholder facts a persona report supports. Observed material only, plus the
    report's own confidence, so the model can weigh it."""
    day = computed_at.date().isoformat()

    def fact(key: str, value: str) -> dict:
        return {"id": f"persona.{key}", "source": "persona", "label": "Contact research", "value": _trim(value),
                "date": day}

    person = persona.get("person") or {}
    confidence = (persona.get("section8") or {}).get("overall")
    facts = []
    role = person.get("roleTitle")
    if role:
        facts.append(fact("role", f"{contact_name}: {role}" + (f" (research confidence: {confidence})" if confidence else "")))
    section1 = persona.get("section1") or {}
    observed = [b.get("text") for b in (section1.get("factsLeft") or []) + (section1.get("factsRight") or [])
                if isinstance(b, dict) and b.get("label") == "observed" and b.get("text")]
    facts += [fact(f"fact_{i}", text) for i, text in enumerate(observed[:MAX_OBSERVED], 1)]
    hooks = [h for h in ((persona.get("section4") or {}).get("hooks") or []) if isinstance(h, dict) and h.get("text")]
    if hooks and observed:  # a hook is only as good as the observed material behind it
        h = hooks[0]
        facts.append(fact("hook", f"Conversation hook: {h.get('title') + ': ' if h.get('title') else ''}{h['text']}"))
    return facts


def record(session: Session, contact: Contact, computed_at: datetime, *, persona: dict | None = None,
           status: str = "ok", error: str | None = None) -> ContactPersona:
    """Append one research result (or failure) to contact_persona."""
    row = ContactPersona(
        contact_key=contact.key, contact_name=contact.name, company_name=contact.company, computed_at=computed_at,
        status=status, persona=persona or {},
        facts=facts_from(persona, contact.name, computed_at) if persona else [], error=error,
    )
    session.add(row)
    return row

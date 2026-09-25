"""Name matching: does this meeting or mail belong to this deal?

Built on the ICP bot's entity-resolution primitives (`api/icp/entity_resolver.py`, copied from the
ICP bot): company names are normalised by stripping legal suffixes and corporate noise words, and
an email domain whose label contains the company name confirms it (their example: sobha.com
confirms "Shobha Limited" is Sobha). Two additions the ICP bot lacks: free-mail and Practus's own
domains never count as a company's domain, and a name must appear as a whole word or phrase.

A deal's identity is its company names (the Zoho account, and the company part of the deal name)
plus the email domains of the people on its outreach log.
"""
from __future__ import annotations

from dataclasses import dataclass

from api.icp.entity_resolver import normalize_for_matching
from api.sources.readai import email_domain
from api.sources.zoho import Reachout, ZohoDeal

# Never a company's own domain.
FREE_MAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "yahoo.in", "outlook.com", "hotmail.com",
    "live.com", "msn.com", "icloud.com", "me.com", "aol.com", "rediffmail.com", "protonmail.com",
    "proton.me", "zoho.com", "zohomail.in", "gmx.com", "mail.com", "yandex.com",
}
PRACTUS_DOMAINS = {"roibypractus.com", "practus.com", "practus.in"}
MIN_NAME_LEN = 4  # shorter normalised names ("abc") match too much prose to be evidence


@dataclass(frozen=True)
class DealIdentity:
    names: tuple[str, ...]  # normalised company names
    domains: frozenset[str]


def company_domain(email: str | None) -> str | None:
    domain = email_domain(email)
    if not domain or domain in FREE_MAIL_DOMAINS or domain in PRACTUS_DOMAINS:
        return None
    return domain


def deal_identity(deal: ZohoDeal, reachouts: list[Reachout] = ()) -> DealIdentity:
    raw_names = [deal.account_name, deal.name.split(" - ")[0] if deal.name else None]
    names = []
    for raw in raw_names:
        norm = normalize_for_matching(raw or "")
        if len(norm) >= MIN_NAME_LEN and norm not in names:
            names.append(norm)
    domains = frozenset(d for d in (company_domain(r.email) for r in reachouts) if d)
    return DealIdentity(tuple(names), domains)


def mentions_company(identity: DealIdentity, text: str | None) -> bool:
    """True when a normalised company name appears in `text` as a whole word or phrase."""
    if not text or not identity.names:
        return False
    haystack = f" {normalize_for_matching(text)} "
    return any(f" {name} " in haystack for name in identity.names)


def domain_matches(identity: DealIdentity, domains) -> bool:
    return bool(identity.domains & {d.lower() for d in domains if d})


def meeting_belongs(identity: DealIdentity, meeting) -> bool:
    return domain_matches(identity, meeting.participant_domains) or mentions_company(identity, meeting.title)


def mail_belongs(identity: DealIdentity, mail) -> bool:
    return domain_matches(identity, mail.domains) or mentions_company(identity, f"{mail.subject} {mail.preview}")

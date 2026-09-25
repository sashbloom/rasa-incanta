# Copied from icp-bot/backend/domains/mahak/people/mahak/icp/conflict_checker.py (read-only reference repo).
# Only imports and plumbing are adapted for Rasa Incanta; the logic is the ICP bot's.
"""Gate 4 (icp-skill.md GATES: "Active Practus pursuit at a direct
competitor" — effect "Flag; force explicit go/no-go"). Same hybrid split
as A1/A2/A3/etc: a web-search call names the target's direct competitors —
a judgment call needing outside knowledge, not something Zoho can answer —
then a plain, deterministic Zoho lookup (reusing `entity_resolver.py`/
`zoho_db.py`, already built) checks each named competitor for a currently-
open deal. Firing the gate is a fact looked up in Zoho, never a model's own
say-so about whether a conflict exists.

icp-skill.md scenario #45 is explicit: a competitor that's a *delivered*
client, not an active pursuit, does **not** fire this gate — it's
surfaced as a credential and talking point instead. Only a deal whose
stage isn't Client Won/Client Lost counts as "active" here.

`identify_competitors()` uses Exa's `outputSchema` synthesis (see
exa_search.py) rather than Anthropic's own agentic web_search tool — live-
confirmed this drops the call from ~32s to ~5s for the same real result,
one of the four calls identified as the dominant cost of a run (see the
plan's "run time" investigation).
"""

from __future__ import annotations

import logging

from pydantic import BaseModel

from . import entity_resolver, exa_search, zoho_db
from .models import CLOSED_STAGES, Stage

logger = logging.getLogger(__name__)

COMPETITOR_SCHEMA = {
    "type": "object",
    "required": ["competitors"],
    "properties": {
        "competitors": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Up to 5 named direct competitors, most relevant first — companies competing for the same customers in the same core business. Empty array if none can be identified.",
        },
    },
}


class ConflictResult(BaseModel):
    active_competitor_pursuit: bool = False
    competitor_name: str | None = None
    detail: str = ""


def identify_competitors(company_name: str, industry_hint: str | None = None) -> list[str]:
    industry_line = f" (industry: {industry_hint})" if industry_hint else ""
    query = (
        f'Direct competitors of "{company_name}"{industry_line} — companies that compete for the same '
        f"customers in the same core business."
    )
    content, _grounding = exa_search.search_and_synthesize(
        query,
        output_schema=COMPETITOR_SCHEMA,
        system_prompt="Return up to 5 real, named company competitors, most relevant first. Company names only, no explanation. Empty array if none can be identified.",
    )
    competitors = content.get("competitors", [])
    logger.info("identify_competitors(%r, industry_hint=%r) -> %s", company_name, industry_hint, competitors)
    return competitors


def _confirmed_account_ids(competitor_name: str) -> list[int]:
    """Only acts on exact/near-exact matches — reuses entity_resolver.py's
    own confirmation bar rather than pooling every fuzzy hit under this
    name, for exactly the reason entity_resolver.py itself was rewritten:
    a merely similar-sounding company (e.g. "Trident" for "Trent") must
    never be treated as the same account."""
    query = entity_resolver.normalize_for_matching(competitor_name)
    ids = []
    for account in entity_resolver.find_matching_accounts(competitor_name):
        candidate = entity_resolver.normalize_for_matching(account.get("account_name") or "")
        if entity_resolver.similarity(query, candidate) >= entity_resolver.NAME_EXACT_THRESHOLD:
            ids.append(account["id"])
    return ids


def _active_deal_name(account_ids: list[int]) -> str | None:
    for deal in zoho_db.find_deals_by_account_ids(account_ids) if account_ids else []:
        try:
            stage_enum = Stage(deal.get("stage")) if deal.get("stage") else None
        except ValueError:
            stage_enum = None
        if stage_enum is not None and stage_enum not in CLOSED_STAGES:
            return deal.get("deal_name") or "(unnamed deal)"
    return None


def check(company_name: str, *, industry_hint: str | None = None, competitors: list[str] | None = None) -> ConflictResult:
    """`competitors`, when given, skips calling `identify_competitors()`
    here entirely — lets a caller that already kicked it off as an early
    background future (see evidence_assembler.py's `on_industry_hint`
    param / orchestrator.py's Gate 4 call site) hand in the already-
    resolved result instead of paying for a second web-search call. `None`
    (the default) preserves the original fetch-it-here behavior for every
    existing caller/test."""
    if competitors is None:
        competitors = identify_competitors(company_name, industry_hint)
    for competitor in competitors:
        account_ids = _confirmed_account_ids(competitor)
        if not account_ids:
            continue
        deal_name = _active_deal_name(account_ids)
        if deal_name:
            return ConflictResult(
                active_competitor_pursuit=True,
                competitor_name=competitor,
                detail=f"An active Practus pursuit ({deal_name!r}) exists at {competitor!r}, a named direct competitor.",
            )
    return ConflictResult(active_competitor_pursuit=False)

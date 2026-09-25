# Copied from icp-bot/backend/domains/mahak/people/mahak/icp/practus_history.py (read-only reference repo).
# Only imports and plumbing are adapted for Rasa Incanta; the logic is the ICP bot's.
"""Step 1 — Practus-history classification (icp-skill.md §1).

Combines two sources of "have we worked with this company before":
- Zoho `deals.stage = 'Client Won'` for the resolved account(s)
- `Client Names 2020-2025.xlsx` -> `Clients` sheet (a separately maintained
  list that predates full CRM coverage — 269 rows, columns Client Name /
  Industry / Ownership type / Service Line, confirmed directly against the
  real file)

Client status is decided entirely from these two sources — never from a
Setu chat-endpoint cross-check (removed; Setu is now DB-only, see
`setu_db.py`).
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import openpyxl

from api.icp.compat import get_settings
from . import zoho_db
from .models import PractusHistory

logger = logging.getLogger(__name__)

_LEGAL_SUFFIXES = re.compile(
    r"\b(private limited|pvt\.?\s*ltd\.?|limited|ltd\.?|inc\.?|llp|llc|corp\.?|corporation|co\.?)\b\.?",
    re.IGNORECASE,
)

# Confirmed clients sometimes appear under multiple Zoho/Excel account-name
# variants that legal-suffix stripping alone doesn't collapse -- e.g.
# "Sanctum Wealth Mgt" / "Sanctum Wealth Private Limited" / "Sanctum Wealth
# Private Limited Recon" all counted as 3 separate confirmed clients in a
# real Manappuram Finance run (2026-09-09) instead of 1. "Recon" is Zoho's
# own naming convention for a reconciliation/duplicate account, and "Mgt" is
# just an abbreviation of the same admin/business-type word every other
# variant already has stripped as a legal suffix -- neither carries
# distinguishing information about WHICH client this is, so strip them the
# same way legal suffixes are stripped, letting the existing normalize-then-
# dedupe-by-key logic at each call site collapse them with no restructuring.
_NOISE_TOKENS = re.compile(r"\b(recon(?:ciliation)?|mgt)\b\.?", re.IGNORECASE)

# Gate 3 / scenario 17 (icp-skill.md line 473): "losses older than 24 months"
# discount rather than count toward the 2+ threshold.
GATE_3_LOOKBACK_DAYS = 730


def _within_lookback(loss_date: date | datetime | None, now: datetime) -> bool:
    """`date_client_lost` is a Postgres DATE column, so zoho_db hands back a
    native `date`/`datetime`, not a string (unlike CrmStructured's fields,
    which go through `_stringify()`). A missing date fails safe toward
    counting the loss -- we have no basis to discount it as "old"."""
    if loss_date is None:
        return True
    if isinstance(loss_date, datetime):
        loss_date = loss_date.date()
    return (now.date() - loss_date).days <= GATE_3_LOOKBACK_DAYS


def normalize_name(name: str) -> str:
    """Strip whitespace/legal suffixes/punctuation for cross-source name
    matching — the xlsx and Zoho use inconsistent casing and legal suffixes
    ('Pvt Ltd' vs 'Private Limited' vs bare, stray leading newlines/spaces
    in the xlsx)."""
    text = name.strip().lower()
    text = _LEGAL_SUFFIXES.sub("", text)
    text = _NOISE_TOKENS.sub("", text)
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    return text


def _looks_like_a_prospect_not_a_confirmed_client(name: str) -> bool:
    """A genuinely confirmed (`stage='Client Won'`) account should never
    carry "prospect" in its own name — confirmed live (Manappuram Finance,
    2026-09-09) that "Blue Foods CFO Recruitment - Prospect" is a real
    account whose `stage` IS 'Client Won' in the DB (so the existing
    stage filter can't catch it), but whose account_name is a leftover
    artifact from when the deal was still a live prospect and was never
    renamed on conversion. The name itself is the only signal available to
    catch this — a real Won client's name never says "Prospect"."""
    return "prospect" in name.lower()


@lru_cache
def _load_client_names_index() -> dict[str, dict]:
    """Keyed by `normalize_name(...)` for O(1) lookup per name variant.
    Missing/unreadable file -> empty index (Zoho alone still works), not a
    hard failure — the xlsx is one of two sources, not the only one."""
    path_str = get_settings().icp_client_names_file
    path = Path(path_str)
    if not path.exists():
        return {}

    workbook = openpyxl.load_workbook(path, data_only=True, read_only=True)
    sheet = workbook["Clients"]
    index: dict[str, dict] = {}
    for row in sheet.iter_rows(min_row=2, values_only=True):
        name = row[0]
        if not name or not str(name).strip():
            continue
        raw_name = str(name).strip()
        index[normalize_name(raw_name)] = {
            "client_name": raw_name,
            "industry": row[1],
            "ownership_type": row[2],
            "service_line": row[3],
        }
    return index


# icp-skill.md P1 counts confirmed clients "in the industry" -- confirmed
# live against the real CRM that `industry_type = 'Others'` is a generic
# catch-all 638 completely unrelated won deals share (a car-detailing shop,
# a mental-health nonprofit, an industrial-parts distributor, ...), not a
# real industry. Treating it as a genuine match inflated P1 to a false
# top-band score for any deal bucketed this way -- which the same live run
# showed is common (`industry_hint='Others'` also fed Gate 4's conflict
# search). Guard against known generic/non-informative bucket values here
# rather than trusting whatever string the BD-entered field happens to hold.
_GENERIC_INDUSTRY_VALUES = {"others", "other", "n/a", "na", "unknown", "general", ""}


def is_generic_industry_value(industry: str | None) -> bool:
    """Public wrapper so other modules (evidence_assembler.py's P3 Setu
    question, in particular) can apply the same guard against the CRM's
    generic `industry_type` catch-all, not just P1's own scoring path."""
    return not industry or industry.strip().lower() in _GENERIC_INDUSTRY_VALUES

# Gate 3 input (icp-skill.md line 430: "2+ Client Lost with no captured
# reason") -- confirmed live against the real CRM that `reason_for_loss`
# defaults to the picklist value 'Others' on essentially every Client Lost
# deal, Sula's three included. 'Others' is not a captured reason, it's
# Zoho's placeholder for "no specific reason selected" -- the real reason,
# when one exists, lives in the free-text `specify_reason_for_lost` field.
# Same bug class as _GENERIC_INDUSTRY_VALUES above (a BD-entered picklist
# default masquerading as real data); confirmed live that treating 'Others'
# as "reason captured" made Gate 3 structurally unable to fire CRM-wide.
_GENERIC_REASON_VALUES = {"others", "other", ""}


def _reason_captured(deal: dict) -> bool:
    specify = (deal.get("specify_reason_for_lost") or "").strip()
    if specify:
        return True
    reason = (deal.get("reason_for_loss") or "").strip().lower()
    return bool(reason) and reason not in _GENERIC_REASON_VALUES


def confirmed_clients_in_industry(industry: str | None) -> list[str]:
    """P1 (icp-skill.md STEP 3): "Counts ONLY confirmed Practus clients —
    Zoho Client Won + the Client Names file", scoped to the target's
    industry and counted across the WHOLE client base (not just this one
    account) -- previously nothing computed this at all, so P1 always
    scored off zero real evidence. Same two-source union as classify()'s
    own is_practus_client determination, de-duplicated by normalized name
    in case a client appears in both sources. Returns [] for a generic/
    non-informative industry value (see _GENERIC_INDUSTRY_VALUES) rather
    than pooling an unrelated catch-all bucket as if it were a real match."""
    if is_generic_industry_value(industry):
        return []
    seen: set[str] = set()
    names: list[str] = []

    for deal in zoho_db.find_won_deals_by_industry(industry):
        name = deal.get("account_name")
        if not name or _looks_like_a_prospect_not_a_confirmed_client(name):
            continue
        key = normalize_name(name)
        if key and key not in seen:
            seen.add(key)
            names.append(name)

    normalized_industry = industry.strip().lower()
    for entry in _load_client_names_index().values():
        entry_industry = (entry.get("industry") or "").strip().lower()
        name = entry["client_name"]
        if _looks_like_a_prospect_not_a_confirmed_client(name):
            continue
        key = normalize_name(name)
        if entry_industry == normalized_industry and key not in seen:
            seen.add(key)
            names.append(name)

    return names


def confirmed_client_industry_breakdown() -> dict[str, int]:
    """Confirmed Practus clients (Zoho Client Won + Client Names file),
    de-duplicated by normalized name, counted per industry — NOT scoped to
    one target industry, unlike confirmed_clients_in_industry() above. Lets
    the narrative honestly describe adjacent-industry breadth even when the
    exact target industry has zero confirmed clients: icp-skill.md's own
    P1 zero-case instruction is to write "no confirmed credential found in
    this industry", but the real reference report for a wine-industry
    prospect went further with real, grounded context — "Twenty-four
    clients across FMCG, retail, consumer goods and hospitality. None in
    alcoholic beverages." — which requires the real cross-industry
    breakdown, not just a single zero-count.

    Excludes the generic/non-informative bucket values (see
    _GENERIC_INDUSTRY_VALUES) as their own category — "Others: 638" isn't
    meaningful breadth, it's a BD-entered catch-all, the same reasoning
    confirmed_clients_in_industry() already applies to the target industry."""
    seen: set[str] = set()
    counts: dict[str, int] = {}

    for deal in zoho_db.find_all_won_deals():
        name = deal.get("account_name")
        industry = (deal.get("industry_type") or "").strip()
        if not name or _looks_like_a_prospect_not_a_confirmed_client(name):
            continue
        key = normalize_name(name)
        if not key or key in seen or is_generic_industry_value(industry):
            continue
        seen.add(key)
        counts[industry] = counts.get(industry, 0) + 1

    for entry in _load_client_names_index().values():
        name = entry["client_name"]
        industry = (entry.get("industry") or "").strip()
        if _looks_like_a_prospect_not_a_confirmed_client(name):
            continue
        key = normalize_name(name)
        if key in seen or is_generic_industry_value(industry):
            continue
        seen.add(key)
        counts[industry] = counts.get(industry, 0) + 1

    return counts


_SECTOR_RELEVANCE_SCHEMA = {
    "type": "object",
    "properties": {
        "irrelevant_names": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Names from the candidate list that, based on the name itself and general "
                "knowledge of well-known firms, are clearly NOT genuine members of the stated "
                "industry (e.g. a law firm, accounting firm, hospital, spa, or nonprofit "
                "foundation counted under a financial-services/other industry tag purely because "
                "of a CRM data-entry error on that one deal). Copy each name VERBATIM from the "
                "candidate list. Return an empty array if every name plausibly belongs — do not "
                "drop a name just because it is unfamiliar or a diversified fund/PE/VC name that "
                "doesn't sound like a traditional player in the industry."
            ),
        },
    },
    "required": ["irrelevant_names"],
    "additionalProperties": False,
}


# Deterministic, near-zero-risk backstop for the clearest institutional-
# type mismatches -- confirmed live (Manappuram Finance, 2026-09-10) that
# the LLM sector-relevance call below can miss even the most obvious cases
# on a given run (non-determinism): "Michael & Susan Dell Foundation" and
# "FRR Immigration" both survived a real filter call that should have
# caught them. These specific words are about as close to a 100%-reliable
# "not a genuine industry client, regardless of target industry" signal as
# exists -- almost no real company legitimately carries them in its own
# name. Deliberately a much smaller, safer list than a general blocklist
# would need: "Trust" and "Society" are excluded on purpose since both have
# real, legitimate BFSI meanings (unit trusts, REITs, cooperative credit
# societies) that a keyword match would wrongly exclude.
_INSTITUTIONAL_TYPE_RE = re.compile(r"\b(foundation|charitable trust|ngo|non-?profit|immigration)\b", re.IGNORECASE)


def _looks_like_a_nonfinancial_institution_by_name(name: str) -> bool:
    return bool(_INSTITUTIONAL_TYPE_RE.search(name))


def filter_sector_irrelevant_names(industry: str, names: list[str]) -> list[str]:
    """icp-skill.md P1 counts confirmed clients "in the industry" — but
    `industry_type` is a BD-entered field per deal, not verified, and a live
    Manappuram Finance run (2026-09-09) confirmed real, wrong tags feeding
    straight into the confirmed-client count: a law firm (Kobre & Kim),
    accounting firms (Withum, KC Patel & Co), a hospital-focused PE deal
    (TPG -Hosptial), and outright nonprofits (Michael & Susan Dell
    Foundation, Samhita Development Network) all counted as confirmed BFSI
    clients purely because their own deal's industry_type field said so.

    This is the same class of problem `_GENERIC_INDUSTRY_VALUES` already
    guards against for the universal 'Others' catch-all — just for
    individually-wrong values on specific deals instead of one
    universally-generic one, so it needs actual judgment (does this NAME
    plausibly belong to this industry), not another string blocklist. Kept
    as its own standalone function — deliberately NOT called from inside
    confirmed_clients_in_industry()/confirmed_client_industry_breakdown()
    above, which stay pure/LLM-free by design (independently unit-testable,
    no mocking required) — callers that can tolerate an LLM call (i.e.
    evidence_assembler.py, which already makes several) apply this as an
    explicit extra step on the result.

    Degrades to the FULL, unfiltered list (never an empty one) on any
    failure — a missed false-positive here is a much narrower harm than
    losing real, correct client evidence entirely, same fail-open
    philosophy as confirmed_clients_by_keyword_context() above."""
    if not names:
        return names

    # Skip the deterministic keyword backstop only when the TARGET industry
    # is itself nonprofit-related -- otherwise a genuine nonprofit-sector
    # client would be wrongly excluded by its own industry's defining word.
    irrelevant_by_keyword: set[str] = set()
    if not _looks_like_a_nonfinancial_institution_by_name(industry):
        irrelevant_by_keyword = {
            normalize_name(n) for n in names if _looks_like_a_nonfinancial_institution_by_name(n)
        }

    from .anthropic_client import generate_structured_narrative

    prompt = f"""Below is a list of client names Practus's CRM has tagged as confirmed clients in the \
"{industry}" industry. CRM industry tags are entered by a salesperson per deal and occasionally wrong \
for one specific client — decide which names, if any, are clearly not genuine members of this industry \
based on the name itself and general knowledge of well-known firms.

CANDIDATE NAMES:
{chr(10).join(f"- {name}" for name in names)}

Only flag a name if it is CLEARLY unrelated to "{industry}" (e.g. a law firm, accounting firm, hospital, \
spa, or nonprofit foundation) — an unfamiliar name, or a diversified fund/PE/VC/holding company name that \
doesn't sound like a traditional player in the industry, is NOT grounds to flag it. When genuinely unsure, \
do not flag it — an empty list is the correct, expected answer when every name plausibly belongs."""

    try:
        # Raised 1500->6000. Langfuse (2026-09-10 Manappuram run) shows this
        # call emitting EXACTLY 1500 tokens, truncating, retrying at 2250 --
        # and hitting 2250 EXACTLY too, so the retry was lost as well and
        # both attempts were billed for nothing. This filters over the whole
        # confirmed-client list (hundreds of names on a real run) with
        # adaptive thinking on top, so the old ceiling was never realistic.
        data = generate_structured_narrative(prompt, _SECTOR_RELEVANCE_SCHEMA, max_tokens=6000, label="filter_sector_irrelevant_names", include_skill_reference=False)
        irrelevant_by_llm = {normalize_name(n) for n in data.get("irrelevant_names", []) if n}
    except Exception as exc:
        logger.warning("P1 sector-relevance filter FAILED for industry %r: %s — keeping the full list "
                        "(minus any deterministic keyword matches).", industry, exc)
        irrelevant_by_llm = set()

    irrelevant = irrelevant_by_keyword | irrelevant_by_llm
    if not irrelevant:
        return names
    filtered = [n for n in names if normalize_name(n) not in irrelevant]
    dropped = [n for n in names if normalize_name(n) in irrelevant]
    if dropped:
        logger.info("P1 sector-relevance filter dropped %d name(s) from %r: %s", len(dropped), industry, dropped)
    return filtered


_DEDUP_SCHEMA = {
    "type": "object",
    "properties": {
        "duplicate_groups": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "canonical_name": {
                        "type": "string",
                        "description": "The fullest/most complete name to KEEP, copied verbatim from the candidate list.",
                    },
                    "duplicate_names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Other names from the list that refer to the SAME real company as "
                            "canonical_name, copied verbatim. Never include canonical_name itself here."
                        ),
                    },
                },
                "required": ["canonical_name", "duplicate_names"],
                "additionalProperties": False,
            },
            "description": (
                "Groups of names from the candidate list that refer to the SAME real company under a "
                "different spelling/abbreviation/legal-entity-suffix variant. Only group names you are "
                "genuinely confident refer to the same company. Return an empty array if no genuine "
                "duplicates exist -- do not force a grouping."
            ),
        },
    },
    "required": ["duplicate_groups"],
    "additionalProperties": False,
}


def deduplicate_client_names(names: list[str]) -> list[str]:
    """icp-skill.md P1 counts confirmed clients "in the industry" -- a live
    Sobha run (2026-09-09) confirmed real name-variant duplicates inflating
    the count that normalize_name()'s exact-key dedup can't catch: "Vianaar
    Homes Private Limited" / "Vianaar" (one is a short form of the other,
    not just a legal-suffix difference) and "Mani Group" / "Mani Realty
    Projects Private Limited" (share only a root word, genuinely ambiguous
    whether they're the same company without real-world knowledge). A
    Manappuram Finance run separately confirmed "Sanctum Wealth Mgt" /
    "...Private Limited" / "...Private Limited Recon" — that specific case
    is already caught by normalize_name()'s noise-token stripping, but the
    Sobha cases prove that's not a general solution: pure string heuristics
    have no way to know "Vianaar" is a genuine abbreviation while "Mani
    Group"/"Mani Realty Projects" might be two different real entities
    under one family/conglomerate. This is the same class of judgment call
    `filter_sector_irrelevant_names()` already delegates to an LLM rather
    than guess with more string rules.

    Kept as its own standalone function for the same reason as
    filter_sector_irrelevant_names() — confirmed_clients_in_industry()/
    confirmed_client_industry_breakdown() stay pure/LLM-free by design;
    callers that can tolerate an LLM call apply this as an explicit extra
    step. Degrades to the FULL, undeduplicated list on any failure or
    hallucinated name — a missed duplicate is a narrower harm than losing a
    real, correct client name."""
    if len(names) < 2:
        return names

    from .anthropic_client import generate_structured_narrative

    prompt = f"""Below is a list of client names from a CRM/client-names file. The same real company \
sometimes appears more than once under a different spelling, abbreviation, or legal-entity-suffix \
variant (e.g. a short form vs. the full registered name, or with/without "Private Limited"). Identify \
any such genuine duplicates.

CANDIDATE NAMES:
{chr(10).join(f"- {name}" for name in names)}

Only group two names together if you are genuinely confident they refer to the SAME real company — \
sharing only one common word (e.g. a family/conglomerate name prefix) is NOT enough on its own unless \
the rest of the evidence (one being an obvious short form of the other) makes you confident. When \
genuinely unsure, do NOT group them — an empty list is the correct, expected answer when every name is \
a genuinely distinct company."""

    try:
        # Raised 2000->8000: same live evidence as the sector filter above --
        # exactly 2000 output tokens recorded, i.e. truncated, then a retry.
        # Dedup reasons over every confirmed client name at once, so its
        # output scales with the client base, not with this one prospect.
        data = generate_structured_narrative(prompt, _DEDUP_SCHEMA, max_tokens=8000, label="deduplicate_client_names", include_skill_reference=False)
    except Exception as exc:
        logger.warning("P1 client-name dedup LLM call FAILED: %s — keeping the full, undeduplicated list.", exc)
        return names

    to_drop: set[str] = set()
    for group in data.get("duplicate_groups", []):
        canonical = group.get("canonical_name")
        if not canonical or canonical not in names:
            continue
        for dup in group.get("duplicate_names", []):
            if dup in names and dup != canonical:
                to_drop.add(dup)

    if not to_drop:
        return names
    deduped = [n for n in names if n not in to_drop]
    logger.info("P1 client-name dedup dropped %d name-variant duplicate(s): %s", len(to_drop), sorted(to_drop))
    return deduped


# Superseded a pure keyword/inverse-document-frequency matcher (see git
# history) after it repeatedly produced confident, wrong matches in
# production: "Real Estate" for a wine/beverage company, "Consumer goods
# industry" for an auto-components company -- both because that label's own
# words happened to be ordinary English ("real", "estate", "good",
# "consumer") that show up incidentally in any long business narrative, and
# a bare word-overlap score has no way to tell "genuine sector signal" from
# "coincidental word". Each fix required hand-adding the offending word to a
# blocklist after the fact, for a specific company, with no way to know
# which other common word would trip the same failure for the next company.
# An LLM classification call replaces that arithmetic with actual reading
# comprehension: given the same research text and the same small (~40-value)
# candidate list, decide whether the text genuinely describes the company
# operating in one of those real sectors, the same semantic-judgment task
# this project already delegates to an LLM for P2/P3 (deterministic
# candidate list, LLM makes the final call) rather than trusting a keyword
# score alone.
_INDUSTRY_MATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "matched_industry": {
            "type": ["string", "null"],
            "description": (
                "Copied VERBATIM from the candidate list — never invent, paraphrase, or abbreviate a "
                "label. Null if none of the candidates genuinely describe this company's real sector."
            ),
        },
        "rationale": {
            "type": "string",
            "description": "One sentence: why this is (or is not) a genuine sector match, based only on the text given.",
        },
    },
    "required": ["matched_industry", "rationale"],
    "additionalProperties": False,
}


def confirmed_clients_by_keyword_context(keyword_context: str) -> tuple[str | None, list[str]]:
    """Live-confirmed real gap: for a company whose own CRM `industry_type`
    is generic/unreliable (e.g. Sula Wines' is 'Others'),
    confirmed_clients_in_industry() can never find a real match, even when
    one genuinely exists -- confirmed live that "Theobroma Foods Private
    Limited" is a real confirmed client tagged 'FMCG' in the Client Names
    file, which a wine/beverage company's own secondary research reliably
    surfaces as its real sector (FMCG is the standard umbrella term for
    that category in Indian market reporting) -- but the exact-match path
    has no way to find it since it only ever compares against the CRM's
    own (broken) field.

    Hands `keyword_context` (typically secondary-research text, which —
    unlike the CRM field — actually describes the company's real business)
    and the real, confirmed-client industry label list to one small, bounded
    LLM call whose only job is to decide whether the text genuinely
    describes the company operating in one of those sectors — see
    `_INDUSTRY_MATCH_SCHEMA`'s module comment for why this replaced a
    keyword/IDF matcher that kept producing confident, wrong matches.
    Degrades to (None, []) — an honest data gap, never a guess — on ANY
    failure (the LLM call erroring, or genuinely returning no match), same
    philosophy as every other connector call in this pipeline. Returns
    (matched_industry_label, client_names) — the label is surfaced
    downstream so the narrative can honestly say this was a real-sector
    proxy match, not the CRM's own (generic) industry field."""
    keyword_context = (keyword_context or "").strip()
    if not keyword_context:
        return None, []

    breakdown = confirmed_client_industry_breakdown()
    if not breakdown:
        return None, []

    from .anthropic_client import generate_structured_narrative

    labels = sorted(breakdown)
    prompt = f"""A company's own CRM industry field is missing or too generic to use for a pre-sales \
qualification brief. Below is unstructured research text about the company, and the real industry \
labels Practus has confirmed clients in. Decide whether the company's REAL sector genuinely matches \
exactly one of these labels, based only on what the text actually says.

RESEARCH TEXT (this covers many topics — financials, competitors, triggers, ownership, etc. — most of \
it is NOT about industry classification; use only the parts that actually describe what the company \
does or sells):
{keyword_context[:6000]}

CANDIDATE INDUSTRY LABELS (copy your answer verbatim from this list, or return null if none genuinely fit):
{chr(10).join(f"- {label}" for label in labels)}

Pick at most one label. A coincidental word overlap is not a match — only pick a label if the text \
actually describes the company operating in that real sector. Returning null is the correct, expected \
answer when no label genuinely fits; never force a match to avoid returning null."""

    try:
        # Raised 1500->4000 preemptively: same shape of call as the two
        # above (reasons over the whole industry label set), both of which
        # were confirmed truncating live at these budgets. A ceiling that is
        # never reached costs nothing -- only a ceiling that IS reached does.
        data = generate_structured_narrative(prompt, _INDUSTRY_MATCH_SCHEMA, max_tokens=4000, label="keyword_industry_match", include_skill_reference=False)
    except Exception as exc:
        logger.warning("P1 industry proxy-match LLM call FAILED: %s — treated as no match.", exc)
        return None, []

    matched = data.get("matched_industry")
    if not matched or matched not in breakdown:
        if matched:
            logger.warning("P1 industry proxy-match LLM returned an unlisted industry %r — ignored.", matched)
        return None, []

    logger.info("P1 industry proxy-match (LLM): %r — %s", matched, data.get("rationale", ""))
    return matched, confirmed_clients_in_industry(matched)


def classify(
    company_name_variants: list[str], account_ids: list[int], *, now: datetime | None = None, industry: str | None = None
) -> PractusHistory:
    """`company_name_variants` should include every spelling
    entity_resolver.py found (the queried name plus any duplicate-account
    names) — the xlsx match is name-based, so every known spelling needs
    checking, not just the one the caller typed.

    `now` anchors the Gate 3 24-month lookback (scenario 17); defaults to
    the current UTC time for callers (tests, ad-hoc scripts) that don't
    thread one through."""
    now = now or datetime.now(timezone.utc)
    won_deals = zoho_db.find_won_deals_by_account_ids(account_ids)
    lost_deals = zoho_db.find_lost_deals_by_account_ids(account_ids)

    index = _load_client_names_index()
    xlsx_hit = next(
        (index[normalize_name(v)] for v in company_name_variants if normalize_name(v) in index),
        None,
    )

    in_zoho = bool(won_deals)
    in_xlsx = xlsx_hit is not None

    if in_zoho and in_xlsx:
        source = "both"
    elif in_zoho:
        source = "zoho_client_won"
    elif in_xlsx:
        source = "client_names_file"
    else:
        source = None

    # Gate 3 input, counted per-loss (icp-skill.md line 430): a loss
    # contributes only if it lacks a captured reason AND falls within the
    # 24-month lookback -- NOT "zero reasons captured across every loss ever".
    unexplained_count = sum(
        1
        for d in lost_deals
        if not _reason_captured(d)
        and _within_lookback(d.get("date_client_lost"), now)
    )

    industry_confirmed_client_names = confirmed_clients_in_industry(industry)
    # Only worth the extra whole-CRM query when the exact-industry list came
    # back empty -- that's the only case the narrative needs broader
    # adjacent-industry context for (see confirmed_client_industry_breakdown()'s
    # docstring); a real exact match already gives it everything it needs.
    all_confirmed_clients_by_industry = {} if industry_confirmed_client_names else confirmed_client_industry_breakdown()

    return PractusHistory(
        is_practus_client=in_zoho or in_xlsx,
        source=source,
        client_lost_count=len(lost_deals),
        client_lost_unexplained_count=unexplained_count,
        industry_confirmed_client_names=industry_confirmed_client_names,
        all_confirmed_clients_by_industry=all_confirmed_clients_by_industry,
    )



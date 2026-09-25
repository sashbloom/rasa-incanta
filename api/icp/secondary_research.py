# Copied from icp-bot/backend/domains/mahak/people/mahak/icp/secondary_research.py (read-only reference repo).
# Only imports and plumbing are adapted for Rasa Incanta; the logic is the ICP bot's.
"""Secondary research — financials, ownership, triggers, peer margins.

icp-skill.md is explicit that these can NEVER come from Zoho CRM fields
(EVIDENCE DISCIPLINE, line 516) and names its own source set per geography
(SECONDARY RESEARCH, lines 550-557), with a conflict-resolution hierarchy:
internal beats public web, audited filings beat databases, named dated
sources beat undated ones.

This module runs one Anthropic call per company with server-side web search
enabled, seeded with the skill's own named sources as concrete starting
URLs (not just names) so the model has somewhere real to start rather than
inventing a search strategy from scratch. It's still free to search beyond
these if a company isn't well covered by them, and must say so if it does.
"""

from __future__ import annotations

import logging

from . import exa_search
from .anthropic_client import generate_structured_narrative
from .criteria_tables import A2_LIQUIDITY_ROUTING
from .models import OwnershipControl

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Base links per geography/segment, straight from icp-skill.md's own
# "Secondary research" section. These are starting anchors, not an
# exhaustive or guaranteed-current list — a 404 or a site redesign doesn't
# mean the source is wrong, just that the model should search from there
# rather than treat the link as load-bearing.
# ---------------------------------------------------------------------------

SOURCE_LINKS: dict[str, dict[str, str]] = {
    "india_listed": {
        "Screener.in (financials, ratios, peer comparison)": "https://www.screener.in/",
        "BSE India (filings, announcements)": "https://www.bseindia.com/",
        "NSE India (filings, announcements)": "https://www.nseindia.com/",
    },
    "india_unlisted": {
        "MCA company master data / filings": "https://www.mca.gov.in/content/mca/global/en/mca/master-data/MDS.html",
        "Tofler (corporate intelligence)": "https://www.tofler.in/",
        "Zauba Corp (company filings)": "https://www.zaubacorp.com/",
        "CRISIL Ratings": "https://www.crisilratings.com/",
        "ICRA": "https://www.icra.in/",
        "CareEdge Ratings (CARE)": "https://www.careratings.com/",
        "Infomerics Ratings": "https://www.infomerics.com/",
    },
    "india_funded": {
        "Tracxn": "https://tracxn.com/",
        "VCCircle": "https://www.vccircle.com/",
        "Entrackr": "https://entrackr.com/",
    },
    "us": {
        "SEC EDGAR full-text search": "https://www.sec.gov/edgar/search/",
        "SEC EDGAR company filings": "https://www.sec.gov/cgi-bin/browse-edgar",
        "PitchBook": "https://pitchbook.com/",
        "Crunchbase": "https://www.crunchbase.com/",
        "Moody's Ratings": "https://www.moodys.com/",
        "S&P Global Ratings": "https://www.spglobal.com/ratings/en/",
        "Fitch Ratings": "https://www.fitchratings.com/",
    },
    "mea": {
        "Saudi Exchange (Tadawul)": "https://www.saudiexchange.sa/",
        "Abu Dhabi Securities Exchange (ADX)": "https://www.adx.ae/",
        "Dubai Financial Market (DFM)": "https://www.dfm.ae/",
        "Qatar Stock Exchange (QSE)": "https://www.qe.com.qa/",
        "Boursa Kuwait": "https://www.boursakuwait.com.kw/",
        "Johannesburg Stock Exchange (JSE)": "https://www.jse.co.za/",
        "Nigerian Exchange Group (NGX)": "https://ngxgroup.com/",
        "ZAWYA (MENA business/financial news)": "https://www.zawya.com/",
        "MAGNiTT (MENA startup/VC data)": "https://magnitt.com/",
    },
}

CONFLICT_RESOLUTION_RULE = (
    "If sources disagree: internal evidence beats public web, audited filings beat "
    "third-party databases, and a named source with a date beats an undated one. "
    "Never estimate a figure from general knowledge — cite where every number came from."
)


def source_bucket(*, geography: str, is_listed: bool, is_pe_vc_funded: bool) -> str:
    """Picks which link set to seed the prompt with. `geography` is a coarse
    tag resolved during identity resolution (e.g. 'india', 'us', 'mea')."""
    geography = geography.lower()
    if geography == "india":
        if is_pe_vc_funded and not is_listed:
            return "india_funded"
        return "india_listed" if is_listed else "india_unlisted"
    if geography in ("mea", "middle east", "gcc", "africa"):
        return "mea"
    return "us"


def build_research_queries(contracting_entity: str, *, bucket: str) -> list[str]:
    """A short, explicit set of targeted search queries covering the same
    ground `build_research_prompt()`'s `needed` list asks for -- issued
    upfront and CONCURRENTLY via `exa_search.search_many()` (see
    evidence_assembler.gather_secondary_research()), replacing the per-round
    query DECISIONS Anthropic's own agentic web_search tool used to make one
    at a time, serially, taking minutes. Mirrors the real query phrasing
    patterns confirmed live across several full runs (e.g. "{entity}
    screener.in financials", "{entity} shareholding pattern promoter") —
    fixed and generic rather than derived mechanically from the `needed`
    list's own long prose asks, which don't make good raw search queries."""
    primary_source = next(iter(SOURCE_LINKS.get(bucket, {})), "")
    source_hint = primary_source.split(" (")[0] if primary_source else ""
    return [
        f"{contracting_entity} {source_hint} financials".strip(),
        f"{contracting_entity} latest quarterly results revenue operating margin",
        f"{contracting_entity} annual report full year revenue EBITDA",
        f"{contracting_entity} peers competitors industry margin comparison",
        f"{contracting_entity} shareholding pattern promoter ownership",
        f"{contracting_entity} recent news CXO change M&A expansion transformation announcement",
        f"{contracting_entity} auditor legal professional fees consultants advisors",
        f"{contracting_entity} strategic priorities management commentary concall",
    ]


def build_research_prompt(
    *,
    company_name: str,
    contracting_entity: str,
    geography: str,
    is_listed: bool,
    is_pe_vc_funded: bool,
    control: OwnershipControl | None,
    needed: list[str],
    today: str | None = None,
) -> str:
    """`needed` is the list of things to find, e.g. ['latest 3 years revenue
    and operating margin', 'three named listed peers and their margins',
    'any of the 9 hard-trigger types dated within the last 12 months'].

    `today` anchors "latest"/"recent" to an actual date -- confirmed live
    this prompt previously had NO date context at all, so the model had no
    way to know a search result citing "as of January 2025" or an "FY23-24
    Annual Report" was stale relative to the real current date, and no
    instruction to prefer a newer filing over an older one when both turn
    up in the same search."""
    bucket = source_bucket(geography=geography, is_listed=is_listed, is_pe_vc_funded=is_pe_vc_funded)
    links = SOURCE_LINKS[bucket]
    link_lines = "\n".join(f"- {label}: {url}" for label, url in links.items())
    needed_lines = "\n".join(f"- {item}" for item in needed)
    control_line = f"\nKnown ownership/control type: {control.value}." if control else ""
    today_line = f"\nToday's date is {today}. Prioritize the MOST RECENT data available as of today — a newer quarterly/interim disclosure beats an older annual one when both exist. Note the age of anything more than 12 months old." if today else ""

    return f"""You are researching the company "{company_name}" (contracting entity: \
"{contracting_entity}") for a pre-sales qualification brief. Use web search.{control_line}{today_line}

Start from these sources (this company's segment: {bucket}) — search beyond them only if \
they don't turn up what's needed for this specific company:
{link_lines}

Find, with a source and a date for every claim:
{needed_lines}

{CONFLICT_RESOLUTION_RULE}

Financial figures must come from filings/exchanges/rating agencies, never from a CRM \
export or an internal system — you have no access to those here regardless.

Return each finding as: claim, source name, source URL, date, and whether it is a \
directly-stated Fact or your Inference from stated data."""


# ---------------------------------------------------------------------------
# A2 (liquidity), B2 (advisory track record) and B3 (stated priorities) used
# to have no dedicated evidence-gathering call at all: P2/P3 get targeted
# Setu questions (setu_questions.py), A1/A3/B1 get targeted asks in the
# `needed` list `evidence_assembler.gather_secondary_research()` builds, but
# A2/B2/B3 were left to whatever happened to surface from the 3 asks above.
# Setu is not the right tool for these three (see setu_questions.py's
# docstring) since the evidence is external market/financial data about the
# *client*, not Practus's own knowledge base -- so this, like the other
# targeted asks, belongs in the secondary-research prompt instead.
# ---------------------------------------------------------------------------


def build_extra_needed_items(control: OwnershipControl | None) -> list[str]:
    """Three additional "needed" asks for `build_research_prompt()`, routed
    by ownership-control type where icp-skill.md provides routing:

    - A2 liquidity (lines 157-169): reuses `A2_LIQUIDITY_ROUTING[control]`
      verbatim rather than re-deriving the routing wording here.
    - B2 advisory track record (lines 237-251): the skill doesn't route
      *where to look* by control (only the scoring norm varies by control,
      which is a scorer concern, not a research-ask concern) -- it names
      one control-agnostic best source for listed targets, the "Legal and
      Professional Fees" P&L note, which this ask surfaces explicitly.
    - B3 stated priorities (lines 262-272): mirrors the skill's own
      per-control/disclosure evidence-bar table.
    """
    if control is not None and control in A2_LIQUIDITY_ROUTING:
        liquidity_ask = (
            f"cash/liquidity position for this {control.value}-controlled entity -- "
            f"{A2_LIQUIDITY_ROUTING[control]}"
        )
    else:
        liquidity_ask = (
            "cash, undrawn facilities, and runway signals (control type not yet known -- "
            "look for group/promoter cash, fund backing, or operating cash generation, "
            "whichever is evidenced for this company)"
        )

    advisory_ask = (
        "evidence of prior professional advisors, consultants, or auditors engaged by the "
        "company: named strategy/ops consultants, statutory auditor (e.g. Big-4), ERP "
        "implementer, or retained recruiter. For a listed entity, the best available source "
        "is the \"Legal and Professional Fees\" line item / note in the P&L -- check it "
        "specifically."
    )

    priorities_ask = _b3_priorities_ask(control)

    return [liquidity_ask, advisory_ask, priorities_ask]


def _b3_priorities_ask(control: OwnershipControl | None) -> str:
    """icp-skill.md's B3 routing table (lines 262-272) keys off BOTH control
    and listed/unlisted disclosure, but this helper only receives `control`
    (per evidence_assembler.py's call site, which doesn't thread `is_listed`
    through here) -- so each branch below states the listed bar generically
    and then gives the control-specific unlisted fallback, letting the
    researcher apply whichever fits the company it finds."""
    base = (
        "stated strategic priorities with a number and a date. If listed: a verbatim "
        "concall/MD&A/board-commentary quote with a number and a date is the bar for a high "
        "score. If unlisted, "
    )
    if control == OwnershipControl.SC:
        return base + (
            "the value-creation plan itself is confidential -- infer priorities from the "
            "sponsor's public investment thesis and this portfolio company's hold-year; a "
            "well-reasoned inference is acceptable here."
        )
    if control == OwnershipControl.SI:
        return base + "check the stated use of funds in the most recent funding-round announcement."
    if control == OwnershipControl.PF:
        return base + (
            "priorities are rarely written down -- they live with the promoter, not in a "
            "document. Look for proxies instead: capex filings, land purchases, new entity "
            "registrations, hiring patterns. Absence of published priorities is not evidence "
            "of no priorities."
        )
    if control == OwnershipControl.CP:
        return base + (
            "priorities cascade from the parent -- check the PARENT company's own "
            "disclosures, not just this local entity's."
        )
    if control == OwnershipControl.JC:
        return base + "check both parent companies' disclosures."
    if control == OwnershipControl.ST:
        return base + "check policy documents and the tender pipeline -- usually specific."
    if control == OwnershipControl.NP:
        return base + "check the annual report and accreditation filings -- often only partial."
    if control == OwnershipControl.WH:
        return base + "check board commentary and analyst notes even absent a formal concall quote."
    return base + "look for direction in board commentary, capex/expansion filings, or hiring patterns as proxies."


# ---------------------------------------------------------------------------
# A structured companion to the free-text call above. The A2 Mode A ticket
# ratio (icp-skill.md §0.4) and the §0.5 scale-mismatch flag both need a
# real number to divide by -- the free-text findings above are prose, not
# something rule_precompute.ticket_ratios() can consume. This is a second,
# schema-forced web-search call (same shape as conflict_checker.py's and
# ownership_classifier.py's) specifically so that number exists as data,
# not just as something the model might mention in passing.
# ---------------------------------------------------------------------------

# Exa's outputSchema has its own real constraints (max nesting depth 2, max
# 10 properties, no documented `additionalProperties` control) -- see
# ownership_classifier.py's CLASSIFICATION_SCHEMA for the fuller note. This
# schema was already flat and small enough to need no other changes.
REVENUE_SCHEMA = {
    "type": "object",
    "required": ["revenue_cr", "ebitda_cr", "post_money_cash_cr", "source", "date"],
    "properties": {
        "revenue_cr": {
            "type": ["number", "null"],
            "description": "Most recent full-year revenue in INR Crore (convert if reported in a different currency/unit). Null if a real filed/audited figure cannot be found -- never estimate.",
        },
        "ebitda_cr": {"type": ["number", "null"], "description": "Most recent full-year EBITDA in INR Crore, or null."},
        "post_money_cash_cr": {
            "type": ["number", "null"],
            "description": "Only for SI (sponsor-influenced) companies: post-money cash on hand after the most recent raise, in INR Crore. Null if not applicable or not findable.",
        },
        "source": {"type": ["string", "null"]},
        "date": {"type": ["string", "null"]},
    },
}


def extract_revenue_figure(
    *,
    contracting_entity: str,
    geography: str,
    is_listed: bool,
    is_pe_vc_funded: bool,
    control: OwnershipControl | None,
    today: str | None = None,
) -> dict:
    """Uses Exa's `outputSchema` synthesis (see exa_search.py) rather than
    Anthropic's own agentic web_search tool -- live-confirmed this drops
    the call from ~30 MINUTES (the single biggest cost of a real, timed
    run — see the plan's "run time" investigation) to ~4-5s for a
    comparably-sourced, correctly-cited figure."""
    bucket = source_bucket(geography=geography, is_listed=is_listed, is_pe_vc_funded=is_pe_vc_funded)
    cash_line = (
        " This is an SI (sponsor-influenced) company -- also find its post-money cash on hand "
        "after the most recent funding round, in INR Crore; otherwise leave post_money_cash_cr null."
        if control == OwnershipControl.SI
        else " post_money_cash_cr only applies to SI (sponsor-influenced) companies -- leave it null here."
    )
    today_line = f" Today's date is {today} — if a more recent quarter/filing exists than the last full fiscal year, prefer it and say so." if today else ""
    system_prompt = (
        f"Find the most recent full-year revenue and EBITDA, in INR Crore, from a real filing, "
        f"exchange disclosure, or rating-agency report only — never estimate or infer from general "
        f"knowledge.{cash_line}{today_line} {CONFLICT_RESOLUTION_RULE} Return null for any figure "
        f"you cannot find from a real source."
    )
    content, _grounding = exa_search.search_and_synthesize(
        f'"{contracting_entity}" ({bucket} segment) most recent full-year revenue and EBITDA financial results',
        output_schema=REVENUE_SCHEMA,
        system_prompt=system_prompt,
        search_type="deep-lite",
    )
    return content


# ---------------------------------------------------------------------------
# P2 fallback: infer a client problem/priority statement from secondary
# research when the CRM has none. Unlike REVENUE_SCHEMA above, this reads
# text ALREADY gathered by research() (no fresh web search needed), so it
# goes through a plain Anthropic call, not Exa's outputSchema synthesis.
# ---------------------------------------------------------------------------

_PROBLEM_STATEMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "problem_statement": {
            "type": ["string", "null"],
            "description": (
                "A concise (1-2 sentence) statement of a real, specific, dated business problem, "
                "priority, or pressure the company itself has stated or is objectively facing, drawn "
                "only from the research text given. Null if nothing genuinely specific is stated -- a "
                "generic strategy phrase ('grow the business', 'improve efficiency', 'expand "
                "operations') does not count on its own."
            ),
        },
        "rationale": {"type": "string", "description": "One sentence: why this is (or isn't) specific enough to count."},
    },
    "required": ["problem_statement", "rationale"],
    "additionalProperties": False,
}


def infer_problem_statement_from_research(research_text: str, contracting_entity: str) -> str | None:
    """icp-skill.md P2 step 1's priority order names 5 sources for
    establishing "the client's problem": the Potential's own Deal_Name/
    Problem_Area/Client_s_Problem_Statement fields, then Read.ai, then
    Outlook, then disclosures, then inference -- but nothing in this
    pipeline ever tried the "disclosures"/"inference" end of that list
    against the secondary-research text this pipeline already gathers for
    every company. Live-confirmed real gap (Uniparts India, 2026-09-09): a
    no-CRM manual-entry company's secondary research contained rich, dated,
    quoted management priorities (an EBITDA margin target, a near-shoring
    expansion plan, an M&A ROCE/ROE hurdle, a customer-concentration risk
    figure) -- exactly the kind of "stated priority" icp-skill.md's own P2
    band table anticipates -- yet P2 scored 1/5 "problem not established"
    because nothing ever looked at this already-gathered text for it. The
    Setu case-study lookup this feeds was ALSO being skipped outright in
    this exact scenario (see evidence_assembler.gather_setu_evidence()'s
    own "no client problem statement available" skip path).

    Mirrors practus_history.confirmed_clients_by_keyword_context()'s same
    honesty philosophy: an LLM call over already-gathered text, returning
    null (not a guess) when nothing genuinely specific is stated -- a
    generic strategy phrase is explicitly excluded so this can't manufacture
    a "problem" out of ordinary business narrative the same way the old
    keyword matcher used to manufacture a "Real Estate" industry out of
    ordinary words."""
    research_text = (research_text or "").strip()
    if not research_text:
        return None

    prompt = f"""Below is secondary research about "{contracting_entity}". Find a real, specific, dated \
business problem, priority, or pressure the company itself has stated or is objectively facing -- \
something a consulting firm could credibly pitch a specific service against. Do NOT invent one, and do \
NOT count a generic strategic-sounding phrase ("grow the business", "improve efficiency", "expand \
operations") as specific enough on its own -- it needs a real, concrete anchor (a named initiative, a \
stated target/number, a disclosed risk, an announced plan).

RESEARCH TEXT:
{research_text[:8000]}

Return null if nothing in this text is genuinely specific enough to count."""

    try:
        data = generate_structured_narrative(prompt, _PROBLEM_STATEMENT_SCHEMA, max_tokens=1000, label="infer_problem_statement")
    except Exception as exc:
        logger.warning("Problem-statement inference from secondary research FAILED: %s — treated as no match.", exc)
        return None

    statement = data.get("problem_statement")
    if statement:
        logger.info(
            "Problem-statement inferred from secondary research: %r — %s", statement, data.get("rationale", "")
        )
    return statement or None

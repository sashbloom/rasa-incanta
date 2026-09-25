# Copied from icp-bot/backend/domains/mahak/people/mahak/icp/ownership_classifier.py (read-only reference repo).
# Only imports and plumbing are adapted for Rasa Incanta; the logic is the ICP bot's.
"""Step 0 §0.3 — three-axis ownership classification (icp-skill.md
§0.3): Control (PF/SC/SI/WH/CP/JC/ST/NP), Disclosure (Listed/Unlisted),
Management (Owner-managed/Professionally managed). Verified from the
shareholding pattern (screener) or MGT-7 via web search — explicitly
**never** from Zoho's `Ownership_Type` field, which the skill calls
"BD-entered and frequently wrong."

One Anthropic call, web search enabled, forced into a closed schema — same
rule-vs-LLM split as scoring: the model researches and cites, but the
`OwnershipControl` value it returns is still validated against the closed
enum before anything downstream trusts it (belt-and-braces on top of the
schema's own `enum` constraint, matching `llm_interpreter.py`'s convention
of never trusting a raw model string without a second check).

Also resolves the coarse `geography`/`is_listed`/`is_pe_vc_funded` flags
`secondary_research.py` needs to pick its source bucket — this call runs
before that one specifically so those flags exist by the time the deeper
financial-research prompt needs them.

`classify()` uses Exa's `outputSchema` synthesis (see exa_search.py)
rather than Anthropic's own agentic web_search tool — live-confirmed this
drops the call from ~27s to ~4-5s for a comparably-sourced, cited result,
one of the four calls identified as the dominant cost of a run (see the
plan's "run time" investigation). `type="deep"` (not `"deep-lite"`) is
used here specifically because this call has to make a real judgment
across several distinct sub-questions (control-type enum, ipo_track
evidence, management style), not just extract one number.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, Field

from . import exa_search
from .models import EvidenceLabel, OwnershipControl

# Exa's outputSchema has real constraints, different from Anthropic's own
# json_schema support: max nesting depth 2, max 10 total properties, and
# its own docs say not to add citation/confidence-style fields since
# /search already returns per-field citations via `output.grounding`
# automatically (see exa_search.search_and_synthesize()'s return value and
# _sources_from_grounding() below) -- so this schema, unlike the old one,
# has no nested "sources" array; `additionalProperties` also isn't one of
# Exa's documented schema controls (type/description/required/properties/
# items only), so it's dropped here too.
CLASSIFICATION_SCHEMA = {
    "type": "object",
    "required": ["geography", "is_listed", "is_pe_vc_funded", "control", "management", "ipo_track", "confidence", "rationale"],
    "properties": {
        "geography": {"type": "string", "enum": ["india", "us", "mea", "other"]},
        "is_listed": {"type": "boolean"},
        "is_pe_vc_funded": {"type": "boolean"},
        "control": {
            "type": "string",
            "enum": ["PF", "SC", "SI", "WH", "CP", "JC", "ST", "NP"],
            "description": "PF=promoter/family, SC=sponsor controlled, SI=sponsor influenced, WH=widely held, CP=corporate parent, JC=jointly controlled, ST=state controlled, NP=non-profit.",
        },
        "management": {"type": "string", "enum": ["Owner-managed", "Professionally managed"]},
        "ipo_track": {
            "type": "boolean",
            "description": "True ONLY if there is actual evidence of IPO preparation — a filed DRHP, a publicly announced IPO timeline, or board approval for a listing. Do not set true on mere speculation, analyst chatter, or the fact that peers in the sector have listed.",
        },
        "confidence": {"type": "string", "enum": ["Fact", "Inference", "Assumption", "Data gap"]},
        "rationale": {"type": "string", "description": "One or two sentences citing the shareholding evidence found."},
    },
}


class OwnershipSource(BaseModel):
    claim: str
    source: str
    url: str | None = None
    date: str | None = None


class OwnershipClassification(BaseModel):
    geography: str  # "india" | "us" | "mea" | "other"
    is_listed: bool
    is_pe_vc_funded: bool
    control: OwnershipControl
    disclosure: str  # "Listed" | "Unlisted" — derived from is_listed for EntityResolution's field
    management: str  # "Owner-managed" | "Professionally managed"
    ipo_track: bool = False
    confidence: EvidenceLabel
    rationale: str = ""
    sources: list[OwnershipSource] = Field(default_factory=list)


def build_prompt(company_name: str, contracting_entity: str) -> str:
    return f"""Classify the ownership structure of "{contracting_entity}" (the contracting \
entity for "{company_name}"). Use web search — check the shareholding pattern (screener.in \
for India-listed), MCA/MGT-7 filings (India-unlisted), SEC filings (US), or the equivalent \
public filing for other geographies. Do NOT rely on general knowledge without a source. \
For an India-unlisted company specifically, also check for a family trust structure (a named \
trust holding the promoter/founder family's shares, common for Indian founder-led companies \
that have also taken institutional funding) — this is a distinct, common real-world pattern \
that is easy to miss if you only search for "the founder's % holding" directly.

Determine:
1. Geography (india / us / mea / other) and whether the entity is listed on a public exchange.
2. Whether it is PE/VC funded (a fund holds a stake, with or without control).
3. Control type — exactly one: PF (promoter/family holds effective control, INCLUDING via a \
family trust structure even if day-to-day is run by a professional CEO), SC (PE fund >50% or \
effective control via shareholders' agreement), SI (VC/PE minority with board rights, founder \
retains control), WH (no single controller — board plus institutions), CP (subsidiary of \
another operating company, including an MNC arm), JC (joint venture, no single parent decides \
alone), ST (state-controlled — PSU or government body), NP (non-profit — trust, society, \
Section 8 company). A PROFESSIONAL CEO/MD running day-to-day operations is a `management` \
fact, NOT evidence of `control` type on its own — many PF (promoter/family-controlled) \
companies are professionally managed day-to-day while the promoter family or its trust still \
holds effective control; do not infer SI or WH control just because a hired executive, not the \
founder personally, holds the CEO/MD title.
4. Management — Owner-managed, or Professionally managed (founder/family sets direction but \
day-to-day is run by professional executives).
5. IPO-track status — as part of the same research, check for concrete signs the entity is \
pursuing or preparing for a public listing (a filed DRHP, an announced IPO timeline, board \
approval for a listing); set ipo_track to true only when such evidence exists, not on speculation.

A SHAREHOLDING TABLE IS A SNAPSHOT, NOT THE CURRENT CONTROL POSITION. Before settling on a \
control type, separately check for a change-of-control event in the last 18 months: a PE/VC \
fund or strategic acquirer buying into (or out of) the promoter block, a regulatory approval \
for a change in control (RBI/CCI/SEBI in India), an open offer, a completed or announced \
stake sale, or reporting that a founder/family era is ending. A quarterly shareholding \
pattern is filed as of a past date and routinely PREDATES exactly such a transaction — \
promoters can still show a large holding in the latest filed table while that same block is \
already under an approved acquisition. Live-confirmed failure (Manappuram Finance, \
2026-09-11): this call read a June-2026 shareholding table showing promoters at 41.66%, \
concluded PF with confidence="Fact", and never surfaced that Bain Capital held RBI approval \
to acquire up to 41.7% and take control — which is SC, not PF, and inverts the entire \
archetype playbook the report's pitch advice is built from.

So: if a change-of-control transaction is announced, approved, or completed, classify on the \
POST-TRANSACTION structure, name the acquirer and the transaction's status in `rationale`, and \
treat that transaction as the governing evidence over any older shareholding table.

JC vs SC when a FUND is involved: JC means a joint venture between OPERATING companies — two \
corporate parents, neither of which decides alone (the playbook for it is literally "map both \
parents' objectives" and "phase to the slower parent"). A PE/VC fund that has bought into the \
promoter block and now sits as a co-promoter beside a founder/family under a shareholders' \
agreement is NOT a joint venture — that is SC (sponsor controlled), which the definition above \
already covers with "effective control via shareholders' agreement". Only classify JC when both \
controlling parties are operating businesses. Note that a fund routinely acquires through named \
investment vehicles rather than under its own brand (e.g. "BC Asia Investments ... Limited" is \
Bain Capital); a vehicle name that does not look like a fund is still a fund — say which fund it \
belongs to in `rationale` where you can establish it, and do not let an unfamiliar acquirer name \
push you toward JC or CP.

Cite a source and date for every claim. Reserve confidence="Fact" for the CONTROL type \
specifically for when you found a real, specific primary-source document (a shareholding \
percentage breakdown, a named promoter/trust with its stated holding, an MCA filing) AND you \
have checked it against change-of-control news of a later date — a shareholding table alone, \
uncorroborated against more recent events, is confidence="Inference", never "Fact", however \
primary the document is. Likewise, an inference drawn from indirect signals (funding history, \
who holds the CEO title, general company reputation) is confidence="Inference" at best, never \
"Fact", even if you are personally confident in it. If you cannot find reliable shareholding \
evidence at all, say so explicitly rather than guessing — set confidence to "Data gap" and make \
your best inference the assumption of last resort, clearly labeled as such."""


def classify(company_name: str, contracting_entity: str) -> OwnershipClassification:
    # The retrieval query has to ASK for change-of-control events, not just
    # the shareholding pattern -- see build_prompt()'s "A SHAREHOLDING TABLE
    # IS A SNAPSHOT" block for the live Manappuram failure this closes.
    # Instructing the model to weigh a transaction over a stale table is
    # useless if the search that feeds it only ever retrieves the table:
    # confirmed live that the SAME vendor, asked about change of control in
    # a separate 2.5s query, returned the Bain/RBI story three different
    # ways (including primary-source company press releases) while this
    # call's own shareholding-only query surfaced none of it across 24
    # grounded citations.
    content, grounding = exa_search.search_and_synthesize(
        f'Ownership and control of "{contracting_entity}" (the contracting entity for '
        f'"{company_name}") -- promoter/founder holding, PE/VC investors, listing status, '
        f"latest shareholding pattern; AND any change of control in the last 18 months: "
        f"stake acquisition or sale, PE/strategic buyer entering the promoter block, "
        f"regulatory approval for change in control (RBI/CCI/SEBI), open offer, or a "
        f"founder/family era ending.",
        output_schema=CLASSIFICATION_SCHEMA,
        system_prompt=build_prompt(company_name, contracting_entity),
        search_type="deep",
    )
    return _from_data(content, sources=_sources_from_grounding(grounding))


def _sources_from_grounding(grounding: list[dict]) -> list[OwnershipSource]:
    """Exa's `outputSchema` synthesis returns its own per-field citations
    (`output.grounding`, a `[{field, citations: [{url, title}], confidence}]`
    list) instead of asking the model to author a `sources` array itself —
    this maps that real, structured evidence onto the same `OwnershipSource`
    shape every existing caller/render template already expects, so nothing
    downstream of `classify()` needed to change."""
    sources: list[OwnershipSource] = []
    for entry in grounding:
        field = entry.get("field", "")
        for citation in entry.get("citations", []) or []:
            sources.append(OwnershipSource(claim=field, source=citation.get("title") or citation.get("url") or "Exa", url=citation.get("url")))
    return sources


def parse_response(text: str) -> OwnershipClassification:
    import json

    return _from_data(json.loads(text))


def _from_data(data: dict, *, sources: list[OwnershipSource] | None = None) -> OwnershipClassification:
    control_raw = data.get("control")
    try:
        control = OwnershipControl(control_raw)
    except ValueError:
        # Belt-and-braces on top of the schema's own enum constraint — see
        # module docstring. An out-of-enum value becomes an explicit data
        # gap rather than a guessed control type, matching
        # llm_interpreter.py's convention for invalid scoring labels.
        control = OwnershipControl.WH
        data["confidence"] = EvidenceLabel.DATA_GAP.value
        data["rationale"] = (data.get("rationale") or "") + f" [Model returned invalid control type {control_raw!r} — defaulted to WH, ownership unclear.]"

    if data.get("confidence") == EvidenceLabel.DATA_GAP.value:
        # icp-skill.md scenario 48: "Ownership unresolvable -> state it,
        # default to WH vocabulary, reduce confidence." A schema-valid but
        # unconfident guess still isn't a resolved classification — the
        # model's own "best inference of last resort" (its prompt invites
        # exactly that) must not silently become the scored control type.
        control = OwnershipControl.WH

    return OwnershipClassification(
        geography=data.get("geography", "other"),
        is_listed=bool(data.get("is_listed", False)),
        is_pe_vc_funded=bool(data.get("is_pe_vc_funded", False)),
        control=control,
        disclosure="Listed" if data.get("is_listed") else "Unlisted",
        management=data.get("management", "Professionally managed"),
        ipo_track=bool(data.get("ipo_track", False)),
        confidence=EvidenceLabel(data.get("confidence", EvidenceLabel.DATA_GAP.value)),
        rationale=data.get("rationale", ""),
        sources=sources if sources is not None else [OwnershipSource(**s) for s in data.get("sources", [])],
    )

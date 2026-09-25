# Copied from icp-bot/backend/domains/mahak/people/mahak/icp/rule_precompute.py (read-only reference repo).
# Only imports and plumbing are adapted for Rasa Incanta; the logic is the ICP bot's.
"""Deterministic, pre-LLM computations: date math, A2 mode selection,
ticket ratios, and the scale-mismatch flag (icp-skill.md §0.4-0.5, B1's
staleness hard rule, Gate 5). None of this touches unstructured text —
it only reads already-structured fields, so it never needs a model call.
"""

from __future__ import annotations

from datetime import datetime

from .criteria_tables import B1_STALENESS_CAP_DAYS
from .models import (
    STAGES_WITH_REAL_TICKET,
    CriterionInterpretation,
    CrmStructured,
    EvidenceLabel,
    Stage,
    SupportingFact,
    UnstructuredEvidenceItem,
)


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value[: len(fmt) + 2], fmt)
        except ValueError:
            continue
    return None


def days_since(value: str | None, now: datetime) -> int | None:
    dt = parse_dt(value)
    if dt is None:
        return None
    return (now - dt).days


def latest_stage_change_time(crm: CrmStructured) -> str | None:
    """`deal_history` is append-only; the most recent row is the last stage
    transition. Falls back to `stage_modified_time` if history is absent."""
    if crm.stage_history:
        return max(crm.stage_history, key=lambda row: row.modified_time).modified_time
    return crm.stage_modified_time


def is_stale(crm: CrmStructured, now: datetime) -> bool:
    """Gate 5 / B1's hard cap: no stage movement in 180+ days."""
    age = days_since(latest_stage_change_time(crm), now)
    return age is not None and age >= B1_STALENESS_CAP_DAYS


def a2_mode(stage: str | None) -> str:
    """icp-skill.md §0.4 / scenarios #8, #11-13: A2 is only ever computed
    against a real ticket at QP-or-later stages ('Mode A'); everything
    earlier uses absolute liquidity only ('Mode B'), with no ratio."""
    try:
        stage_enum = Stage(stage) if stage else None
    except ValueError:
        stage_enum = None
    return "A" if stage_enum in STAGES_WITH_REAL_TICKET else "B"


RUPEES_PER_CRORE = 10_000_000.0


def ticket_ratios(amount: float | None, revenue: float | None, ebitda: float | None) -> dict[str, float | None]:
    """Only meaningful in A2 Mode A. `amount` is CRM `deals.amount`, used
    here ONLY as pursuit context per icp-skill.md line 516 — never as an
    input to A1/A3.

    Real bug (Sobha, 2026-09-10): `amount` (evidence_assembler.py passes
    `crm.amount_context_only`, Zoho's raw rupee `Amount` field, e.g.
    9,000,000 for a ₹90L ticket) was divided directly against `revenue`/
    `ebitda`, which are ALREADY in crores (secondary_financials.revenue_cr/
    ebitda_cr) -- a genuine ₹90L-vs-₹5,190cr ticket (a real, unremarkable
    ~0.017% ratio) came out as "173,410%" (~1,734x), presented in a live
    report as a fabricated headline red flag and an urgent CTA to a named
    person to "verify" a CRM data error that was actually a code bug.
    `amount` must be converted to crores before the division."""
    if amount is None or revenue in (None, 0):
        return {"ticket_to_revenue_pct": None, "ticket_to_ebitda_pct": None}
    amount_cr = amount / RUPEES_PER_CRORE
    return {
        "ticket_to_revenue_pct": round(100 * amount_cr / revenue, 4),
        "ticket_to_ebitda_pct": round(100 * amount_cr / ebitda, 4) if ebitda else None,
    }


def scale_mismatch_flag(*, stage: str | None, ticket_to_revenue_pct: float | None, c1_score: int | None) -> bool:
    """icp-skill.md §0.5 — requires BOTH: stage QP+ and ticket/revenue
    implausibly small (<~0.01%), AND C1 <= 2. A small ticket alone (e.g. a
    deliberate pilot with a senior sponsor) does not fire this on its own —
    see scenarios #42/#43."""
    if a2_mode(stage) != "A":
        return False
    if ticket_to_revenue_pct is None or ticket_to_revenue_pct >= 0.01:
        return False
    return c1_score is not None and c1_score <= 2


def count_client_lost(stage_history_stage_values: list[str]) -> int:
    return sum(1 for s in stage_history_stage_values if s == Stage.CLIENT_LOST)


# ---------------------------------------------------------------------------
# C2 · Warmth & multi-threading (icp-skill.md lines 306-315) -- rule-based,
# excluded from llm_interpreter.LLM_CRITERIA on purpose. Previously that
# exclusion was a lie: nothing here ever actually computed it, so every run
# silently fell through to scorer.py's generic "no interpretation" default
# of 2. This is the real computation the module docstring always claimed
# existed. Sources per the skill: the Potential's Reachout_tracker, plus
# meeting/Outlook touches already gathered into `bundle.unstructured`.
# ---------------------------------------------------------------------------


_SENIOR_TITLE_KEYWORDS = (
    "ceo", "cfo", "coo", "cto", "chro", "cmo", "cio", "cxo",
    "chief", "president", "director", "vp", "vice president",
    "head of", "founder", "owner", "chairman", "chairwoman",
    "managing director", "partner", "promoter",
)


def _is_senior_contact(row) -> bool:
    """icp-skill.md C2: the bands are explicitly about "senior contacts",
    not contacts generally -- previously any named/emailed tracker row
    counted, so two junior touches could score a 5 as easily as two CXO
    touches. `contact_role` (Signatory/Sponsor/Influencer/
    Information-gatherer, the same vocabulary C1 classifies against) is the
    stronger signal when populated; designation keywords are the fallback
    for rows where it isn't."""
    role = (row.contact_role or "").strip().lower()
    if role in ("signatory", "sponsor"):
        return True
    if role in ("influencer", "information-gatherer", "information gatherer"):
        return False
    designation = (row.designation or "").strip().lower()
    return any(keyword in designation for keyword in _SENIOR_TITLE_KEYWORDS)


def c2_warmth_interpretation(
    crm: CrmStructured, unstructured: list[UnstructuredEvidenceItem], now: datetime
) -> CriterionInterpretation:
    """"Multiple senior contacts" is approximated as 2+ distinct named/emailed
    SENIOR contacts in Reachout_tracker -- the CRM's own contact structure
    (see `_is_senior_contact`). Meeting (Read.ai) and Outlook touches
    contribute to *recency* only: their free text doesn't reliably name a
    distinct, verifiable person the way a tracker row does, so counting
    them toward the "multiple" threshold would risk inflating warmth off an
    unattributed thread."""
    all_contacts = {
        (row.email or row.client_contact_name).strip().lower()
        for row in crm.reachout_tracker
        if row.email or row.client_contact_name
    }
    senior_contacts = {
        (row.email or row.client_contact_name).strip().lower()
        for row in crm.reachout_tracker
        if (row.email or row.client_contact_name) and _is_senior_contact(row)
    }
    touch_dates = [row.reachout_date for row in crm.reachout_tracker if row.reachout_date]
    touch_dates += [item.date for item in unstructured if item.source_type in ("meeting", "outlook") and item.date]

    ages = sorted(a for a in (days_since(d, now) for d in touch_dates) if a is not None)
    most_recent_age = ages[0] if ages else None

    facts = [
        SupportingFact(
            claim=f"Reachout tracker: {row.client_contact_name or row.email} on {row.reachout_date}"
            + (f" ({row.designation})" if row.designation else ""),
            label=EvidenceLabel.FACT,
            source="Zoho Reachout_tracker",
            date=row.reachout_date,
        )
        for row in crm.reachout_tracker
        if row.reachout_date
    ]
    # Real bug (Livpure Smart Homes, 2026-09-10, a no-CRM manual entry):
    # `touch_dates` above already factors in meeting/Outlook dates, so a
    # recent meeting alone (with an empty Reachout_tracker, common for a
    # manual entry) could drive the label to a real "contact_exists_..."
    # band -- but `facts` only ever recorded Reachout_tracker rows, so the
    # object came back with a non-data-gap label/score and `data_gap=False`
    # while ALSO `supporting_facts=[]`/`evidence_label_overall=DATA_GAP` --
    # an internally-contradictory result. The narrative correctly saw empty
    # supporting_facts and wrote "cannot be verified," while the score
    # silently used the meeting/Outlook date anyway. Record what actually
    # drove the score.
    facts += [
        SupportingFact(
            claim=f"{item.source_type.capitalize()} touch on {item.date} ({item.source})",
            label=EvidenceLabel.FACT,
            source=item.source,
            date=item.date,
        )
        for item in unstructured
        if item.source_type in ("meeting", "outlook") and item.date
    ]

    if not all_contacts and most_recent_age is None:
        label = "no_contact_at_all"
    elif most_recent_age is None:
        label = "contact_exists_no_meeting_or_over_180d"
    elif most_recent_age <= 60:
        # Bands 4/5 are explicitly about SENIOR contacts (icp-skill.md C2) --
        # a merely junior contact touched recently still clears "contact
        # exists," just not the seniority bar, so it falls to band 3 rather
        # than falsely claiming seniority or dropping to "no contact."
        if len(senior_contacts) >= 2:
            label = "multiple_senior_contacts_meeting_last_60d"
        elif len(senior_contacts) == 1:
            label = "one_senior_contact_meeting_last_60d"
        else:
            label = "contact_exists_last_touch_60_to_180d"
    elif most_recent_age <= 180:
        label = "contact_exists_last_touch_60_to_180d"
    else:
        label = "contact_exists_no_meeting_or_over_180d"

    return CriterionInterpretation(
        criterion_id="C2",
        condition_label=label,
        supporting_facts=facts,
        evidence_label_overall=EvidenceLabel.FACT if facts else EvidenceLabel.DATA_GAP,
        rationale="Rule-based from Reachout_tracker contact count/recency plus meeting/Outlook touch dates (icp-skill.md C2).",
        data_gap=label == "no_contact_at_all",
    )

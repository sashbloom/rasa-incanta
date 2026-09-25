# Copied from icp-bot/backend/domains/mahak/people/mahak/icp/verdicts.py (read-only reference repo).
# Only imports and plumbing are adapted for Rasa Incanta; the logic is the ICP bot's.
"""Verdict bands, recommendation matrix, and archetype/pricing lookup
(icp-skill.md VERDICT BANDS, RECOMMENDATION MATRIX, ARCHETYPE PLAYBOOK)."""

from __future__ import annotations

from .models import OwnershipControl


def round_half_up(value: float) -> int:
    """'Whole numbers only' (icp-skill.md, repeated throughout) — standard
    round-half-up, since Python's builtin `round()` uses banker's rounding
    and would silently disagree with a human doing this by hand."""
    return int(value + 0.5) if value >= 0 else -int(-value + 0.5)


def client_lens_verdict(total: int) -> str:
    if total >= 37:
        return "Strong"
    if total >= 27:
        return "Moderate"
    return "Weak"


def practus_lens_verdict(total: int) -> str:
    # Same bands apply to both 50-point lenses (icp-skill.md VERDICT BANDS).
    return client_lens_verdict(total)


GROUP_BANDS: dict[str, dict[str, tuple[int, int | None]]] = {
    "Ability to Pay": {"Strong": (11, None), "Moderate": (8, 10), "Weak": (0, 7)},
    "Willingness to Pay": {"Strong": (15, None), "Moderate": (11, 14), "Weak": (0, 10)},
    "Access": {"Strong": (11, None), "Moderate": (8, 10), "Weak": (0, 7)},
}


def group_sub_verdict(group_name: str, subtotal: int) -> str:
    bands = GROUP_BANDS[group_name]
    for label, (lo, hi) in bands.items():
        if subtotal >= lo and (hi is None or subtotal <= hi):
            return label
    return "Weak"


def criterion_display_band(raw_score: int) -> str:
    """Per-criterion display band for the Practus-lens sub-verdict pills
    (P1-P4 shown individually as e.g. 'Team 5/5'). Not spelled out
    numerically in icp-skill.md's prose, but consistent with the real
    generated samples (Sobha - ICP.html): 5 -> Strong, 4 -> Moderate,
    <=3 -> Weak."""
    if raw_score >= 5:
        return "Strong"
    if raw_score == 4:
        return "Moderate"
    return "Weak"


RECOMMENDATION_MATRIX: dict[tuple[str, str], str] = {
    ("Strong", "Strong"): "Pursue aggressively",
    ("Strong", "Moderate"): "Pursue selectively",
    ("Strong", "Weak"): "Nurture",
    ("Moderate", "Strong"): "Pursue selectively",
    ("Moderate", "Moderate"): "Nurture",
    ("Moderate", "Weak"): "Park",
    ("Weak", "Strong"): "Park",
    ("Weak", "Moderate"): "Park",
    ("Weak", "Weak"): "Park",
}


def recommendation_matrix_cell(client_verdict: str, practus_verdict: str) -> str:
    return RECOMMENDATION_MATRIX[(client_verdict, practus_verdict)]


# ---------------------------------------------------------------------------
# Archetype / pricing playbook (icp-skill.md ARCHETYPE PLAYBOOK, 561-580)
# ---------------------------------------------------------------------------


def pricing_posture(ability_score: int) -> dict[str, str]:
    if ability_score >= 11:
        return {"band": "Strong", "structure": "Fixed-fee or retainer at standard rates", "why": "Liquidity absorbs traditional billing"}
    if ability_score >= 8:
        return {
            "band": "Moderate",
            "structure": "60-70% fixed anchor + 30-40% milestone-linked",
            "why": "Can pay; values value-linked structure",
        }
    return {
        "band": "Weak",
        "structure": "Outcome-linked against EBITDA points or working-capital days released",
        "why": "Cannot fund hourly burn",
    }


ARCHETYPE_PLAYBOOK: dict[OwnershipControl, dict[str, object]] = {
    OwnershipControl.PF: {
        "do": ["Institutionalisation without diluting control", "Professionalising without losing the DNA", "Succession framing", "Trust cadence"],
        "dont": ["Bypass the promoter", "Jargon", "Pure cost-out threatening family employees", "Exit vocabulary"],
        "pricing_note": "Phased anchor with hard ROI proof.",
    },
    OwnershipControl.SC: {
        "do": ["EBITDA uplift", "Cash and deleverage", "IRR and multiple expansion", "VCP alignment", "Hold-year framing"],
        "dont": ["Long discovery", "Maturity models", "Culture work without a P&L hook"],
        "pricing_note": "At least one milestone band always.",
    },
    OwnershipControl.SI: {
        "do": ["Unit economics", "Path to profitability", "Scale-readiness"],
        "dont": ["Cost-out to a growth company", "Assume the investor can mandate"],
        "pricing_note": "Milestone-gated to funding events.",
    },
    OwnershipControl.WH: {
        "do": ["Board confidence", "Capital-allocation discipline", "Quarterly cadence", "Benchmarks"],
        "dont": ["Over-personalise to one CXO", "Miss the budget window"],
        "pricing_note": "Fixed-fee with gates.",
    },
    OwnershipControl.CP: {
        "do": ["Local execution plus parent alignment", "Group KPI integration"],
        "dont": ["Pretend the parent doesn't exist", "Assume local authority"],
        "pricing_note": "Check the local limit first.",
    },
    OwnershipControl.JC: {
        "do": ["Map both parents' objectives"],
        "dont": ["Assume one parent's yes suffices"],
        "pricing_note": "Phase to the slower parent.",
    },
    OwnershipControl.ST: {
        "do": ["Confirm empanelment first", "Align to tender scope"],
        "dont": ["Relationship-sell into a tender"],
        "pricing_note": "Tender-compliant.",
    },
    OwnershipControl.NP: {
        "do": ["Mission-aligned framing", "Surplus sensitivity"],
        "dont": ["Commercial-transformation vocabulary"],
        "pricing_note": "Right-sized.",
    },
}


# IPO-track overlay (icp-skill.md ARCHETYPE PLAYBOOK, ~line 579) — an
# ADD-ON layered on top of whichever control-type playbook above already
# applies (per icp-skill.md's own "Add Do"/"Add Don't" framing), never a
# replacement for it, so this is kept as its own small structure rather
# than merged into ARCHETYPE_PLAYBOOK's per-control dict.
IPO_TRACK_OVERLAY: dict[str, object] = {
    "add_dos": ["IPO-readiness", "FP&A maturity for DRHP", "Board-pack discipline"],
    "add_donts": ["Anything conflicting with sensitive disclosures"],
    "pricing_note": "Milestones gated to DRHP, listing, +6 months.",
}

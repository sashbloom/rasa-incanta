# Copied from icp-bot/backend/domains/mahak/people/mahak/icp/criteria_tables.py (read-only reference repo).
# Only imports and plumbing are adapted for Rasa Incanta; the logic is the ICP bot's.
"""Verbatim scoring tables from icp-skill.md.

Every dict here is a direct transcription of one table in the skill. Each
one is docstring-cited to the section it comes from so a future edit to
icp-skill.md has an obvious place to update. `condition_label` values are
the closed enum the LLM Interpretation stage is allowed to emit for that
criterion (see models.CriterionInterpretation) — `scorer.py` does a plain
dict lookup here to turn a label into a score. The model never reports a
number; this file is the only thing that does.
"""

from __future__ import annotations

from .models import OwnershipControl

# ---------------------------------------------------------------------------
# Weights (icp-skill.md STEP 2 / STEP 3) — sum to 10.0 per lens, times max
# raw score 5, gives max 50 per lens.
# ---------------------------------------------------------------------------

WEIGHTS: dict[str, float] = {
    # Group A · Ability to Pay — max 15
    "A1": 0.75,
    "A2": 1.50,
    "A3": 0.75,
    # Group B · Willingness to Pay — max 20
    "B1": 1.75,
    "B2": 1.25,
    "B3": 1.00,
    # Group C · Access — max 15
    "C1": 1.75,
    "C2": 0.75,
    "C3": 0.50,
    # Practus lens — max 50
    "P1": 2.50,
    "P2": 3.50,
    "P3": 2.00,
    "P4": 2.00,
}

GROUPS: dict[str, list[str]] = {
    "Ability to Pay": ["A1", "A2", "A3"],
    "Willingness to Pay": ["B1", "B2", "B3"],
    "Access": ["C1", "C2", "C3"],
}

GROUP_MAX_POINTS: dict[str, int] = {
    "Ability to Pay": 15,
    "Willingness to Pay": 20,
    "Access": 15,
}

PRACTUS_CRITERIA = ["P1", "P2", "P3", "P4"]

# ---------------------------------------------------------------------------
# A1 · Scale — weight 0.75 (icp-skill.md lines 122-131)
# ---------------------------------------------------------------------------

A1_SCALE_BANDS: dict[str, int] = {
    "revenue_gte_2000cr": 5,
    "revenue_750_to_2000cr": 4,
    "revenue_250_to_750cr": 3,
    "revenue_75_to_250cr": 2,
    "revenue_lt_75cr": 1,
}
A1_SILENT_DEFAULT = 2
# SI-only uplift: +1 (capped at 5) if post-money cash > INR 200 Cr — applied
# by scorer.py as a post-hoc adjustment, not a table row.

# ---------------------------------------------------------------------------
# A2 · Liquidity — weight 1.50, two modes by stage (lines 133-155)
# ---------------------------------------------------------------------------

A2_MODE_A_BANDS: dict[str, int] = {
    "ticket_lt_0.1pct_and_cash_positive": 5,
    "ticket_lt_0.5pct_and_cash_positive": 4,
    "ticket_lt_1pct_cash_positive_or_marginal": 3,
    "ticket_1_to_3pct_or_cash_negative": 2,
    "ticket_gt_3pct_or_no_liquidity": 1,
}

A2_MODE_B_BANDS: dict[str, int] = {
    "funds_engagement_many_times_over_cash_generative": 5,
    "positive_cash_clear_headroom_no_stress": 4,
    "positive_but_tight_or_preprofit_gt_12mo_runway": 3,
    "negative_cash_no_funding_or_runway_lt_12mo": 2,
    "no_liquidity_or_active_distress": 1,
}

A2_SILENT_DEFAULT = 2

# Where the money sits, by control type (lines 157-169) — tells the
# Setu/secondary-research question-builder and the LLM prompt where to look;
# does not itself produce a score.
A2_LIQUIDITY_ROUTING: dict[OwnershipControl, str] = {
    OwnershipControl.PF: (
        "Liquidity sits across group entities, the promoter balance sheet, and land — "
        "single-entity filings understate it. Look at group cash and promoter holdings; "
        "proxies: Big-4 auditor, entity count, dividend behaviour."
    ),
    OwnershipControl.SC: "Portco cash plus fund dry powder. Look at runway, fund life, follow-on reserve.",
    OwnershipControl.SI: "Post-raise cash. Look at runway and months since last raise.",
    OwnershipControl.WH: (
        "Visible, reliable — operating cash, conversion, working-capital days. "
        "Separate growth capex from distress."
    ),
    OwnershipControl.CP: (
        "Cash is swept to parent — local cash is near-zero by design. "
        "Look at the local authority limit and parent approval route."
    ),
    OwnershipControl.JC: "JV account, often restricted. Look at distribution rules in the JV agreement.",
    OwnershipControl.ST: "Budget allocation vs free reserves. Look at the sanctioned head and unrestricted reserves only.",
    OwnershipControl.NP: "Corpus vs free reserves. Look at the sanctioned head and unrestricted reserves only.",
}

# ---------------------------------------------------------------------------
# A3 · Financial trajectory — weight 0.75 (lines 171-195)
# ---------------------------------------------------------------------------

A3_BANDS: dict[str, int] = {
    "above_range_improving": 5,
    "within_range_stable": 4,
    "within_range_mild_deterioration": 3,
    "below_range_deteriorating": 2,
    "sustained_loss_no_funded_plan": 1,
}
A3_NO_PEERS_DEFAULT = 3  # "Score 3, Data gap" when 3 named peers can't be identified
A3_NA_CONTROL_TYPES = {OwnershipControl.SI, OwnershipControl.ST, OwnershipControl.NP}

# Is reported margin meaningful, by control type (lines 184-193) -- prose
# version for the LLM prompt (llm_interpreter.py _criterion_block), mirroring
# the skill's own wording
# (lines 184-193) the same way A2/B1/C1's *_ROUTING dicts already do.
A3_MEANINGFUL_MARGIN_PROSE: dict[OwnershipControl, str] = {
    OwnershipControl.PF: (
        "Reported margin is understated for tax purposes, not meaningful as-is. "
        "Use adjusted EBITDA or gross margin plus cash generation instead; "
        "default to 3 if it cannot be adjusted."
    ),
    OwnershipControl.SC: (
        "Margin is meaningful — it is the investment thesis. "
        "Compare current margin vs entry margin vs VCP target."
    ),
    OwnershipControl.SI: "Margin is not meaningful — negative by design. Treat as N/A and renormalise.",
    OwnershipControl.WH: "Margin is meaningful. Use peer-derived comparison.",
    OwnershipControl.CP: (
        "Margin is not meaningful — it's an accounting artifact (cost-plus shows "
        "8-12% regardless of performance). Use the parent's segment margin instead."
    ),
    OwnershipControl.JC: (
        "Margin is only partially meaningful — check transfer pricing. "
        "Use segment margin at each parent."
    ),
    OwnershipControl.ST: "Margin is not meaningful (N/A). Use surplus vs budget instead.",
    OwnershipControl.NP: "Margin is not meaningful (N/A). Use surplus vs budget instead.",
}

# ---------------------------------------------------------------------------
# B1 · Trigger & urgency — weight 1.75 (lines 199-235)
# ---------------------------------------------------------------------------

HARD_TRIGGER_TYPES = [
    "cxo_change",
    "m_and_a",
    "new_pe_vc_event",
    "transformation_announced",
    "expansion_announced",
    "performance_shock",
    "regulatory_deadline",
    "covenant_or_refinancing",
    "succession_event",
]

B1_BANDS: dict[str, int] = {
    "named_buyer_deadline_plus_hard_trigger": 5,
    "hard_trigger_within_6mo": 4,
    "live_potential_recent_movement_no_external_trigger": 3,
    "trigger_inferred_or_over_12mo": 2,
    "no_trigger_dormant_over_12mo": 1,
}
B1_SILENT_DEFAULT = 2
B1_STALENESS_CAP_DAYS = 180
B1_STALENESS_CAP_SCORE = 2

B1_TRIGGER_ROUTING: dict[OwnershipControl, str] = {
    OwnershipControl.PF: "MCA director-change filings, charge filings, trade press, conversation.",
    OwnershipControl.SC: (
        "Fund portfolio page -> hold-year. Yr 0-1 = 100-day plan, Yr 2-3 = grind, "
        "Yr 4-5 = exit prep (highest value), Yr 6+ = LP pressure."
    ),
    OwnershipControl.SI: "Tracxn/Entrackr round history, months since raise, investor commentary.",
    OwnershipControl.WH: "Concall Q&A, analyst notes, rating actions.",
    OwnershipControl.CP: "The PARENT's communications — urgency starts at HQ, not locally.",
    OwnershipControl.JC: "Both parents' disclosures.",
    OwnershipControl.ST: "Policy documents, tender portals, budget speeches.",
    OwnershipControl.NP: "Regulator and accreditation calendars.",
}

# ---------------------------------------------------------------------------
# B2 · Advisory track record — weight 1.25, scored relative to control-type
# norm (lines 237-251).
# ---------------------------------------------------------------------------

B2_BANDS_BY_CONTROL: dict[OwnershipControl, dict[str, int]] = {
    OwnershipControl.PF: {
        "named_prior_strategy_ops_consultant": 5,
        "big4_plus_erp_plus_retained_recruiter": 4,
        "big4_auditor_only": 3,
        "no_professional_service": 2,
        "advisor_hostile": 1,
    },
    OwnershipControl.SC: {
        "active_vcp_with_named_advisors": 5,
        "dd_only": 3,
        "none_sponsor_disengaged": 2,
    },
    OwnershipControl.SI: {
        "named_transformation_partner": 5,
        "some_diligence_default": 3,
    },
    OwnershipControl.WH: {"expected_big4_minimum": 4},
    OwnershipControl.CP: {"extensive_via_global_panels": 5},
    OwnershipControl.JC: {"both_parents_panels": 3},
    OwnershipControl.ST: {"empanelled": 4, "not_empanelled": 1},
    OwnershipControl.NP: {"low_often_pro_bono": 2},
}

# ---------------------------------------------------------------------------
# B3 · Stated priorities — weight 1.00 (lines 253-272)
# ---------------------------------------------------------------------------

B3_BANDS: dict[str, int] = {
    "two_plus_priorities_with_number_and_date": 5,
    "one_priority_with_number_and_date": 4,
    "clear_direction_no_numbers_or_dates": 3,
    "direction_inferable_from_proxies_only": 2,
    "nothing_identifiable": 1,
}
B3_PF_UNLISTED_FLOOR = 3  # absence of published priorities != absence of priorities

# ---------------------------------------------------------------------------
# C1 · Authority — weight 1.75 (lines 276-304)
# ---------------------------------------------------------------------------

C1_BANDS: dict[str, int] = {
    "verified_signatory_warm_relationship": 5,
    "verified_signatory_cool_or_strong_sponsor_warm": 4,
    "sponsor_verified_path_to_signatory": 3,
    "influencer_only": 2,
    "information_gatherer_only_or_nobody_identified": 1,
}
C1_GATE2_TRIGGER_SCORE = 1

C1_AUTHORITY_ROUTING: dict[OwnershipControl, str] = {
    OwnershipControl.PF: "Usually one person decides; everyone else is an influencer regardless of title.",
    OwnershipControl.SC: "Two buyers: sponsor operating partner AND portco CEO; check SHA reserved matters.",
    OwnershipControl.SI: "Founder decides, investor influences — do not treat the investor as a signatory.",
    OwnershipControl.WH: "Committee plus procurement — do not over-invest in one CXO who may rotate out.",
    OwnershipControl.CP: "Two-stage: local sponsor, then HQ above threshold.",
    OwnershipControl.JC: "Both parents must agree.",
    OwnershipControl.ST: "Tender committee — relationships do not sign.",
    OwnershipControl.NP: "Trustee board decides.",
}

# ---------------------------------------------------------------------------
# C2 · Warmth & multi-threading — weight 0.75 (lines 306-315) — rule-based
# ---------------------------------------------------------------------------

C2_BANDS: dict[str, int] = {
    "multiple_senior_contacts_meeting_last_60d": 5,
    "one_senior_contact_meeting_last_60d": 4,
    "contact_exists_last_touch_60_to_180d": 3,
    "contact_exists_no_meeting_or_over_180d": 2,
    "no_contact_at_all": 1,
}

# ---------------------------------------------------------------------------
# C3 · Decision process & timing — weight 0.50 (lines 317-326)
# ---------------------------------------------------------------------------

C3_BANDS: dict[str, int] = {
    "single_approver_window_open_now": 5,
    "short_path_window_opens_within_quarter": 4,
    "committee_path_window_open": 3,
    "committee_and_procurement_window_uncertain": 2,
    "long_path_window_just_closed": 1,
}
C3_SILENT_DEFAULT = 3  # never penalise for absent process information

# ---------------------------------------------------------------------------
# P1 · Industry experience — weight 2.50 (lines 332-341)
# ---------------------------------------------------------------------------

P1_BANDS: dict[str, int] = {
    "five_plus_clients_two_plus_citable_cases": 5,
    "three_to_four_confirmed": 4,
    "two_confirmed": 3,
    "one_confirmed": 2,
    "zero_confirmed": 1,
}

# ---------------------------------------------------------------------------
# P2 · Problem-solution proof — weight 3.50, highest in the model (343-365)
# ---------------------------------------------------------------------------

P2_BANDS: dict[str, int] = {
    "named_case_same_problem_same_or_adjacent_industry_stated_impact": 5,
    "named_case_same_problem_different_industry": 4,
    "adjacent_problem_same_industry": 3,
    "generic_capability_no_specific_case": 2,
    "problem_not_established_or_nothing_relevant": 1,
}
P2_GATE6_LABEL = "problem_not_established_or_nothing_relevant"

# ---------------------------------------------------------------------------
# P3 · Team we'd field — weight 2.00 (lines 367-391)
# ---------------------------------------------------------------------------

P3_BANDS: dict[str, int] = {
    "two_plus_ep_el_full_matches_one_active": 5,
    "one_ep_el_full_match": 4,
    "tl_full_match_or_ep_el_adjacency": 3,
    "adjacency_only": 2,
    "nobody_at_ep_el_tl_covers_this": 1,
}
P3_PRIOR_CAREER_UPLIFT = 1  # capped at 5, applied once

# ---------------------------------------------------------------------------
# P4 · Competitive position & warm path — weight 2.00 (lines 397-406)
# ---------------------------------------------------------------------------

P4_BANDS: dict[str, int] = {
    "champion_plus_analogous_win_no_incumbent": 5,
    "champion_or_analogous_win_no_incumbent": 4,
    "neither_but_no_incumbent": 3,
    "named_incumbent_present_have_champion": 2,
    "named_incumbent_entrenched_no_champion": 1,
}

# ---------------------------------------------------------------------------
# All condition-label tables in one place, keyed by criterion_id, for the
# LLM-facing enum whitelist and for scorer.py's generic lookup.
# Control-routed criteria (A3/B2/C1 use the *_ROUTING dicts to build the
# prompt but still resolve through a single flat band table per criterion,
# except B2 which genuinely has different label sets per control type.
# ---------------------------------------------------------------------------

FLAT_CRITERION_BANDS: dict[str, dict[str, int]] = {
    "A1": A1_SCALE_BANDS,
    "A3": A3_BANDS,
    "B1": B1_BANDS,
    "B3": B3_BANDS,
    "C1": C1_BANDS,
    "C2": C2_BANDS,
    "C3": C3_BANDS,
    "P1": P1_BANDS,
    "P2": P2_BANDS,
    "P3": P3_BANDS,
    "P4": P4_BANDS,
}


# ---------------------------------------------------------------------------
# Display names — the short criterion/group labels used verbatim in every
# real generated report (cross-checked against Sobha - ICP.html /
# Sula Wines - ICP.html), for html_renderer.py's scorecard rows and
# sub-verdict pills. Not scoring inputs.
# ---------------------------------------------------------------------------

CRITERION_DISPLAY_NAMES: dict[str, str] = {
    "A1": "Scale",
    "A2": "Liquidity",
    "A3": "Financial trajectory",
    "B1": "Trigger &amp; urgency",
    "B2": "Advisory track record",
    "B3": "Stated priorities",
    "C1": "Authority",
    "C2": "Warmth &amp; threading",
    "C3": "Process &amp; timing",
    "P1": "Industry experience",
    "P2": "Problem–solution proof",
    "P3": "Team we'd field",
    "P4": "Competitive position",
}

GROUP_SHORT_NAMES: dict[str, str] = {
    "Ability to Pay": "Ability",
    "Willingness to Pay": "Willingness",
    "Access": "Access",
}

# Practus-lens sub-verdict pill labels shown per criterion (Sector/Problem
# proof/Team/Position), keyed by criterion_id rather than a group name since
# the Practus lens has no group blocks — it's one flat 4-row scorecard.
PRACTUS_SHORT_NAMES: dict[str, str] = {
    "P1": "Sector",
    "P2": "Problem proof",
    "P3": "Team",
    "P4": "Position",
}


def allowed_labels(criterion_id: str, control: OwnershipControl | None = None) -> list[str]:
    """The closed enum of condition_labels the LLM may emit for a criterion."""
    if criterion_id == "A2":
        return list(A2_MODE_A_BANDS) + list(A2_MODE_B_BANDS)
    if criterion_id == "B2":
        if control is None:
            return sorted({label for bands in B2_BANDS_BY_CONTROL.values() for label in bands})
        return list(B2_BANDS_BY_CONTROL[control])
    return list(FLAT_CRITERION_BANDS[criterion_id])

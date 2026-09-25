# Copied from icp-bot/backend/tests/test_scorer.py; only import paths are adapted.
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.icp import criteria_tables as ct
from api.icp import scorer
from api.icp.models import (
    CriterionInterpretation,
    CrmStructured,
    DealHistoryRow,
    EvidenceLabel,
    OwnershipControl,
)

NOW = datetime(2026, 8, 18)


def _interp(criterion_id: str, label: str, data_gap: bool = False) -> CriterionInterpretation:
    return CriterionInterpretation(
        criterion_id=criterion_id,
        condition_label=label,
        evidence_label_overall=EvidenceLabel.FACT,
        rationale="test fixture",
        data_gap=data_gap,
    )


def _crm(stage: str, stage_change_days_ago: int = 5) -> CrmStructured:
    stage_dt = NOW - timedelta(days=stage_change_days_ago)
    return CrmStructured(
        deal_id=1,
        deal_name="Test Deal",
        stage=stage,
        stage_history=[DealHistoryRow(stage=stage, modified_time=stage_dt.isoformat())],
    )


def _best_b2_label(control: OwnershipControl) -> str:
    """Not every control type's B2 table reaches 5 — e.g. WH's only defined
    band is 'expected; Big-4 minimum' = 4 (icp-skill.md line 245). That's a
    real feature of the norm-relative design, not a gap: pick whatever that
    control's own ceiling is instead of assuming every type can hit 5."""
    bands = ct.B2_BANDS_BY_CONTROL[control]
    return max(bands.items(), key=lambda kv: kv[1])[0]


def _all_fives_interpretations(control: OwnershipControl) -> dict[str, CriterionInterpretation]:
    return {
        "A1": _interp("A1", "revenue_gte_2000cr"),
        "A2": _interp("A2", "ticket_lt_0.1pct_and_cash_positive"),
        "A3": _interp("A3", "above_range_improving"),
        "B1": _interp("B1", "named_buyer_deadline_plus_hard_trigger"),
        "B2": _interp("B2", _best_b2_label(control)),
        "B3": _interp("B3", "two_plus_priorities_with_number_and_date"),
        "C1": _interp("C1", "verified_signatory_warm_relationship"),
        "C2": _interp("C2", "multiple_senior_contacts_meeting_last_60d"),
        "C3": _interp("C3", "single_approver_window_open_now"),
        "P1": _interp("P1", "five_plus_clients_two_plus_citable_cases"),
        "P2": _interp("P2", "named_case_same_problem_same_or_adjacent_industry_stated_impact"),
        "P3": _interp("P3", "two_plus_ep_el_full_matches_one_active"),
        "P4": _interp("P4", "champion_plus_analogous_win_no_incumbent"),
    }


def test_all_fives_scores_max_and_recommends_pursue_aggressively():
    # PF is used here (not WH) because its B2 table is the only one with a
    # full 1-5 range — see _best_b2_label's docstring.
    interpretations = _all_fives_interpretations(OwnershipControl.PF)
    result = scorer.full_score(
        interpretations=interpretations,
        control=OwnershipControl.PF,
        ownership_disclosure="Listed",
        stage="Qualified Prospect",
        crm=_crm("Qualified Prospect"),
        entity_resolved=True,
        entity_note="",
        problem_established=True,
        active_competitor_pursuit=False,
        client_lost_unexplained_count=0,
        now=NOW,
        ticket_to_revenue_pct=5.0,
    )

    assert result.client_total == 50
    assert result.client_verdict == "Strong"
    assert result.practus_total == 50
    assert result.practus_verdict == "Strong"
    assert result.recommendation == "Pursue aggressively"
    assert not any(g.fired for g in result.gates)
    assert not result.provisional


def test_gate_4_fires_and_carries_the_competitor_detail_through_full_score():
    interpretations = _all_fives_interpretations(OwnershipControl.PF)
    result = scorer.full_score(
        interpretations=interpretations,
        control=OwnershipControl.PF,
        ownership_disclosure="Listed",
        stage="Qualified Prospect",
        crm=_crm("Qualified Prospect"),
        entity_resolved=True,
        entity_note="",
        problem_established=True,
        active_competitor_pursuit=True,
        competitor_conflict_detail="An active Practus pursuit exists at 'Rival Co', a named direct competitor.",
        client_lost_unexplained_count=0,
        now=NOW,
        ticket_to_revenue_pct=5.0,
    )

    gate_4 = next(g for g in result.gates if g.gate_id == "gate_4")
    assert gate_4.fired is True
    assert "Rival Co" in gate_4.detail
    # Gate 4 only flags for a human go/no-go -- it never caps the matrix
    # cell the way gates 2/3/6 do (icp-skill.md: "Flag; force explicit
    # go/no-go", not "cap at").
    assert result.recommendation == "Pursue aggressively"


def test_gate_2_caps_at_nurture_even_when_matrix_says_aggressive():
    interpretations = _all_fives_interpretations(OwnershipControl.WH)
    interpretations["C1"] = _interp("C1", "information_gatherer_only_or_nobody_identified")

    result = scorer.full_score(
        interpretations=interpretations,
        control=OwnershipControl.WH,
        ownership_disclosure="Listed",
        stage="Qualified Prospect",
        crm=_crm("Qualified Prospect"),
        entity_resolved=True,
        entity_note="",
        problem_established=True,
        active_competitor_pursuit=False,
        client_lost_unexplained_count=0,
        now=NOW,
    )

    gate_2 = next(g for g in result.gates if g.gate_id == "gate_2")
    assert gate_2.fired is True
    assert result.recommendation == "Nurture"


def test_gate_6_caps_at_pursue_selectively_when_problem_unestablished():
    interpretations = _all_fives_interpretations(OwnershipControl.WH)
    interpretations["P2"] = _interp("P2", "problem_not_established_or_nothing_relevant")

    result = scorer.full_score(
        interpretations=interpretations,
        control=OwnershipControl.WH,
        ownership_disclosure="Listed",
        stage="Qualified Prospect",
        crm=_crm("Qualified Prospect"),
        entity_resolved=True,
        entity_note="",
        problem_established=False,
        active_competitor_pursuit=False,
        client_lost_unexplained_count=0,
        now=NOW,
    )

    gate_6 = next(g for g in result.gates if g.gate_id == "gate_6")
    assert gate_6.fired is True
    assert result.recommendation == "Pursue selectively"


def test_a3_na_for_si_control_renormalizes_group_a_to_same_max():
    interpretations = _all_fives_interpretations(OwnershipControl.SI)
    # A3 is N/A for SI by default (see criteria_tables.A3_NA_CONTROL_TYPES) —
    # don't even give it a label, scorer should mark it N/A automatically.
    del interpretations["A3"]

    result = scorer.full_score(
        interpretations=interpretations,
        control=OwnershipControl.SI,
        ownership_disclosure="Unlisted",
        stage="Qualified Prospect",
        crm=_crm("Qualified Prospect"),
        entity_resolved=True,
        entity_note="",
        problem_established=True,
        active_competitor_pursuit=False,
        client_lost_unexplained_count=0,
        now=NOW,
    )

    group_a = next(g for g in result.client_groups if g.name == "Ability to Pay")
    assert group_a.renormalized is True
    assert group_a.max_points == 15
    # A1 and A2 both scored 5, weights rescaled so max is still reachable at 15.
    assert group_a.subtotal == 15
    a3 = next(c for c in group_a.criteria if c.criterion_id == "A3")
    assert a3.is_na is True


def test_staleness_caps_b1_and_fires_gate_5():
    interpretations = _all_fives_interpretations(OwnershipControl.WH)
    result = scorer.full_score(
        interpretations=interpretations,
        control=OwnershipControl.WH,
        ownership_disclosure="Listed",
        stage="Need Identification",
        crm=_crm("Need Identification", stage_change_days_ago=400),
        entity_resolved=True,
        entity_note="",
        problem_established=True,
        active_competitor_pursuit=False,
        client_lost_unexplained_count=0,
        now=NOW,
    )

    gate_5 = next(g for g in result.gates if g.gate_id == "gate_5")
    assert gate_5.fired is True
    group_b = next(g for g in result.client_groups if g.name == "Willingness to Pay")
    b1 = next(c for c in group_b.criteria if c.criterion_id == "B1")
    assert b1.raw_score == 2  # capped from 5 down to the staleness ceiling


def test_gate_3_and_gate_2_together_most_restrictive_park_wins():
    interpretations = _all_fives_interpretations(OwnershipControl.WH)
    interpretations["C1"] = _interp("C1", "information_gatherer_only_or_nobody_identified")

    result = scorer.full_score(
        interpretations=interpretations,
        control=OwnershipControl.WH,
        ownership_disclosure="Listed",
        stage="Qualified Prospect",
        crm=_crm("Qualified Prospect"),
        entity_resolved=True,
        entity_note="",
        problem_established=True,
        active_competitor_pursuit=False,
        client_lost_unexplained_count=2,
        now=NOW,
    )

    assert result.recommendation == "Park"  # Gate 3 (Park) beats Gate 2 (Nurture)


def _p3_interp(label: str, *prior_career_claims: str) -> CriterionInterpretation:
    from api.icp.models import SupportingFact

    return CriterionInterpretation(
        criterion_id="P3",
        condition_label=label,
        evidence_label_overall=EvidenceLabel.FACT,
        rationale="test fixture",
        supporting_facts=[
            SupportingFact(claim=claim, label=EvidenceLabel.FACT) for claim in prior_career_claims
        ],
    )


def test_score_practus_lens_applies_p3_prior_career_uplift():
    """icp-skill.md P3: "Prior-career uplift: +1, capped at 5, applied
    once." Confirmed by an earlier audit to have ZERO test coverage at any
    level despite the logic existing in scorer.py."""
    interpretations = _all_fives_interpretations(OwnershipControl.PF)
    interpretations["P3"] = _p3_interp(
        "tl_full_match_or_ep_el_adjacency",  # base score 3
        "Led this exact transformation at a peer company, prior to Practus.",
    )

    scores, _, _ = scorer.score_practus_lens(interpretations)

    p3 = next(s for s in scores if s.criterion_id == "P3")
    assert p3.raw_score == 4  # 3 + 1 uplift


def test_score_practus_lens_caps_p3_prior_career_uplift_at_five():
    interpretations = _all_fives_interpretations(OwnershipControl.PF)
    interpretations["P3"] = _p3_interp(
        "two_plus_ep_el_full_matches_one_active",  # base score already 5
        "The fielded EL led this at the target company, prior to Practus.",
    )

    scores, _, _ = scorer.score_practus_lens(interpretations)

    p3 = next(s for s in scores if s.criterion_id == "P3")
    assert p3.raw_score == 5  # capped, never 6


def test_score_practus_lens_applies_p3_uplift_once_even_with_two_prior_career_matches():
    """Scenario 30: "Two EP/ELs both have prior relevance -> uplift applies
    once."""
    interpretations = _all_fives_interpretations(OwnershipControl.PF)
    interpretations["P3"] = _p3_interp(
        "tl_full_match_or_ep_el_adjacency",  # base score 3
        "The EP led a similar mandate at a competitor, prior to Practus.",
        "The EL separately worked this exact account, prior to Practus.",
    )

    scores, _, _ = scorer.score_practus_lens(interpretations)

    p3 = next(s for s in scores if s.criterion_id == "P3")
    assert p3.raw_score == 4  # +1 once, not +2 for two matching facts


def test_score_practus_lens_does_not_uplift_without_a_prior_career_supporting_fact():
    interpretations = _all_fives_interpretations(OwnershipControl.PF)
    interpretations["P3"] = _p3_interp("tl_full_match_or_ep_el_adjacency")  # base score 3, no prior-career claim

    scores, _, _ = scorer.score_practus_lens(interpretations)

    p3 = next(s for s in scores if s.criterion_id == "P3")
    assert p3.raw_score == 3

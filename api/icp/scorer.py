# Copied from icp-bot/backend/domains/mahak/people/mahak/icp/scorer.py (read-only reference repo).
# Only imports and plumbing are adapted for Rasa Incanta; the logic is the ICP bot's.
"""Ties condition_labels (from the LLM, or rule-based for C2/gates) into
the final ScoreResult. This is the one place that does arithmetic — every
number here traces back to a closed-enum label via criteria_tables.py, a
weight, and a table lookup. No LLM output is ever treated as a number.
"""

from __future__ import annotations

from datetime import datetime

from . import criteria_tables as ct
from . import gates as gate_fns
from . import rule_precompute as rp
from . import verdicts as vd
from .models import (
    CriterionInterpretation,
    CriterionScore,
    CrmStructured,
    EvidenceLabel,
    GroupScore,
    OwnershipControl,
    ScoreResult,
)


def _resolve_raw_score(criterion_id: str, label: str, *, control: OwnershipControl | None = None) -> int:
    if criterion_id == "A2":
        if label in ct.A2_MODE_A_BANDS:
            return ct.A2_MODE_A_BANDS[label]
        return ct.A2_MODE_B_BANDS[label]
    if criterion_id == "B2":
        assert control is not None, "B2 requires a control type to resolve its band table"
        return ct.B2_BANDS_BY_CONTROL[control][label]
    return ct.FLAT_CRITERION_BANDS[criterion_id][label]


def _build_criterion_score(
    criterion_id: str,
    weight: float,
    interp: CriterionInterpretation | None,
    *,
    control: OwnershipControl | None = None,
    is_na: bool = False,
    silent_default: int | None = None,
) -> CriterionScore:
    if is_na:
        return CriterionScore(
            criterion_id=criterion_id,
            raw_score=None,
            is_na=True,
            weight=weight,
            weighted_contribution=0.0,
            condition_label=None,
            rationale="N/A for this control/ownership type — group renormalised.",
            evidence_label=EvidenceLabel.DATA_GAP,
            data_gap=False,
        )

    if interp is None or not interp.condition_label:
        score = silent_default if silent_default is not None else 2
        return CriterionScore(
            criterion_id=criterion_id,
            raw_score=score,
            weight=weight,
            weighted_contribution=weight * score,
            condition_label=None,
            rationale="No evidence found — silent default applied.",
            evidence_label=EvidenceLabel.DATA_GAP,
            data_gap=True,
        )

    raw = _resolve_raw_score(criterion_id, interp.condition_label, control=control)
    return CriterionScore(
        criterion_id=criterion_id,
        raw_score=raw,
        weight=weight,
        weighted_contribution=weight * raw,
        condition_label=interp.condition_label,
        rationale=interp.rationale,
        evidence_label=interp.evidence_label_overall,
        data_gap=interp.data_gap,
    )


def _finalize_group(name: str, scores: list[CriterionScore]) -> GroupScore:
    """Renormalises weights across any N/A rows so the group's max stays
    the same (icp-skill.md line 195: 'mark the row, renormalise Group A,
    rescale so group max stays 15')."""
    max_points = ct.GROUP_MAX_POINTS[name]
    active = [s for s in scores if not s.is_na]
    na = [s for s in scores if s.is_na]

    renormalized = bool(na) and bool(active)
    if renormalized:
        original_weight_sum = sum(ct.WEIGHTS[s.criterion_id] for s in scores)
        active_weight_sum = sum(ct.WEIGHTS[s.criterion_id] for s in active)
        scale = original_weight_sum / active_weight_sum if active_weight_sum else 0.0
        rescaled: list[CriterionScore] = []
        for s in active:
            new_weight = ct.WEIGHTS[s.criterion_id] * scale
            rescaled.append(s.model_copy(update={"weight": new_weight, "weighted_contribution": new_weight * (s.raw_score or 0)}))
        active = rescaled

    subtotal = vd.round_half_up(sum(s.weighted_contribution for s in active))
    all_scores = active + na
    return GroupScore(
        name=name,
        criteria=all_scores,
        subtotal=subtotal,
        max_points=max_points,
        sub_verdict=vd.group_sub_verdict(name, subtotal),
        renormalized=renormalized,
    )


def score_client_lens(
    interpretations: dict[str, CriterionInterpretation],
    *,
    control: OwnershipControl,
    ownership_disclosure: str | None,
    stage: str | None,
    crm: CrmStructured,
    now: datetime,
    a1_si_cash_uplift: bool = False,
    a3_is_na: bool | None = None,
) -> tuple[list[GroupScore], int, str]:
    a3_na = a3_is_na if a3_is_na is not None else (control in ct.A3_NA_CONTROL_TYPES)

    a1 = _build_criterion_score("A1", ct.WEIGHTS["A1"], interpretations.get("A1"), silent_default=ct.A1_SILENT_DEFAULT)
    if a1_si_cash_uplift and control == OwnershipControl.SI and a1.raw_score is not None:
        bumped = min(5, a1.raw_score + 1)
        a1 = a1.model_copy(update={"raw_score": bumped, "weighted_contribution": a1.weight * bumped})

    a2 = _build_criterion_score("A2", ct.WEIGHTS["A2"], interpretations.get("A2"), silent_default=ct.A2_SILENT_DEFAULT)
    a3 = _build_criterion_score("A3", ct.WEIGHTS["A3"], interpretations.get("A3"), is_na=a3_na, silent_default=ct.A3_NO_PEERS_DEFAULT)
    group_a = _finalize_group("Ability to Pay", [a1, a2, a3])

    b1 = _build_criterion_score("B1", ct.WEIGHTS["B1"], interpretations.get("B1"), silent_default=ct.B1_SILENT_DEFAULT)
    if rp.is_stale(crm, now) and b1.raw_score is not None and b1.raw_score > ct.B1_STALENESS_CAP_SCORE:
        capped = ct.B1_STALENESS_CAP_SCORE
        b1 = b1.model_copy(update={"raw_score": capped, "weighted_contribution": b1.weight * capped, "data_gap": b1.data_gap})

    b2 = _build_criterion_score("B2", ct.WEIGHTS["B2"], interpretations.get("B2"), control=control)

    b3 = _build_criterion_score("B3", ct.WEIGHTS["B3"], interpretations.get("B3"))
    if control == OwnershipControl.PF and ownership_disclosure == "Unlisted" and b3.raw_score is not None:
        floored = max(b3.raw_score, ct.B3_PF_UNLISTED_FLOOR)
        if floored != b3.raw_score:
            b3 = b3.model_copy(update={"raw_score": floored, "weighted_contribution": b3.weight * floored})
    group_b = _finalize_group("Willingness to Pay", [b1, b2, b3])

    c1 = _build_criterion_score("C1", ct.WEIGHTS["C1"], interpretations.get("C1"))
    c2 = _build_criterion_score("C2", ct.WEIGHTS["C2"], interpretations.get("C2"))
    c3 = _build_criterion_score("C3", ct.WEIGHTS["C3"], interpretations.get("C3"), silent_default=ct.C3_SILENT_DEFAULT)
    group_c = _finalize_group("Access", [c1, c2, c3])

    groups = [group_a, group_b, group_c]
    client_total = sum(g.subtotal for g in groups)
    client_verdict = vd.client_lens_verdict(client_total)
    return groups, client_total, client_verdict


def score_practus_lens(interpretations: dict[str, CriterionInterpretation]) -> tuple[list[CriterionScore], int, str]:
    scores = [
        _build_criterion_score(cid, ct.WEIGHTS[cid], interpretations.get(cid))
        for cid in ct.PRACTUS_CRITERIA
    ]

    # P3 prior-career uplift, capped at 5, applied once (icp-skill.md line 389).
    p3 = next(s for s in scores if s.criterion_id == "P3")
    interp = interpretations.get("P3")
    if interp and any("prior to practus" in f.claim.lower() for f in interp.supporting_facts) and p3.raw_score is not None:
        bumped = min(5, p3.raw_score + ct.P3_PRIOR_CAREER_UPLIFT)
        idx = scores.index(p3)
        scores[idx] = p3.model_copy(update={"raw_score": bumped, "weighted_contribution": p3.weight * bumped})

    total = vd.round_half_up(sum(s.weighted_contribution for s in scores))
    return scores, total, vd.practus_lens_verdict(total)


def full_score(
    *,
    interpretations: dict[str, CriterionInterpretation],
    control: OwnershipControl,
    ownership_disclosure: str | None,
    stage: str | None,
    crm: CrmStructured,
    entity_resolved: bool,
    entity_note: str,
    problem_established: bool,
    active_competitor_pursuit: bool,
    competitor_conflict_detail: str = "",
    client_lost_unexplained_count: int,
    now: datetime | None = None,
    ticket_to_revenue_pct: float | None = None,
    a1_si_cash_uplift: bool = False,
) -> ScoreResult:
    now = now or datetime.utcnow()

    client_groups, client_total, client_verdict = score_client_lens(
        interpretations,
        control=control,
        ownership_disclosure=ownership_disclosure,
        stage=stage,
        crm=crm,
        now=now,
        a1_si_cash_uplift=a1_si_cash_uplift,
    )
    practus_scores, practus_total, practus_verdict = score_practus_lens(interpretations)

    c1_score = next((s.raw_score for g in client_groups for s in g.criteria if s.criterion_id == "C1"), None)

    gate_list = [
        gate_fns.gate_1_entity(entity_resolved, entity_note),
        gate_fns.gate_2_authority(c1_score),
        gate_fns.gate_3_repeat_loss(client_lost_unexplained_count),
        gate_fns.gate_4_conflict(active_competitor_pursuit, competitor_conflict_detail),
        gate_fns.gate_5_staleness(rp.is_stale(crm, now)),
        gate_fns.gate_6_problem_unknown(problem_established),
        gate_fns.gate_7_evidence_floor(
            sum(1 for g in client_groups for s in g.criteria if s.data_gap)
        ),
    ]

    base_recommendation = vd.recommendation_matrix_cell(client_verdict, practus_verdict)
    recommendation, gate_reason = gate_fns.apply_gate_effects(base_recommendation, gate_list)

    provisional = next(g for g in gate_list if g.gate_id == "gate_7").fired
    scale_flag = rp.scale_mismatch_flag(stage=stage, ticket_to_revenue_pct=ticket_to_revenue_pct, c1_score=c1_score)

    return ScoreResult(
        client_groups=client_groups,
        practus_criteria=practus_scores,
        client_total=client_total,
        client_verdict=client_verdict,
        practus_total=practus_total,
        practus_verdict=practus_verdict,
        gates=gate_list,
        recommendation=recommendation,
        recommendation_reason=gate_reason,
        provisional=provisional,
        scale_mismatch_flag=scale_flag,
    )

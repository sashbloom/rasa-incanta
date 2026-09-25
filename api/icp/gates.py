# Copied from icp-bot/backend/domains/mahak/people/mahak/icp/gates.py (read-only reference repo).
# Only imports and plumbing are adapted for Rasa Incanta; the logic is the ICP bot's.
"""The 7 gates (icp-skill.md GATES section, lines 424-436) and their
precedence rule ("most restrictive wins", scenario #49).

Gates 2/3/6 are pure rule-based checks over already-computed values (a
score, a CRM count, a boolean the LLM already resolved) — see the plan's
per-criterion classification. Gate 1 and Gate 4 need the LLM's judgment
(is the entity resolved; is that other pursuit a direct competitor) but the
gate *firing condition itself*, once that boolean exists, is a rule.
"""

from __future__ import annotations

from .models import GateResult

RECOMMENDATION_RANK = {
    "Pursue aggressively": 0,
    "Pursue selectively": 1,
    "Nurture": 2,
    "Park": 3,
}


def most_restrictive(a: str, b: str) -> str:
    return a if RECOMMENDATION_RANK[a] >= RECOMMENDATION_RANK[b] else b


def gate_1_entity(entity_resolved: bool, note: str) -> GateResult:
    return GateResult(
        gate_id="gate_1",
        name="Entity",
        fired=not entity_resolved,
        detail=note or "Contracting entity not identifiable.",
    )


def gate_2_authority(c1_score: int | None) -> GateResult:
    fired = c1_score == 1
    return GateResult(
        gate_id="gate_2",
        name="Authority",
        fired=fired,
        detail="Only an information-gatherer identified, or nobody at all — caps at Nurture." if fired else "",
    )


def gate_3_repeat_loss(unexplained_loss_count: int) -> GateResult:
    """icp-skill.md line 430 / scenario 16: "2+ `Client Lost` with no
    captured reason" is counted per-loss (how many losses individually lack
    a reason), not all-or-nothing across the whole history -- 3 losses where
    only 1 has a reason recorded still fires, because 2 don't. Scenario 17:
    a single prior loss, or losses older than 24 months, discounts instead
    and does NOT fire this gate. Both the "no reason" filter and the
    24-month lookback already happened upstream in
    practus_history.classify() -- `unexplained_loss_count` is that
    pre-filtered count, so this function only thresholds it."""
    fired = unexplained_loss_count >= 2
    return GateResult(
        gate_id="gate_3",
        name="Repeat loss",
        fired=fired,
        detail=(
            f"{unexplained_loss_count} prior pursuits closed lost within the last 24 months with no "
            "reason recorded — that must be resolved before a third attempt."
        )
        if fired
        else "",
    )


def gate_4_conflict(active_competitor_pursuit: bool, detail: str = "") -> GateResult:
    return GateResult(
        gate_id="gate_4",
        name="Conflict",
        fired=active_competitor_pursuit,
        detail=detail or ("An active Practus pursuit exists at a direct competitor." if active_competitor_pursuit else ""),
    )


def gate_5_staleness(is_stale: bool) -> GateResult:
    return GateResult(
        gate_id="gate_5",
        name="Staleness",
        fired=is_stale,
        detail="No stage movement in 180+ days — a reopen decision is forced." if is_stale else "",
    )


def gate_6_problem_unknown(problem_established: bool) -> GateResult:
    fired = not problem_established
    return GateResult(
        gate_id="gate_6",
        name="Problem unknown",
        fired=fired,
        detail=(
            "The client's problem has not been established — this is a discovery call, not a pitch."
            if fired
            else ""
        ),
    )


def gate_7_evidence_floor(data_gap_count_of_9_client_criteria: int) -> GateResult:
    fired = data_gap_count_of_9_client_criteria >= 4
    return GateResult(
        gate_id="gate_7",
        name="Evidence floor",
        fired=fired,
        detail=(
            f"{data_gap_count_of_9_client_criteria} of 9 client criteria are data gaps — "
            "reported as Provisional, band only."
            if fired
            else ""
        ),
    )


def apply_gate_effects(recommendation: str, gates: list[GateResult]) -> tuple[str, str]:
    """Applies the cap effects of gates 2/3/6 (the gates that override the
    recommendation-matrix cell outright). Gates 1/4/5/7 are surfaced but do
    not themselves recompute the recommendation here — Gate 1 stops the run
    upstream, Gate 4 forces a human go/no-go rather than auto-capping, Gate
    5's effect already landed inside B1's score, and Gate 7 only changes
    presentation (Provisional), not the verdict cell."""
    result = recommendation
    reasons: list[str] = []
    by_id = {g.gate_id: g for g in gates}

    if by_id["gate_3"].fired:
        result = most_restrictive(result, "Park")
        reasons.append(by_id["gate_3"].detail)
    if by_id["gate_2"].fired:
        result = most_restrictive(result, "Nurture")
        reasons.append(by_id["gate_2"].detail)
    if by_id["gate_6"].fired:
        result = most_restrictive(result, "Pursue selectively")
        reasons.append(by_id["gate_6"].detail)

    return result, " ".join(reasons)

"""One company's ICP scoring, adapted from the ICP bot's `orchestrator.run()` (steps 1 to 8).

Kept exactly: evidence assembly (entity resolution, CRM, ownership and secondary research via Exa,
Practus history, Setu P2/P3, Read.ai meetings, Outlook mail), the Gate 1 stop, the A2 Mode A
ticket-to-revenue ratio computed before interpretation, the single criteria-interpretation call,
C2 warmth by rule, the Gate 4 competitor check started early in the background, the Gate 6
problem-established rule, and `full_score()`.

Left out, because Rasa Incanta does not produce ICP reports: the per-run JSON files, the progress
file, the persona deep-dive (it needs a named contact, which the Zoho mirror does not have), QC
checks on the rendered report, the narrative and the HTML/Markdown/DOCX/PDF renderers.
"""
from __future__ import annotations

import logging
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import conflict_checker, criteria_tables as ct, evidence_assembler, tracing
from . import rule_precompute as rp
from .evidence_assembler import problem_statement_text
from .llm_interpreter import interpret
from .models import CriterionInterpretation, EvidenceBundle, OwnershipControl, ScoreResult, UnstructuredEvidenceItem
from .scorer import full_score

logger = logging.getLogger(__name__)


@dataclass
class CompanyScore:
    company_name: str
    status: str  # scored | gate_1_stopped
    bundle: EvidenceBundle
    interpretations: dict[str, CriterionInterpretation] = field(default_factory=dict)
    score: ScoreResult | None = None
    stop_reason: str | None = None


def score_company(company_name: str, *, generated_at: datetime | None = None) -> CompanyScore:
    """Score one company. Raises only if the pipeline itself breaks (e.g. no Anthropic key);
    every individual source failure is already a data gap inside the bundle."""
    generated_at = generated_at or datetime.now(timezone.utc)
    tracing.current_run_id.set(f"rasa-incanta:{generated_at:%Y%m%d}:{company_name}")

    competitor_pool = ThreadPoolExecutor(max_workers=1)
    competitor_future: Future | None = None

    def kick_off_competitor_check(industry_hint: str | None) -> None:
        nonlocal competitor_future
        competitor_future = competitor_pool.submit(
            tracing.propagate_context(conflict_checker.identify_competitors), company_name, industry_hint)

    try:
        bundle = evidence_assembler.assemble(company_name, now=generated_at, on_industry_hint=kick_off_competitor_check)

        if not bundle.entity.entity_resolved:  # Gate 1: stop before any interpretation or scoring
            reason = bundle.entity.contracting_entity_note or (
                "Contracting entity could not be identified after the full four-step lookup "
                "(icp-skill.md §0.1). Insufficient basis to score. Confirm the legal name or website.")
            return CompanyScore(company_name, "gate_1_stopped", bundle, stop_reason=reason)

        control = bundle.entity.ownership_control or OwnershipControl.WH

        # A2 Mode A ratio before interpretation, so the model sees the number (icp-skill.md §0.4).
        ratios = {"ticket_to_revenue_pct": None, "ticket_to_ebitda_pct": None}
        if rp.a2_mode(bundle.crm.stage) == "A":
            ratios = rp.ticket_ratios(bundle.crm.amount_context_only, bundle.secondary_financials.revenue_cr,
                                      bundle.secondary_financials.ebitda_cr)
            if ratios["ticket_to_revenue_pct"] is not None:
                amount_cr = bundle.crm.amount_context_only / rp.RUPEES_PER_CRORE
                bundle.unstructured.append(UnstructuredEvidenceItem(
                    text=f"Ticket-to-revenue ratio (A2 Mode A ONLY — do not use for A1 Scale or A3 "
                         f"Financial trajectory, and do not restate this as a headline stat, red flag, or "
                         f"CTA anywhere else in the report — it is A2-scoring input only): "
                         f"{ratios['ticket_to_revenue_pct']}% "
                         f"(ticket ₹{amount_cr:.4f} Cr vs revenue ₹{bundle.secondary_financials.revenue_cr} Cr, "
                         f"source: {bundle.secondary_financials.source or 'secondary research'}).",
                    source="Computed", source_type="secondary_research", criterion_tags=["A2"]))

        interpretations = interpret(bundle, control)
        interpretations["C2"] = rp.c2_warmth_interpretation(bundle.crm, bundle.unstructured,
                                                            generated_at.replace(tzinfo=None))

        try:  # Gate 4 only flags for a human decision, so a failed check degrades to "no conflict"
            competitors = competitor_future.result() if competitor_future is not None else None
            conflict = conflict_checker.check(company_name, industry_hint=bundle.crm.industry_type, competitors=competitors)
        except Exception as exc:
            logger.warning("Gate 4 competitor-conflict check failed for %r: %s", company_name, exc)
            conflict = conflict_checker.ConflictResult(active_competitor_pursuit=False)
            bundle.data_gaps.append(f"Gate 4 competitor-conflict check failed — treated as no conflict found. ({type(exc).__name__})")

        # Gate 6: a real P2 interpretation is authoritative; raw CRM text only when P2 never ran.
        p2 = interpretations.get("P2")
        if p2 is not None:
            problem_established = not p2.data_gap and p2.condition_label != ct.P2_GATE6_LABEL
        else:
            problem_established = bool(problem_statement_text(bundle.crm))

        a1_si_cash_uplift = (control == OwnershipControl.SI and bundle.secondary_financials.post_money_cash_cr is not None
                             and bundle.secondary_financials.post_money_cash_cr > 200)

        score = full_score(
            interpretations=interpretations, control=control, ownership_disclosure=bundle.entity.ownership_disclosure,
            stage=bundle.crm.stage, crm=bundle.crm, entity_resolved=bundle.entity.entity_resolved,
            entity_note=bundle.entity.contracting_entity_note or "", problem_established=problem_established,
            active_competitor_pursuit=conflict.active_competitor_pursuit, competitor_conflict_detail=conflict.detail,
            client_lost_unexplained_count=bundle.practus_history.client_lost_unexplained_count,
            now=generated_at.replace(tzinfo=None), ticket_to_revenue_pct=ratios["ticket_to_revenue_pct"],
            a1_si_cash_uplift=a1_si_cash_uplift,
        )
        logger.info("ICP %r: client %d/50 (%s), Practus %d/50 (%s), %s%s", company_name, score.client_total,
                    score.client_verdict, score.practus_total, score.practus_verdict, score.recommendation,
                    " [provisional]" if score.provisional else "")
        return CompanyScore(company_name, "scored", bundle, interpretations, score)
    finally:
        competitor_pool.shutdown(wait=False)

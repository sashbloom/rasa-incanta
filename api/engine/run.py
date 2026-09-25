"""One pass over the pipeline: pull deals, keep their history, build context cards, draft NBAs.

History is append-only. Every run adds its own snapshots, cards and recommendations; the
`deals` table alone holds the latest state. A rerun in the same week regenerates only
recommendations nobody has decided on yet: a deal with a decision this week is left alone.

`nba_limit` sets how many deals get an NBA (most advanced and best-documented first); None
means every deal, which is what POST /api/run does. Each deal gets one NBA for now; Brick 4
widens this to 3-4 per deal, per user.
"""
from __future__ import annotations

import copy
import logging
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

import anthropic
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.config import Settings
from api.domain.stages import board_for, stage_position
from api.domain.weeks import local_today, week_start
from api.engine.context import CardContent, build_card
from api.engine.nba import generate_nba
from api.models import ContextCard, Deal, DealSnapshot, Decision, Recommendation, Run
from api.sources.setu import SetuResult, fetch_case_studies
from api.sources.zoho import ZohoDeal, ZohoResult, fetch_deals, jsonable

logger = logging.getLogger(__name__)


def upsert_deals(session: Session, deals: list[ZohoDeal], now: datetime) -> dict[str, Deal]:
    """Bring `deals` up to date with this pull; deals missing from it are marked inactive."""
    existing = {d.zoho_id: d for d in session.scalars(select(Deal))}
    seen: dict[str, Deal] = {}
    for z in deals:
        deal = existing.get(z.zoho_id) or Deal(zoho_id=z.zoho_id)
        board = board_for(z.stage)
        deal.name = z.name
        deal.account_name = z.account_name
        deal.contact_name = z.contact_name
        deal.owner_name = z.owner_name
        deal.stage = z.stage
        deal.board = board.value if board else None
        deal.sbu = z.sbu
        deal.industry = z.industry
        deal.city_state = z.city_state
        deal.lead_source = z.lead_source
        deal.ep_involved = z.ep_involved
        deal.el_involved = z.el_involved
        deal.stage_changed_at = z.stage_entered_at
        deal.zoho_modified_at = z.modified_at
        deal.is_active = True
        deal.last_seen_at = now
        deal.raw = z.raw
        session.add(deal)
        seen[z.zoho_id] = deal
    for zoho_id, deal in existing.items():
        if zoho_id not in seen and deal.is_active:
            deal.is_active = False
    session.flush()
    return seen


def pick_deals(deals: list[ZohoDeal], cards: dict[str, CardContent], limit: int | None) -> list[ZohoDeal]:
    """Most advanced stage first, then the best-documented, then the most recently touched."""
    def key(z: ZohoDeal):
        touched = z.modified_at.timestamp() if z.modified_at else 0
        return (stage_position(z.stage) or 0, len(cards[z.zoho_id].facts()), touched)

    return sorted(deals, key=key, reverse=True)[:limit]


def decided_this_week(session: Session, deal: Deal, week: Any) -> bool:
    stmt = (
        select(Decision.id)
        .join(Recommendation, Recommendation.id == Decision.recommendation_id)
        .join(Run, Run.id == Recommendation.run_id)
        .where(Recommendation.deal_id == deal.id, Run.week_start == week)
        .limit(1)
    )
    return session.scalar(stmt) is not None


def default_llm_client(settings: Settings) -> Any | None:
    return anthropic.Anthropic(api_key=settings.anthropic_api_key) if settings.anthropic_api_key else None


def run_week(
    session: Session,
    settings: Settings,
    *,
    now: datetime | None = None,
    kind: str = "manual",
    fetch: Callable[[Settings], ZohoResult] = fetch_deals,
    fetch_setu: Callable[[Settings], SetuResult] = fetch_case_studies,
    llm_client: Any | None = None,
    deal_zoho_id: str | None = None,
    nba_limit: int | None = 1,
    run: Run | None = None,
) -> Run:
    """Run the pass. Pass `run` (already saved, status "running") to fill in a run the caller
    created, e.g. so an API can return its id before the work finishes."""
    now = now or datetime.now(timezone.utc)
    week = week_start(now, settings.timezone)
    today = local_today(now, settings.timezone)
    if run is None:
        run = Run(week_start=week, kind=kind, status="running", started_at=now, stats={})
        session.add(run)
        session.commit()

    stats: dict[str, Any] = {"sources": {}, "nba_created": 0, "nba_kept": 0, "nba_skipped": []}
    zoho = fetch(settings)
    stats["sources"]["zoho"] = "ok" if zoho.ok else zoho.error
    if zoho.ok:
        stats["sources"]["zoho_outreach"] = zoho.reachout_error or "ok"
    if not zoho.ok:
        run.status, run.error, run.stats, run.finished_at = "failed", zoho.error, stats, datetime.now(timezone.utc)
        session.commit()
        return run

    setu = fetch_setu(settings)  # once per run; a Setu failure only costs the capability signal
    stats["sources"]["setu"] = "ok" if setu.ok else setu.error
    case_studies = setu.case_studies if setu.ok else None

    deals = upsert_deals(session, zoho.deals, now)
    cards: dict[str, CardContent] = {}
    for z in zoho.deals:
        deal = deals[z.zoho_id]
        card = build_card(z, today, settings.timezone, zoho.reachouts.get(z.zoho_id, []), case_studies)
        cards[z.zoho_id] = card
        snapshot = {k: v for k, v in asdict(z).items() if k != "raw"}
        session.add(DealSnapshot(run_id=run.id, deal_id=deal.id, stage=deal.stage, board=deal.board,
                                 data=jsonable(snapshot)))
        session.add(ContextCard(run_id=run.id, deal_id=deal.id, account_fit=card.account_fit,
                                stakeholder=card.stakeholder, conversation=card.conversation,
                                capability=card.capability, deal_state=card.deal_state, gaps=card.gaps))
    stats["deals"] = len(zoho.deals)
    session.flush()

    if deal_zoho_id:
        chosen = [z for z in zoho.deals if z.zoho_id == deal_zoho_id]
        if not chosen:
            stats["nba_skipped"].append({"deal": deal_zoho_id, "reason": "Deal not found among in-scope deals."})
    else:
        chosen = pick_deals(zoho.deals, cards, nba_limit)
    stats["nba_to_draft"] = len(chosen)
    stats["nba_done"] = 0  # deals handled so far, whatever the outcome; drives the progress bar
    run.stats = copy.deepcopy(stats)  # a snapshot: sharing lists with stats would hide later changes
    session.commit()  # the cards and snapshots are saved before the slow part starts

    client = llm_client if llm_client is not None else default_llm_client(settings)

    def draft_for(z: ZohoDeal) -> None:
        deal = deals[z.zoho_id]
        if decided_this_week(session, deal, week):
            stats["nba_kept"] += 1
            return
        if client is None:
            stats["nba_skipped"].append({"deal": z.name, "reason": "ANTHROPIC_API_KEY is not set."})
            return
        result = generate_nba(client, settings.llm_model_actions, z.name, cards[z.zoho_id])
        if not result.ok:
            stats["nba_skipped"].append({"deal": z.name, "reason": result.error, "problems": result.problems})
            return
        draft = result.draft
        session.add(Recommendation(
            run_id=run.id, deal_id=deal.id, rank=1, objective=draft.objective, action=draft.action.strip(),
            why_now=draft.why_now.strip(), evidence=result.evidence, effort=draft.effort,
            gaps=cards[z.zoho_id].gaps, model=result.model,
        ))
        stats["nba_created"] += 1

    for z in chosen:
        draft_for(z)
        stats["nba_done"] += 1
        run.stats = copy.deepcopy(stats)
        session.commit()  # progress is visible while the run is still going, skips included

    run.status = "partial" if stats["nba_skipped"] else "succeeded"
    run.stats = copy.deepcopy(stats)
    run.finished_at = datetime.now(timezone.utc)
    session.commit()
    logger.info("Run %s finished: %s", run.id, run.status)
    return run

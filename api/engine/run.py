"""One pass over the pipeline: pull every signal, keep the history, build context cards, draft NBAs.

History is append-only. Every run adds its own snapshots, cards and recommendations; the
`deals` table alone holds the latest state. A rerun in the same week regenerates only
recommendations nobody has decided on yet: a deal with a decision this week is left alone.

Phases (`stats.phase`, with done/total counts for the board's progress bar):
  pull        Zoho deals and outreach log, Setu case studies, recent Read.ai meetings
  mail        Outlook mail per deal (delegated Graph), then key points per deal with mail
  capability  the ICP bot's P2 case-study re-rank (one Claude call per deal) and SMEs
  icp         the ICP bot's scoring for companies without a fresh cached result
  persona     Exa research on the person in each deal's outreach log, cached 8 weeks per contact
  cards       context cards and snapshots
  nba         one NBA per deal (`nba_limit`; None means every deal, as POST /api/run does). The first
              draft runs alone so its cached prompt prefix is written once and read by the rest.
Slow network work runs in thread pools; database writes stay on this thread. One failing
source never fails the run: it is recorded under `stats.sources` and becomes gap flags.
Without an Anthropic key the re-rank, SMEs and ICP scoring are skipped (and said so); persona
research also needs EXA_API_KEY.

Every Claude and Exa call is traced in Langfuse under one session per run ("rasa-incanta:run:<id>";
ICP scoring keeps its own session per company). Pool submits go through tracing.propagate_context,
because contextvars do not cross into ThreadPoolExecutor workers on their own.
"""
from __future__ import annotations

import copy
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

import anthropic
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.config import Settings
from api.domain.stages import board_for, stage_position
from api.domain.weeks import local_today, week_start
from api.engine import icp_signal, persona_signal
from api.engine.capability import Capability, capability_for, sme_text
from api.engine.context import CardContent, build_card
from api.engine.linking import deal_identity, mail_belongs, meeting_belongs
from api.engine.mail import key_points
from api.engine.nba import generate_nba
from api.icp import tracing
from api.models import ContextCard, Deal, DealSnapshot, Decision, Recommendation, Run
from api.persona.generator import NoSourceMaterial
from api.sources import icp_shared, outlook, readai
from api.sources.setu import SetuResult, fetch_case_studies
from api.sources.zoho import ZohoDeal, ZohoResult, fetch_deals, jsonable

logger = logging.getLogger(__name__)

WORKERS = 6  # parallel Claude / Graph calls per phase


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


def _default_fetch_mail(session: Session, settings: Settings, deals, belongs) -> outlook.MailResult:
    return outlook.fetch_mail(session, settings, deals, belongs)


def run_week(
    session: Session,
    settings: Settings,
    *,
    now: datetime | None = None,
    kind: str = "manual",
    fetch: Callable[[Settings], ZohoResult] = fetch_deals,
    fetch_setu: Callable[[Settings], SetuResult] = fetch_case_studies,
    fetch_mail: Callable | None = None,
    capability_fn: Callable[..., Capability] | None = None,
    score_company_fn: Callable | None = None,
    persona_fn: Callable | None = None,
    llm_client: Any | None = None,
    deal_zoho_id: str | None = None,
    nba_limit: int | None = 1,
    run: Run | None = None,
) -> Run:
    """Run the pass. Pass `run` (already saved, status "running") to fill in a run the caller
    created, e.g. so an API can return its id before the work finishes. The source functions are
    injectable for tests; by default they are the real sources."""
    now = now or datetime.now(timezone.utc)
    week = week_start(now, settings.timezone)
    today = local_today(now, settings.timezone)
    if run is None:
        run = Run(week_start=week, kind=kind, status="running", started_at=now, stats={})
        session.add(run)
        session.commit()

    tracing.current_run_id.set(f"rasa-incanta:run:{run.id}")  # the Langfuse session for this run
    stats: dict[str, Any] = {"phase": "pull", "sources": {}, "nba_created": 0, "nba_kept": 0, "nba_skipped": [],
                             "phases": {"pull": {"started_at": now.isoformat()}}}

    def save(**updates) -> None:
        if updates.get("phase") and updates["phase"] != stats.get("phase"):
            # when each phase started and ended, for the board's step list
            stamp = datetime.now(timezone.utc).isoformat()
            phases = stats.setdefault("phases", {})
            if stats.get("phase") in phases:
                phases[stats["phase"]]["finished_at"] = stamp
            phases[updates["phase"]] = {"started_at": stamp}
        stats.update(updates)
        run.stats = copy.deepcopy(stats)  # a snapshot: sharing lists with stats would hide later changes
        session.commit()

    client = llm_client if llm_client is not None else default_llm_client(settings)

    # ---------------------------------------------------------------- pull
    zoho = fetch(settings)
    stats["sources"]["zoho"] = "ok" if zoho.ok else zoho.error
    if not zoho.ok:
        run.status, run.error, run.finished_at = "failed", zoho.error, datetime.now(timezone.utc)
        save()
        return run
    stats["sources"]["zoho_outreach"] = zoho.reachout_error or "ok"

    pilot = settings.pilot_company_list
    if pilot:
        pilot_keys = {icp_signal.company_key(c) for c in pilot}
        total = len(zoho.deals)
        zoho.deals = [z for z in zoho.deals if icp_signal.company_key(z.account_name or z.name) in pilot_keys]
        stats["sources"]["zoho"] = f"ok (pilot: {len(zoho.deals)} of {total} deals, {len(pilot)} companies)"

    setu = fetch_setu(settings)  # once per run; a Setu failure only costs the capability signal
    stats["sources"]["setu"] = "ok" if setu.ok else setu.error
    case_studies = setu.case_studies if setu.ok else None

    try:
        meetings = readai.recent_meetings(session, now)
        stats["sources"]["readai"] = "ok" if meetings else "ok (no meetings received yet)"
    except Exception as exc:
        logger.exception("Reading stored Read.ai meetings failed")
        meetings, stats["sources"]["readai"] = [], f"Read.ai meetings could not be read ({type(exc).__name__})."

    deals = upsert_deals(session, zoho.deals, now)
    stats["deals"] = len(zoho.deals)
    identities = {z.zoho_id: deal_identity(z, zoho.reachouts.get(z.zoho_id, [])) for z in zoho.deals}
    deal_meetings = {zid: [m for m in meetings if meeting_belongs(ident, m)] for zid, ident in identities.items()}
    save(phase="mail")

    # ---------------------------------------------------------------- mail
    mail_fetcher = fetch_mail or _default_fetch_mail
    try:
        mail = mail_fetcher(session, settings, [(z.zoho_id, z.account_name or z.name, identities[z.zoho_id])
                                                for z in zoho.deals], mail_belongs)
    except Exception as exc:  # a dead Outlook sign-in must never stop the run: every card gets no_mail
        logger.exception("Outlook mail fetch failed")
        session.rollback()
        mail = outlook.MailResult(error=f"Outlook mail could not be read ({type(exc).__name__}).")
    stats["sources"]["outlook"] = "ok" if mail.ok else mail.error
    mail_points: dict[str, dict[int, list[str]]] = {}
    with_mail = [z for z in zoho.deals if mail.by_deal.get(z.zoho_id)]
    if with_mail and client is not None:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures = {pool.submit(tracing.propagate_context(key_points), client, settings.llm_model_extraction, z.name, mail.by_deal[z.zoho_id]): z
                       for z in with_mail}
            for future in as_completed(futures):
                mail_points[futures[future].zoho_id] = future.result()
    save(phase="capability", capability_done=0, capability_to_do=len(zoho.deals))

    # ---------------------------------------------------------------- capability
    capability_fn = capability_fn or (capability_for if settings.anthropic_api_key else None)
    capabilities: dict[str, Capability] = {}
    if capability_fn is not None and case_studies is not None:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures = {pool.submit(tracing.propagate_context(capability_fn), z, case_studies): z for z in zoho.deals}
            for future in as_completed(futures):
                z = futures[future]
                try:
                    capabilities[z.zoho_id] = future.result()
                except Exception:
                    logger.exception("Capability match failed for %s", z.name)  # falls back to the strict match
                stats["capability_done"] += 1
                save()
        reranked = sum(c.reranked for c in capabilities.values())
        stats["sources"]["setu_rerank"] = (f"ok ({reranked} of {len(zoho.deals)} deals re-ranked)" if reranked == len(zoho.deals)
                                           else f"{len(zoho.deals) - reranked} of {len(zoho.deals)} deals fell back to the strict match")
    elif case_studies is not None:
        stats["sources"]["setu_rerank"] = "skipped: ANTHROPIC_API_KEY is not set"

    # ---------------------------------------------------------------- icp
    icp_by_key: dict[str, tuple[dict, dict, str]] = {}
    companies: dict[str, str] = {}
    for z in zoho.deals:
        companies.setdefault(icp_signal.company_key(z.account_name or z.name), z.account_name or z.name)
    stale = []
    shared_reused, shared_error = 0, None
    for key, company in companies.items():
        require_exa = bool(settings.exa_api_key)
        row = icp_signal.fresh_icp(session, key, now, settings.icp_cache_days, require_exa=require_exa)
        if row is None and icp_shared.configured(settings):
            found = icp_shared.find(settings, key, now, settings.icp_cache_days, require_exa=require_exa)
            if not found.ok:
                shared_error = found.error  # unreachable: fall back to our own scoring for this company
            elif found.score is not None:
                row = icp_signal.adopt_shared(session, company, found.score)
                shared_reused += 1
        if row is not None:
            icp_by_key[key] = (row.account_fit, row.stakeholder, row.status)
        else:
            stale.append((key, company))
    session.commit()
    score_fn = score_company_fn
    if score_fn is None and settings.anthropic_api_key:
        from api.icp.pipeline import score_company
        score_fn = score_company
    save(phase="icp", icp_cached=len(icp_by_key), icp_to_score=len(stale) if score_fn else 0, icp_done=0, icp_failed=0)
    if score_fn is None:
        stats["sources"]["icp"] = "skipped: ANTHROPIC_API_KEY is not set" if stale else "ok (all cached)"
    elif stale:
        with ThreadPoolExecutor(max_workers=max(1, settings.icp_concurrency)) as pool:
            futures = {pool.submit(tracing.propagate_context(score_fn), company, generated_at=now): (key, company) for key, company in stale}
            for future in as_completed(futures):
                key, company = futures[future]
                try:
                    row = icp_signal.record(session, company, now, result=future.result(),
                                            exa_research=bool(settings.exa_api_key))
                    icp_by_key[key] = (row.account_fit, row.stakeholder, row.status)
                    if icp_shared.configured(settings):
                        why = icp_shared.publish(settings, key, icp_shared.SharedScore(
                            company_name=company, computed_at=now, status=row.status, account_fit=row.account_fit,
                            stakeholder=row.stakeholder, result=row.result, data_gaps=row.data_gaps))
                        if why:
                            shared_error = why
                        elif row.status in icp_shared.REUSABLE:
                            stats["icp_shared_published"] = stats.get("icp_shared_published", 0) + 1
                except Exception as exc:
                    logger.exception("ICP scoring failed for %s", company)
                    icp_signal.record(session, company, now, error=f"{type(exc).__name__}: {str(exc)[:300]}")
                    stats["icp_failed"] += 1
                stats["icp_done"] += 1
                save()
        scored = stats["icp_done"] - stats["icp_failed"]
        summary = f"{scored} scored, {stats['icp_cached']} cached"
        stats["sources"]["icp"] = (f"ok ({summary})" if not stats["icp_failed"]
                                   else f"{stats['icp_failed']} companies could not be scored ({summary})")
    else:
        stats["sources"]["icp"] = "ok (all cached)"

    if not icp_shared.configured(settings):
        stats["sources"]["icp_shared"] = "not configured (ICP_SHARED_DB_URL); using the local cache only"
    elif shared_error:
        stats["sources"]["icp_shared"] = (f"unreachable or failed ({shared_error}); used the local cache and our own "
                                          f"scoring ({shared_reused} reused, {stats.get('icp_shared_published', 0)} published)")
    else:
        stats["sources"]["icp_shared"] = f"ok ({shared_reused} reused, {stats.get('icp_shared_published', 0)} published)"
    stats["icp_shared_reused"] = shared_reused
    save()

    # ---------------------------------------------------------------- persona
    contacts: dict[str, persona_signal.Contact] = {}
    for z in zoho.deals:
        contact = persona_signal.primary_contact(zoho.reachouts.get(z.zoho_id, []), z.account_name or z.name)
        if contact is not None:
            contacts[z.zoho_id] = contact
    persona_by_key: dict[str, list[dict]] = {}
    to_research: dict[str, persona_signal.Contact] = {}
    for contact in contacts.values():
        if contact.key in persona_by_key or contact.key in to_research:
            continue
        row = persona_signal.fresh_persona(session, contact.key, now, settings.persona_cache_days)
        if row is not None:
            persona_by_key[contact.key] = row.facts
        else:
            to_research[contact.key] = contact
    research_fn = persona_fn
    if research_fn is None and settings.anthropic_api_key and settings.exa_api_key:
        from api.persona.generator import generate_persona_data
        research_fn = generate_persona_data
    save(phase="persona", persona_cached=len(persona_by_key), persona_to_do=len(to_research) if research_fn else 0,
         persona_done=0, persona_failed=0)
    if not contacts:
        stats["sources"]["persona"] = "ok (no named contact in any outreach log)"
    elif not to_research:
        stats["sources"]["persona"] = "ok (all cached)"
    elif research_fn is None:
        missing = "EXA_API_KEY" if not settings.exa_api_key else "ANTHROPIC_API_KEY"
        stats["sources"]["persona"] = f"skipped: {missing} is not set"
    else:
        def authority(contact: persona_signal.Contact) -> str | None:
            icp = icp_by_key.get(icp_signal.company_key(contact.company))
            facts = (icp[1] if icp else {}).get("facts", [])
            return next((f["value"] for f in facts if f.get("id") == "icp.authority"), None)

        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures = {pool.submit(tracing.propagate_context(research_fn), c.name, c.company, designation=c.designation,
                                   model=settings.llm_model_icp, known_context=authority(c)): c
                       for c in to_research.values()}
            for future in as_completed(futures):
                contact = futures[future]
                try:
                    row = persona_signal.record(session, contact, now, persona=future.result())
                except NoSourceMaterial:
                    row = persona_signal.record(session, contact, now, status="no_material")
                except Exception as exc:
                    logger.exception("Persona research failed for a contact at %s", contact.company)
                    row = persona_signal.record(session, contact, now, status="failed",
                                                error=f"{type(exc).__name__}: {str(exc)[:300]}")
                    stats["persona_failed"] += 1
                persona_by_key[contact.key] = row.facts
                stats["persona_done"] += 1
                save()
        researched = stats["persona_done"] - stats["persona_failed"]
        summary = f"{researched} researched, {stats['persona_cached']} cached"
        stats["sources"]["persona"] = (f"ok ({summary})" if not stats["persona_failed"]
                                       else f"{stats['persona_failed']} contacts could not be researched ({summary})")
    save(phase="cards")

    # ---------------------------------------------------------------- cards
    cards: dict[str, CardContent] = {}
    for z in zoho.deals:
        deal = deals[z.zoho_id]
        card = build_card(
            z, today, settings.timezone, zoho.reachouts.get(z.zoho_id, []), case_studies,
            meetings=deal_meetings.get(z.zoho_id, []), mails=mail.by_deal.get(z.zoho_id, []),
            mail_points=mail_points.get(z.zoho_id), capability=capabilities.get(z.zoho_id),
            icp=icp_by_key.get(icp_signal.company_key(z.account_name or z.name)),
            persona=((contacts[z.zoho_id].name, persona_by_key.get(contacts[z.zoho_id].key, []))
                     if z.zoho_id in contacts else None),
        )
        cards[z.zoho_id] = card
        snapshot = {k: v for k, v in asdict(z).items() if k != "raw"}
        session.add(DealSnapshot(run_id=run.id, deal_id=deal.id, stage=deal.stage, board=deal.board,
                                 data=jsonable(snapshot)))
        session.add(ContextCard(run_id=run.id, deal_id=deal.id, account_fit=card.account_fit,
                                stakeholder=card.stakeholder, conversation=card.conversation,
                                capability=card.capability, deal_state=card.deal_state, gaps=card.gaps))
    session.flush()

    # ---------------------------------------------------------------- nba
    if deal_zoho_id:
        chosen = [z for z in zoho.deals if z.zoho_id == deal_zoho_id]
        if not chosen:
            stats["nba_skipped"].append({"deal": deal_zoho_id, "reason": "Deal not found among in-scope deals."})
    else:
        chosen = pick_deals(zoho.deals, cards, nba_limit)
    save(phase="nba", nba_to_draft=len(chosen), nba_done=0)  # cards and snapshots are saved first

    to_draft = []
    for z in chosen:
        if decided_this_week(session, deals[z.zoho_id], week):
            stats["nba_kept"] += 1
            stats["nba_done"] += 1
        elif client is None:
            stats["nba_skipped"].append({"deal": z.name, "reason": "ANTHROPIC_API_KEY is not set."})
            stats["nba_done"] += 1
        else:
            to_draft.append(z)
    save()

    def keep(z: ZohoDeal, result) -> None:
        if result.ok:
            draft, card = result.draft, cards[z.zoho_id]
            case = (card.capability.get("case_studies") or [None])[0]
            session.add(Recommendation(
                run_id=run.id, deal_id=deals[z.zoho_id].id, rank=1, objective=draft.objective,
                action=draft.action.strip(), why_now=draft.why_now.strip(), evidence=result.evidence,
                effort=draft.effort, gaps=card.gaps, model=result.model, treasury_ref=draft.treasury_ref,
                proof={"name": case["name"], "why": case.get("why")} if case else {},
                sme=sme_text(card.capability.get("smes") or []),
            ))
            stats["nba_created"] += 1
        else:
            stats["nba_skipped"].append({"deal": z.name, "reason": result.error, "problems": result.problems})
        stats["nba_done"] += 1
        save()  # progress is visible while the run is still going, skips included

    # The first draft goes alone: it writes the prompt cache (the shared system prefix) once, and
    # the parallel drafts after it read it instead of each paying to write it.
    for z in to_draft[:1]:
        keep(z, generate_nba(client, settings.llm_model_actions, z.name, cards[z.zoho_id]))  # never raises
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(tracing.propagate_context(generate_nba), client, settings.llm_model_actions, z.name,
                               cards[z.zoho_id]): z for z in to_draft[1:]}
        for future in as_completed(futures):
            keep(futures[future], future.result())

    run.status = "partial" if stats["nba_skipped"] else "succeeded"
    run.finished_at = datetime.now(timezone.utc)
    save(phase="done")
    tracing.flush()
    logger.info("Run %s finished: %s", run.id, run.status)
    return run

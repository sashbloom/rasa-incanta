"""What the board shows: the week's three boards and one deal's detail.

The board is open, so everyone sees every active, on-board deal. Every query still goes through
`visible_deals`, the one place per-user SBU scoping would return (`user_allowed_sbus` is kept
in the schema for that).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from api.config import Settings
from api.domain.gaps import Gap, gap_copy
from api.domain.stages import Board, stage_position
from api.domain.weeks import week_start
from api.models import ContextCard, Deal, Recommendation, Run

BOARD_ORDER = (Board.PIPELINE, Board.PRE_PIPELINE, Board.PROSPECT)
BOARD_LABEL = {Board.PIPELINE: "Pipeline", Board.PRE_PIPELINE: "Pre-Pipeline", Board.PROSPECT: "Prospect"}

SEGMENTS = (
    ("fit", "Fit", "account_fit", (Gap.NO_ICP,)),
    ("contact", "Contact", "stakeholder", (Gap.NO_CONTACT,)),
    ("conversation", "Conversation", "conversation", (Gap.NO_CALL_LOGGED, Gap.NO_MAIL)),
    ("proof", "Proof", "capability", (Gap.NO_SETU_MATCH,)),
    ("deal_state", "Deal state", "deal_state", ()),
)
SOURCE_LABEL = {"zoho": "Zoho", "readai": "Call", "outlook": "Mail", "setu": "Setu", "icp": "ICP"}


def visible_deals() -> Select:
    return select(Deal).where(Deal.is_active.is_(True), Deal.board.is_not(None))


def _run_notice(session: Session) -> str | None:
    latest = session.scalar(select(Run).order_by(Run.started_at.desc()).limit(1))
    if latest is None or latest.status != "failed":
        return None
    at = latest.started_at.strftime("%H:%M") if latest.started_at else "the last run"
    return f"Zoho didn't respond at {at}. Showing the latest deals we have."


def week_view(session: Session, settings: Settings, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    week = week_start(now, settings.timezone)
    deals = list(session.scalars(visible_deals()))
    with_actions = set(session.scalars(
        select(Recommendation.deal_id).join(Run, Run.id == Recommendation.run_id)
        .where(Run.week_start == week, Recommendation.deal_id.in_([d.id for d in deals]))
    )) if deals else set()

    boards = []
    for board in BOARD_ORDER:
        on_board = [d for d in deals if d.board == board.value]
        stages: dict[str, list[dict]] = {}
        for d in sorted(on_board, key=lambda d: (stage_position(d.stage) or 0, d.name.lower())):
            stages.setdefault(d.stage, []).append({
                "id": str(d.id), "name": d.name, "account_name": d.account_name, "owner_name": d.owner_name,
                "has_actions": d.id in with_actions,
            })
        boards.append({
            "id": board.value, "label": BOARD_LABEL[board], "count": len(on_board),
            "stages": [{"stage": s, "count": len(ds), "deals": ds} for s, ds in stages.items()],
        })
    return {"week_start": week.isoformat(), "notice": _run_notice(session), "boards": boards}


def _segments(card: ContextCard | None) -> list[dict]:
    gaps = set(card.gaps or []) if card else set()
    out = []
    for key, name, column, flags in SEGMENTS:
        signal = (getattr(card, column) if card else None) or {}
        facts = signal.get("facts") or []
        if facts:
            dates = [f["date"] for f in facts if f.get("date")]
            out.append({"key": key, "name": name, "present": True,
                        "source": SOURCE_LABEL.get(facts[0].get("source"), facts[0].get("source")),
                        "date": max(dates) if dates else None, "reason": None})
        else:
            reason = " ".join(gap_copy(f.value) for f in flags if f.value in gaps) or "Nothing found yet."
            out.append({"key": key, "name": name, "present": False, "source": None, "date": None, "reason": reason})
    return out


def deal_view(session: Session, deal_id: uuid.UUID) -> dict | None:
    deal = session.scalar(visible_deals().where(Deal.id == deal_id))
    if deal is None:
        return None
    card = session.scalar(
        select(ContextCard).join(Run, Run.id == ContextCard.run_id)
        .where(ContextCard.deal_id == deal.id).order_by(Run.started_at.desc()).limit(1)
    )
    latest_rec = session.scalar(
        select(Recommendation).where(Recommendation.deal_id == deal.id).order_by(Recommendation.created_at.desc()).limit(1)
    )
    actions, actions_week = [], None
    if latest_rec is not None:
        run = session.get(Run, latest_rec.run_id)
        actions_week = run.week_start.isoformat() if run else None
        actions = [
            {"id": str(r.id), "rank": r.rank, "objective": r.objective, "action": r.action, "why_now": r.why_now,
             "effort": r.effort, "sme": r.sme, "proof": r.proof or None,
             "evidence": [{**e, "source_label": SOURCE_LABEL.get(e.get("source"), e.get("source"))}
                          for e in r.evidence or []]}
            for r in session.scalars(
                select(Recommendation).where(Recommendation.run_id == latest_rec.run_id,
                                             Recommendation.deal_id == deal.id).order_by(Recommendation.rank)
            )
        ]
    state = (card.deal_state if card else {}) or {}
    return {
        "id": str(deal.id), "name": deal.name, "stage": deal.stage, "board": deal.board, "sbu": deal.sbu,
        "account_name": deal.account_name, "contact_name": deal.contact_name, "owner_name": deal.owner_name,
        "ep_involved": deal.ep_involved or [], "el_involved": deal.el_involved or [],
        "days_in_stage": state.get("days_in_stage"), "last_touch": state.get("last_touch"),
        "segments": _segments(card), "actions": actions, "actions_week": actions_week,
    }

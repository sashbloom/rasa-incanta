"""The decision layer: saving a user's decision on a deal, each deal's weekly history, and the weekly
flags (new, moved, decided, pending) behind the My week list and the Summary page.

A decision belongs to the week of the actions it was made on (the week of the run that drafted them),
which is the week `engine.run.decided_this_week` looks at, so a rerun leaves a decided deal alone.
The board is open, so a review has no user (`user_id` None) until sign-in returns. History is
append-only: editing a decision adds a `deal_reviews` row with the next version for the same deal and
week, the highest version is the current decision, and earlier versions stay as history.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from api.config import Settings
from api.domain.weeks import week_start
from api.models import Deal, DealReview, DealSnapshot, Decision, Recommendation, Run


def _iso(value: datetime) -> str:
    """UTC ISO 8601. SQLite hands timestamps back without a zone; they are UTC."""
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()


def latest_actions(session: Session, deal_id: uuid.UUID) -> tuple[date | None, list[Recommendation]]:
    """The week and the actions of the newest run that drafted any for this deal."""
    latest = session.scalar(select(Recommendation).where(Recommendation.deal_id == deal_id)
                            .order_by(Recommendation.created_at.desc()).limit(1))
    if latest is None:
        return None, []
    run = session.get(Run, latest.run_id)
    recs = list(session.scalars(select(Recommendation)
                                .where(Recommendation.run_id == latest.run_id, Recommendation.deal_id == deal_id)
                                .order_by(Recommendation.rank)))
    return (run.week_start if run else None), recs


def latest_action_weeks(session: Session) -> dict[uuid.UUID, date]:
    """{deal id: week of its newest actions}."""
    out: dict[uuid.UUID, date] = {}
    rows = session.execute(select(Recommendation.deal_id, Run.week_start)
                           .join(Run, Run.id == Recommendation.run_id)
                           .order_by(Recommendation.created_at.desc()))
    for deal_id, week in rows:
        out.setdefault(deal_id, week)
    return out


def decided_deal_ids(session: Session, weeks: dict[uuid.UUID, date]) -> set[uuid.UUID]:
    """Deals with a review for the week their newest actions are from."""
    return {deal_id for deal_id, week in session.execute(select(DealReview.deal_id, DealReview.week_start))
            if weeks.get(deal_id) == week}


def _picks(session: Session, review: DealReview) -> dict[uuid.UUID, bool]:
    return {d.recommendation_id: d.selected
            for d in session.scalars(select(Decision).where(Decision.review_id == review.id))}


def reviews_for(session: Session, deal_id: uuid.UUID) -> dict[date, tuple[DealReview, dict[uuid.UUID, bool]]]:
    """{week: (current review, {recommendation id: ticked})}. The current review is the highest version."""
    out: dict[date, tuple[DealReview, dict[uuid.UUID, bool]]] = {}
    for review in session.scalars(select(DealReview).where(DealReview.deal_id == deal_id)
                                  .order_by(DealReview.version.desc(), DealReview.created_at.desc())):
        if review.week_start not in out:
            out[review.week_start] = (review, _picks(session, review))
    return out


def _review_payload(review: DealReview, picks: dict[uuid.UUID, bool]) -> dict:
    return {
        "version": review.version, "rationale": review.rationale, "own_action": review.own_action,
        "decided_at": _iso(review.completed_at or review.created_at),
        "selected_ids": [str(rid) for rid, ticked in picks.items() if ticked],
    }


def decision_for(session: Session, deal_id: uuid.UUID, week: date | None) -> dict | None:
    """The current decision for that week, with the versions it replaced (newest first) under `earlier`."""
    found = reviews_for(session, deal_id).get(week) if week else None
    if found is None:
        return None
    review, picks = found
    older = session.scalars(select(DealReview).where(DealReview.deal_id == deal_id, DealReview.week_start == week,
                                                     DealReview.id != review.id)
                            .order_by(DealReview.version.desc(), DealReview.created_at.desc()))
    return {"week_start": week.isoformat(), **_review_payload(review, picks),
            "earlier": [_review_payload(r, _picks(session, r)) for r in older]}


def history_for(session: Session, deal_id: uuid.UUID, skip_week: date | None) -> list[dict]:
    """Every week other than `skip_week` that had actions, newest first: the actions of that week's last
    run, which were ticked, and the rationale. Nothing is dropped; the page shows four and tucks the rest
    behind "Show older"."""
    recs = list(session.scalars(select(Recommendation).where(Recommendation.deal_id == deal_id)))
    if not recs:
        return []
    runs = list(session.scalars(select(Run).where(Run.id.in_({r.run_id for r in recs}))))
    chosen: dict[date, Run] = {}
    for run in sorted(runs, key=lambda r: _iso(r.started_at), reverse=True):
        chosen.setdefault(run.week_start, run)
    reviews = reviews_for(session, deal_id)

    out = []
    for week in sorted(chosen, reverse=True):
        if week == skip_week:
            continue
        review, picks = reviews.get(week, (None, {}))
        actions = sorted((r for r in recs if r.run_id == chosen[week].id), key=lambda r: r.rank)
        out.append({
            "week_start": week.isoformat(), "decided": review is not None,
            "rationale": review.rationale if review else None, "own_action": review.own_action if review else None,
            "actions": [{"id": str(r.id), "objective": r.objective, "action": r.action, "why_now": r.why_now,
                         "selected": picks.get(r.id, False) if review else None} for r in actions],
        })
    return out


def save_decision(session: Session, deal: Deal, selected: list[uuid.UUID], rationale: str,
                  own_action: str | None, now: datetime | None = None) -> date:
    """Record the decision on this deal's current actions; returns the week it was made for. A deal that is
    already decided for the week gets a new version (an edit): the old rows stay. Raises HTTPException: 422
    for no rationale or a tick that is not one of the deal's actions, 409 when the deal has no actions or
    someone else saved at the same moment."""
    now = now or datetime.now(timezone.utc)
    rationale = (rationale or "").strip()
    if not rationale:
        raise HTTPException(422, "Add a line of rationale to save this decision.")
    week, recs = latest_actions(session, deal.id)
    if not recs or week is None:
        raise HTTPException(409, "This deal has no actions to decide on yet.")
    ticked = set(selected)
    if ticked - {r.id for r in recs}:
        raise HTTPException(422, "One of the ticked actions does not belong to this deal.")
    current = reviews_for(session, deal.id).get(week)

    review = DealReview(week_start=week, deal_id=deal.id, user_id=None, version=(current[0].version + 1) if current else 1,
                        rationale=rationale, own_action=(own_action or "").strip() or None, status="done",
                        completed_at=now)
    session.add(review)
    try:
        session.flush()
        for r in recs:
            session.add(Decision(review_id=review.id, recommendation_id=r.id, selected=r.id in ticked))
        session.commit()
    except IntegrityError:  # two saves at once: the unique index let only one version through
        session.rollback()
        raise HTTPException(409, "This decision was just changed by someone else. Reload the deal and try again.") from None
    return week


# ---------------------------------------------------------------- weekly flags

def change_flags(session: Session, settings: Settings, now: datetime) -> tuple[bool, dict[uuid.UUID, dict]]:
    """(comparison possible, {deal id: {"new": bool, "moved": {"from", "to"} or None}}) for the current week.
    A deal is new when its first snapshot is this week's and moved when its stage this week differs from its
    stage in the last earlier week. With no earlier week on record there is nothing to compare with."""
    week = week_start(now, settings.timezone)
    rows = session.execute(select(DealSnapshot.deal_id, DealSnapshot.stage, Run.week_start)
                           .join(Run, Run.id == DealSnapshot.run_id).order_by(Run.started_at))
    current: dict[uuid.UUID, str] = {}
    earlier: dict[uuid.UUID, str] = {}
    for deal_id, stage, snap_week in rows:  # oldest first, so the last write per deal is its newest
        if snap_week == week:
            current[deal_id] = stage
        elif snap_week < week:
            earlier[deal_id] = stage
    comparison = bool(earlier)
    flags: dict[uuid.UUID, dict] = {}
    for deal_id, stage in current.items():
        if not comparison:
            flags[deal_id] = {"new": False, "moved": None}
        elif deal_id not in earlier:
            flags[deal_id] = {"new": True, "moved": None}
        else:
            moved = {"from": earlier[deal_id], "to": stage} if earlier[deal_id] != stage else None
            flags[deal_id] = {"new": False, "moved": moved}
    return comparison, flags

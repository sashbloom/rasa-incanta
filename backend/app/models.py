"""Core tables for Rasa Incanta.

Brick 1 creates what the first end-to-end slice needs. Later bricks add calls, mails,
company caches (ICP and persona), treasury ideas and learning tables through new
Alembic migrations.

History rules: runs, snapshots, context cards, recommendations and decisions are
append-only. Foreign keys deliberately have no cascade, so history cannot be
deleted by accident.
"""
import uuid
from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

# JSONB on Postgres (queryable, indexable); plain JSON on SQLite for local runs and tests.
JSONType = JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class User(Timestamped, Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    display_name: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20), default="user")  # admin | user | viewer
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    aliases: Mapped[list["UserAlias"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class UserAlias(Timestamped, Base):
    """How a user's name appears in Zoho's Owner, EP Involved or EL Involved fields, e.g. "Nair"."""

    __tablename__ = "user_aliases"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    alias: Mapped[str] = mapped_column(String(200), unique=True)

    user: Mapped[User] = relationship(back_populates="aliases")


class Run(Timestamped, Base):
    """One weekly (or manual) pass over the pipeline."""

    __tablename__ = "runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    week_start: Mapped[date] = mapped_column(Date, index=True)
    kind: Mapped[str] = mapped_column(String(20), default="weekly")  # weekly | manual
    status: Mapped[str] = mapped_column(String(20), default="running")  # running | succeeded | partial | failed
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stats: Mapped[dict] = mapped_column(JSONType, default=dict)
    error: Mapped[str | None] = mapped_column(Text)


class Deal(Timestamped, Base):
    """Latest known state of a Zoho opportunity (Potentials / Deals module)."""

    __tablename__ = "deals"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    zoho_id: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(300))
    account_name: Mapped[str | None] = mapped_column(String(300))
    contact_name: Mapped[str | None] = mapped_column(String(200))
    owner_name: Mapped[str | None] = mapped_column(String(200))
    stage: Mapped[str] = mapped_column(String(100))
    board: Mapped[str | None] = mapped_column(String(20))  # prospect | pre_pipeline | pipeline; None = out of scope
    industry: Mapped[str | None] = mapped_column(String(200))
    city_state: Mapped[str | None] = mapped_column(String(200))
    lead_source: Mapped[str | None] = mapped_column(String(200))
    ep_involved: Mapped[list] = mapped_column(JSONType, default=list)
    el_involved: Mapped[list] = mapped_column(JSONType, default=list)
    stage_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    zoho_modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    raw: Mapped[dict] = mapped_column(JSONType, default=dict)


class DealSnapshot(Timestamped, Base):
    """What a deal looked like in a given run."""

    __tablename__ = "deal_snapshots"
    __table_args__ = (UniqueConstraint("run_id", "deal_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id"), index=True)
    deal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("deals.id"), index=True)
    stage: Mapped[str] = mapped_column(String(100))
    board: Mapped[str | None] = mapped_column(String(20))
    data: Mapped[dict] = mapped_column(JSONType, default=dict)


class ContextCard(Timestamped, Base):
    """The five input signals for one deal in one run, plus the gaps where a source had nothing."""

    __tablename__ = "context_cards"
    __table_args__ = (UniqueConstraint("run_id", "deal_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id"), index=True)
    deal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("deals.id"), index=True)
    account_fit: Mapped[dict] = mapped_column(JSONType, default=dict)
    stakeholder: Mapped[dict] = mapped_column(JSONType, default=dict)
    conversation: Mapped[dict] = mapped_column(JSONType, default=dict)
    capability: Mapped[dict] = mapped_column(JSONType, default=dict)
    deal_state: Mapped[dict] = mapped_column(JSONType, default=dict)
    gaps: Mapped[list] = mapped_column(JSONType, default=list)


class Recommendation(Timestamped, Base):
    """One next best action (NBA) for one deal, for one user, in one run."""

    __tablename__ = "recommendations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id"), index=True)
    deal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("deals.id"), index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), index=True)
    rank: Mapped[int] = mapped_column(Integer, default=1)
    objective: Mapped[str] = mapped_column(String(20))  # see app.domain.objectives.Objective
    action: Mapped[str] = mapped_column(Text)
    why_now: Mapped[str] = mapped_column(Text)
    evidence: Mapped[list] = mapped_column(JSONType, default=list)  # [{source, ref, date, excerpt}]
    proof: Mapped[dict] = mapped_column(JSONType, default=dict)  # matched Setu case study
    sme: Mapped[str | None] = mapped_column(String(200))  # Practus person to bring in
    effort: Mapped[str | None] = mapped_column(String(10))  # low | medium | high
    treasury_ref: Mapped[str | None] = mapped_column(String(20))  # closest Ideas Treasury idea, e.g. "4.1"
    gaps: Mapped[list] = mapped_column(JSONType, default=list)
    model: Mapped[str | None] = mapped_column(String(100))


class DealReview(Timestamped, Base):
    """A user's review of one deal for one week: their rationale and any action of their own."""

    __tablename__ = "deal_reviews"
    __table_args__ = (UniqueConstraint("week_start", "deal_id", "user_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    week_start: Mapped[date] = mapped_column(Date, index=True)
    deal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("deals.id"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    rationale: Mapped[str | None] = mapped_column(Text)
    own_action: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="open")  # open | done
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Decision(Timestamped, Base):
    """Whether the user selected a given recommendation in their review."""

    __tablename__ = "decisions"
    __table_args__ = (UniqueConstraint("review_id", "recommendation_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    review_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("deal_reviews.id"), index=True)
    recommendation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("recommendations.id"), index=True)
    selected: Mapped[bool] = mapped_column(Boolean, default=False)

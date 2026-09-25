import pytest
from sqlalchemy import func, select

from api.db import get_sessionmaker
from api.engine.run import run_week
from api.models import ContextCard, Deal, DealReview, DealSnapshot, Decision, Recommendation, User
from api.sources.zoho import fetch_deals
from tests.fakes import NOW, FakeClaude, FakeConnection, claude_response, load, settings, zoho_connect


@pytest.fixture()
def session(migrated):
    with get_sessionmaker()() as s:
        yield s


def zoho_fetch(conn=None):
    conn = conn or FakeConnection()
    return lambda s: fetch_deals(s, connect_fn=zoho_connect(conn))


def reply():
    return claude_response(load("claude/nba_reply.json"))


def count(session, model):
    return session.scalar(select(func.count()).select_from(model))


def test_run_pulls_deals_builds_cards_and_one_nba(session):
    run = run_week(session, settings(), now=NOW, fetch=zoho_fetch(), llm_client=FakeClaude(reply()))
    assert run.status == "succeeded", run.stats
    assert str(run.week_start) == "2026-09-21"
    assert count(session, Deal) == 2
    assert count(session, DealSnapshot) == 2
    assert count(session, ContextCard) == 2

    rec = session.scalar(select(Recommendation))
    deal = session.get(Deal, rec.deal_id)
    assert deal.name == "Northwind Foods - Working capital"  # Proposal Sent outranks Qualified Prospect
    assert deal.board == "pipeline"
    assert deal.sbu == "India"
    assert rec.objective == "advance" and rec.model == "claude-sonnet-5"
    assert rec.evidence and all(e["source"] == "zoho" for e in rec.evidence)
    assert "no_mail" in rec.gaps and "no_call_logged" not in rec.gaps  # a Teams meeting is on the outreach log


def test_rerun_regenerates_undecided_and_keeps_decided(session):
    run_week(session, settings(), now=NOW, fetch=zoho_fetch(), llm_client=FakeClaude(reply()))
    run_week(session, settings(), now=NOW, fetch=zoho_fetch(), llm_client=FakeClaude(reply()))
    assert count(session, Recommendation) == 2  # append-only: the first stays as history
    assert count(session, DealSnapshot) == 4

    latest = session.scalars(select(Recommendation).order_by(Recommendation.created_at.desc())).first()
    user = User(username="rao")
    session.add(user)
    session.flush()
    review = DealReview(week_start=NOW.date().replace(day=21), deal_id=latest.deal_id, user_id=user.id, rationale="Fits")
    session.add(review)
    session.flush()
    session.add(Decision(review_id=review.id, recommendation_id=latest.id, selected=True))
    session.commit()

    third = run_week(session, settings(), now=NOW, fetch=zoho_fetch(), llm_client=FakeClaude())
    assert third.stats["nba_kept"] == 1 and third.stats["nba_created"] == 0
    assert count(session, Recommendation) == 2


def test_deal_missing_from_the_pull_goes_inactive(session):
    run_week(session, settings(), now=NOW, fetch=zoho_fetch(), llm_client=FakeClaude(reply()))
    only_first = FakeConnection(deals=load("zoho/deals.json")[:1])
    run_week(session, settings(), now=NOW, fetch=zoho_fetch(only_first), llm_client=FakeClaude(reply()))
    blue = session.scalar(select(Deal).where(Deal.name.like("Blue Harbour%")))
    assert blue.is_active is False


def test_zoho_failure_fails_the_run_without_raising(session):
    run = run_week(session, settings(zoho_pg_host=""), now=NOW, fetch=fetch_deals, llm_client=FakeClaude())
    assert run.status == "failed" and "not configured" in run.error
    assert count(session, Deal) == 0


def test_no_api_key_means_cards_but_no_nba(session):
    run = run_week(session, settings(), now=NOW, fetch=zoho_fetch())
    assert run.status == "partial"
    assert run.stats["nba_skipped"][0]["reason"] == "ANTHROPIC_API_KEY is not set."
    assert count(session, ContextCard) == 2 and count(session, Recommendation) == 0


def test_rejected_draft_is_flagged_not_saved(session):
    bad = claude_response(dict(load("claude/nba_reply.json"), evidence_ids=[]))
    run = run_week(session, settings(), now=NOW, fetch=zoho_fetch(), llm_client=FakeClaude(bad, bad))
    assert run.status == "partial" and count(session, Recommendation) == 0
    assert run.stats["nba_skipped"][0]["problems"]


def test_a_named_deal_gets_the_nba(session):
    fits = claude_response(dict(load("claude/nba_reply.json"), objective="re_engage", evidence_ids=["zoho.stage"]))
    run_week(session, settings(), now=NOW, fetch=zoho_fetch(), llm_client=FakeClaude(fits),
             deal_zoho_id="598723000011234002")
    rec = session.scalar(select(Recommendation))
    assert session.get(Deal, rec.deal_id).name.startswith("Blue Harbour")


def test_citing_another_deals_facts_is_rejected(session):
    # Northwind's reply cites a proposal date and problem statement that Blue Harbour's card does not have.
    run = run_week(session, settings(), now=NOW, fetch=zoho_fetch(), llm_client=FakeClaude(reply(), reply()),
                   deal_zoho_id="598723000011234002")
    assert count(session, Recommendation) == 0
    assert "not on the card" in run.stats["nba_skipped"][0]["problems"][0]

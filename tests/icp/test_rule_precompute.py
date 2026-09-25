# Copied from icp-bot/backend/tests/test_rule_precompute.py; only import paths are adapted.
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.icp import rule_precompute as rp
from api.icp.models import (
    CrmStructured,
    DealHistoryRow,
    EvidenceLabel,
    ReachoutRow,
    UnstructuredEvidenceItem,
)

# rule_precompute's date parsing always yields naive datetimes (see
# parse_dt) -- `now` must match, same convention as test_scorer.py's NOW.
NOW = datetime(2026, 8, 18)


def test_a2_mode_is_a_only_at_qp_or_later():
    assert rp.a2_mode("Qualified Prospect") == "A"
    assert rp.a2_mode("Proposal Sent") == "A"
    assert rp.a2_mode("Negotiated Proposal Sent") == "A"
    assert rp.a2_mode("Prospect") == "B"
    assert rp.a2_mode("Need Identification") == "B"
    assert rp.a2_mode(None) == "B"
    assert rp.a2_mode("not-a-real-stage") == "B"


def test_is_stale_reads_the_most_recent_stage_history_row():
    crm = CrmStructured(stage_history=[
        DealHistoryRow(stage="Prospect", modified_time=(NOW - timedelta(days=400)).isoformat()),
        DealHistoryRow(stage="Qualified Prospect", modified_time=(NOW - timedelta(days=17)).isoformat()),
    ])
    assert rp.is_stale(crm, NOW) is False  # most recent row is 17 days ago


def test_is_stale_true_past_180_days():
    crm = CrmStructured(stage_modified_time="2025-01-01T00:00:00")
    assert rp.is_stale(crm, NOW) is True


def test_ticket_ratios_requires_a_real_revenue_figure():
    assert rp.ticket_ratios(None, 1000.0, 100.0) == {"ticket_to_revenue_pct": None, "ticket_to_ebitda_pct": None}
    assert rp.ticket_ratios(10.0, None, 100.0) == {"ticket_to_revenue_pct": None, "ticket_to_ebitda_pct": None}
    # `amount` is a raw rupee figure (CRM's `deals.amount`) -- ₹1 Cr in
    # rupees (10,000,000) against a ₹2,000 Cr revenue / ₹200 Cr EBITDA.
    result = rp.ticket_ratios(10_000_000.0, 2000.0, 200.0)
    assert result["ticket_to_revenue_pct"] == 0.05
    assert result["ticket_to_ebitda_pct"] == 0.5


def test_ticket_ratios_converts_the_raw_rupee_amount_to_crores_before_dividing():
    """Real bug (Sobha, 2026-09-10): `amount` (CRM's raw-rupee `Amount`
    field, e.g. 9,000,000 for a ₹90L ticket) was divided directly against
    `revenue`/`ebitda` (already in crores) with no unit conversion -- a
    real, unremarkable ~0.017% ticket-to-revenue ratio came out as
    "173,410%" (~1,734x) and was presented in a live report as a
    fabricated headline red flag."""
    result = rp.ticket_ratios(9_000_000.0, 5190.0, None)

    assert result["ticket_to_revenue_pct"] == 0.0173


def test_scale_mismatch_flag_requires_all_three_conditions():
    # Mode B (pre-QP) -- never fires regardless of the other two.
    assert rp.scale_mismatch_flag(stage="Prospect", ticket_to_revenue_pct=0.001, c1_score=1) is False
    # Mode A but ratio not implausibly small.
    assert rp.scale_mismatch_flag(stage="Qualified Prospect", ticket_to_revenue_pct=0.5, c1_score=1) is False
    # Mode A, small ratio, but C1 shows a verified signatory (scenario 42) -- flag does NOT fire.
    assert rp.scale_mismatch_flag(stage="Qualified Prospect", ticket_to_revenue_pct=0.001, c1_score=5) is False
    # All three -- fires (scenario 43).
    assert rp.scale_mismatch_flag(stage="Qualified Prospect", ticket_to_revenue_pct=0.001, c1_score=2) is True


def _reachout(days_ago: int, contact="Priya Shah", email="priya@target.com", designation="CFO") -> ReachoutRow:
    return ReachoutRow(
        reachout_date=(NOW - timedelta(days=days_ago)).isoformat(), client_contact_name=contact, email=email,
        designation=designation,
    )


def test_c2_no_contact_at_all_when_nothing_gathered():
    interp = rp.c2_warmth_interpretation(CrmStructured(), [], NOW)
    assert interp.condition_label == "no_contact_at_all"
    assert interp.data_gap is True


def test_c2_multiple_senior_contacts_within_60_days():
    crm = CrmStructured(reachout_tracker=[
        _reachout(10, "Priya Shah", "priya@target.com"),
        _reachout(20, "Ravi Kumar", "ravi@target.com"),
    ])
    interp = rp.c2_warmth_interpretation(crm, [], NOW)
    assert interp.condition_label == "multiple_senior_contacts_meeting_last_60d"
    assert interp.data_gap is False
    assert len(interp.supporting_facts) == 2


def test_c2_one_contact_within_60_days():
    crm = CrmStructured(reachout_tracker=[_reachout(10)])
    interp = rp.c2_warmth_interpretation(crm, [], NOW)
    assert interp.condition_label == "one_senior_contact_meeting_last_60d"


def test_c2_a_recent_junior_contact_does_not_count_as_senior():
    """icp-skill.md C2's bands are explicitly about SENIOR contacts, not
    contacts generally -- previously any named/emailed tracker row counted
    toward "senior," so two junior touches could score a 5 as easily as two
    CXO touches. A junior-only recent contact still "exists" (band 3), it
    just doesn't clear the seniority bar for bands 4/5."""
    crm = CrmStructured(reachout_tracker=[_reachout(10, designation="Executive Assistant")])
    interp = rp.c2_warmth_interpretation(crm, [], NOW)
    assert interp.condition_label == "contact_exists_last_touch_60_to_180d"


def test_c2_contact_role_signatory_counts_as_senior_even_without_a_title_keyword():
    row = ReachoutRow(
        reachout_date=(NOW - timedelta(days=5)).isoformat(), client_contact_name="Alex Doe",
        email="alex@target.com", designation="", contact_role="Signatory",
    )
    crm = CrmStructured(reachout_tracker=[row])
    interp = rp.c2_warmth_interpretation(crm, [], NOW)
    assert interp.condition_label == "one_senior_contact_meeting_last_60d"


def test_c2_contact_role_influencer_does_not_count_as_senior():
    row = ReachoutRow(
        reachout_date=(NOW - timedelta(days=5)).isoformat(), client_contact_name="Alex Doe",
        email="alex@target.com", designation="Chief of Something", contact_role="Influencer",
    )
    crm = CrmStructured(reachout_tracker=[row])
    interp = rp.c2_warmth_interpretation(crm, [], NOW)
    # contact_role is the stronger signal -- an explicit "Influencer" role
    # overrides a designation string that happens to contain "Chief".
    assert interp.condition_label == "contact_exists_last_touch_60_to_180d"


def test_c2_contact_exists_last_touch_60_to_180_days():
    crm = CrmStructured(reachout_tracker=[_reachout(100)])
    interp = rp.c2_warmth_interpretation(crm, [], NOW)
    assert interp.condition_label == "contact_exists_last_touch_60_to_180d"


def test_c2_contact_exists_but_over_180_days():
    crm = CrmStructured(reachout_tracker=[_reachout(200)])
    interp = rp.c2_warmth_interpretation(crm, [], NOW)
    assert interp.condition_label == "contact_exists_no_meeting_or_over_180d"


def test_c2_meeting_and_outlook_touches_count_toward_recency_not_contact_count():
    # One reachout-tracker contact, but a recent Outlook touch keeps it warm
    # without being counted as a second distinct "senior contact".
    crm = CrmStructured(reachout_tracker=[_reachout(150)])
    recent_outlook = UnstructuredEvidenceItem(
        text="thread", source="Outlook (mahak@practus.com)", source_type="outlook",
        date=(NOW - timedelta(days=5)).isoformat(),
    )
    interp = rp.c2_warmth_interpretation(crm, [recent_outlook], NOW)
    assert interp.condition_label == "one_senior_contact_meeting_last_60d"


def test_c2_a_meeting_touch_with_empty_reachout_tracker_is_not_a_contradictory_data_gap():
    """Real bug (Livpure Smart Homes, 2026-09-10, a no-CRM manual entry):
    an empty Reachout_tracker with a real, recent meeting touch drove the
    label/score to a real "contact exists" band (touch_dates already
    factors in meeting/Outlook dates), but supporting_facts only ever
    recorded Reachout_tracker rows -- so the object came back internally
    contradictory: data_gap=False and a real score, yet
    supporting_facts=[]/evidence_label_overall=DATA_GAP. The narrative
    correctly saw the empty supporting_facts and wrote "cannot be
    verified," while the score silently used the meeting date anyway."""
    recent_meeting = UnstructuredEvidenceItem(
        text="Meeting \"Livpure & Practus\" on 2026-09-08: intro call.",
        source="Read.ai", source_type="meeting",
        date=(NOW - timedelta(days=2)).isoformat(),
    )

    interp = rp.c2_warmth_interpretation(CrmStructured(), [recent_meeting], NOW)

    assert interp.condition_label == "contact_exists_last_touch_60_to_180d"
    assert interp.data_gap is False
    assert interp.evidence_label_overall == EvidenceLabel.FACT
    assert len(interp.supporting_facts) == 1
    assert interp.supporting_facts[0].date == recent_meeting.date

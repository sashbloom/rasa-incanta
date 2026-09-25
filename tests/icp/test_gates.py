# Copied from icp-bot/backend/tests/test_gates.py; only import paths are adapted.
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.icp import gates as g


def test_gate_2_fires_only_on_c1_equals_1():
    assert g.gate_2_authority(1).fired is True
    assert g.gate_2_authority(2).fired is False
    assert g.gate_2_authority(None).fired is False


def test_gate_3_fires_at_two_plus_unexplained_losses():
    # `unexplained_loss_count` is already pre-filtered upstream by
    # practus_history.classify() (per-loss "no reason" check + 24-month
    # lookback) -- gate_3_repeat_loss only thresholds it at 2+.
    assert g.gate_3_repeat_loss(2).fired is True
    assert g.gate_3_repeat_loss(3).fired is True
    assert g.gate_3_repeat_loss(1).fired is False
    assert g.gate_3_repeat_loss(0).fired is False


def test_gate_6_fires_when_problem_not_established():
    assert g.gate_6_problem_unknown(False).fired is True
    assert g.gate_6_problem_unknown(True).fired is False


def test_gate_7_fires_at_four_of_nine_data_gaps():
    assert g.gate_7_evidence_floor(4).fired is True
    assert g.gate_7_evidence_floor(3).fired is False


def test_gate_4_conflict_carries_the_named_competitor_detail():
    fired = g.gate_4_conflict(True, "An active Practus pursuit exists at 'Rival Co'.")
    assert fired.fired is True
    assert "Rival Co" in fired.detail

    not_fired = g.gate_4_conflict(False, "")
    assert not_fired.fired is False
    assert not_fired.detail == ""


def test_most_restrictive_wins_park_beats_nurture():
    assert g.most_restrictive("Park", "Nurture") == "Park"
    assert g.most_restrictive("Nurture", "Park") == "Park"
    assert g.most_restrictive("Pursue aggressively", "Pursue selectively") == "Pursue selectively"


def test_apply_gate_effects_gate_2_and_3_both_fire_most_restrictive_wins():
    gate_list = [
        g.gate_1_entity(True, ""),
        g.gate_2_authority(1),  # fires -> Nurture cap
        g.gate_3_repeat_loss(2),  # fires -> Park cap (more restrictive)
        g.gate_4_conflict(False),
        g.gate_5_staleness(False),
        g.gate_6_problem_unknown(True),
        g.gate_7_evidence_floor(0),
    ]
    result, reason = g.apply_gate_effects("Pursue aggressively", gate_list)
    assert result == "Park"
    assert "Park" not in reason  # detail text doesn't literally need the word, just checking it's non-empty
    assert reason

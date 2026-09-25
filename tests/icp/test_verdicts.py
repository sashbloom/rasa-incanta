# Copied from icp-bot/backend/tests/test_verdicts.py; only import paths are adapted.
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.icp import verdicts as vd


def test_round_half_up():
    assert vd.round_half_up(10.5) == 11
    assert vd.round_half_up(10.4) == 10
    assert vd.round_half_up(2.5) == 3  # Python's builtin round() would give 2 (banker's rounding)


def test_client_lens_verdict_bands():
    assert vd.client_lens_verdict(37) == "Strong"
    assert vd.client_lens_verdict(36) == "Moderate"
    assert vd.client_lens_verdict(27) == "Moderate"
    assert vd.client_lens_verdict(26) == "Weak"


def test_group_sub_verdict_ability_bands():
    assert vd.group_sub_verdict("Ability to Pay", 11) == "Strong"
    assert vd.group_sub_verdict("Ability to Pay", 10) == "Moderate"
    assert vd.group_sub_verdict("Ability to Pay", 8) == "Moderate"
    assert vd.group_sub_verdict("Ability to Pay", 7) == "Weak"


def test_group_sub_verdict_willingness_bands():
    assert vd.group_sub_verdict("Willingness to Pay", 15) == "Strong"
    assert vd.group_sub_verdict("Willingness to Pay", 14) == "Moderate"
    assert vd.group_sub_verdict("Willingness to Pay", 11) == "Moderate"
    assert vd.group_sub_verdict("Willingness to Pay", 10) == "Weak"


def test_recommendation_matrix_matches_skill_table():
    assert vd.recommendation_matrix_cell("Strong", "Strong") == "Pursue aggressively"
    assert vd.recommendation_matrix_cell("Strong", "Weak") == "Nurture"
    assert vd.recommendation_matrix_cell("Moderate", "Weak") == "Park"
    assert vd.recommendation_matrix_cell("Weak", "Strong") == "Park"


def test_criterion_display_band_matches_sample_output_convention():
    # Confirmed against the real Sobha - ICP.html sample: raw 5 -> Strong tag,
    # raw 4 -> Moderate tag.
    assert vd.criterion_display_band(5) == "Strong"
    assert vd.criterion_display_band(4) == "Moderate"
    assert vd.criterion_display_band(3) == "Weak"


def test_ipo_track_overlay_matches_skill_wording():
    # icp-skill.md ARCHETYPE PLAYBOOK, ~line 579: "IPO-track overlay -- Add
    # Do: IPO-readiness, FP&A maturity for DRHP, board-pack discipline. Add
    # Don't: anything conflicting with sensitive disclosures. Pricing:
    # milestones gated to DRHP, listing, +6 months." An ADD-ON, kept
    # separate from ARCHETYPE_PLAYBOOK's per-control dict.
    overlay = vd.IPO_TRACK_OVERLAY

    assert overlay["add_dos"] == ["IPO-readiness", "FP&A maturity for DRHP", "Board-pack discipline"]
    assert overlay["add_donts"] == ["Anything conflicting with sensitive disclosures"]
    assert overlay["pricing_note"] == "Milestones gated to DRHP, listing, +6 months."
    assert vd.IPO_TRACK_OVERLAY is not vd.ARCHETYPE_PLAYBOOK

# Copied from icp-bot/backend/tests/test_criteria_tables.py; only import paths are adapted.
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.icp import criteria_tables as ct


def test_group_weights_sum_to_group_max_at_score_5():
    for group_name, criterion_ids in ct.GROUPS.items():
        weight_sum = sum(ct.WEIGHTS[cid] for cid in criterion_ids)
        assert weight_sum * 5 == ct.GROUP_MAX_POINTS[group_name], group_name


def test_client_lens_weights_sum_to_ten():
    total = sum(ct.WEIGHTS[cid] for group in ct.GROUPS.values() for cid in group)
    assert total == 10.0


def test_practus_lens_weights_sum_to_ten_and_max_fifty():
    total = sum(ct.WEIGHTS[cid] for cid in ct.PRACTUS_CRITERIA)
    assert total == 10.0
    assert total * 5 == 50


def test_every_band_table_has_five_distinct_scores_1_to_5():
    for criterion_id, bands in ct.FLAT_CRITERION_BANDS.items():
        assert sorted(bands.values()) == [1, 2, 3, 4, 5], criterion_id


def test_a2_mode_tables_each_have_five_distinct_scores():
    assert sorted(ct.A2_MODE_A_BANDS.values()) == [1, 2, 3, 4, 5]
    assert sorted(ct.A2_MODE_B_BANDS.values()) == [1, 2, 3, 4, 5]


def test_allowed_labels_b2_requires_control_or_returns_union():
    from api.icp.models import OwnershipControl

    pf_labels = ct.allowed_labels("B2", OwnershipControl.PF)
    assert set(pf_labels) == set(ct.B2_BANDS_BY_CONTROL[OwnershipControl.PF])

    union_labels = ct.allowed_labels("B2")
    assert set(union_labels) >= set(pf_labels)

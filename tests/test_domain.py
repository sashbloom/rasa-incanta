import pytest

from api.domain.objectives import BUCKET_BY_OBJECTIVE, Bucket, Objective
from api.domain.stages import IN_SCOPE_STAGES, Board, board_for, stage_position


@pytest.mark.parametrize("stage, board", [
    ("Prospect", Board.PROSPECT),
    ("Need Identification", Board.PRE_PIPELINE),
    ("Walk-through Scheduled/ Conducted", Board.PRE_PIPELINE),
    ("Qualified Prospect", Board.PIPELINE),
    ("Proposal Sent", Board.PIPELINE),
    ("Negotiated Proposal Sent", Board.PIPELINE),
    ("Negetiated Proposal Sent", Board.PIPELINE),  # org typo
    ("  proposal   sent ", Board.PIPELINE),  # stray spacing and case
])
def test_in_scope_stages_map_to_boards(stage, board):
    assert board_for(stage) == board


@pytest.mark.parametrize("stage", ["Client Lost", "Client Won", "New Enquiry", "", None])
def test_other_stages_stay_off_the_boards(stage):
    assert board_for(stage) is None


def test_six_stages_plus_typo_variant():
    assert len(IN_SCOPE_STAGES) == 7


def test_relay_order():
    assert stage_position("Prospect") < stage_position("Qualified Prospect") < stage_position("Negotiated Proposal Sent")
    assert stage_position("Negetiated Proposal Sent") == stage_position("Negotiated Proposal Sent")


def test_every_objective_has_a_bucket():
    assert set(BUCKET_BY_OBJECTIVE) == set(Objective)
    assert BUCKET_BY_OBJECTIVE[Objective.REFRAME] == Bucket.REPOSITION

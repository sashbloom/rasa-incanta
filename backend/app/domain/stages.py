"""Zoho stages and the board each one belongs to.

Six stages are in scope, from Prospect to Negotiated Proposal Sent. The org's picklist
also has a misspelt "Negetiated Proposal Sent", which maps to the same board. Every
other stage, including Client Lost and Client Won, stays off the boards.
"""
from enum import Enum


class Board(str, Enum):
    PROSPECT = "prospect"
    PRE_PIPELINE = "pre_pipeline"
    PIPELINE = "pipeline"


# Stage name exactly as Zoho stores it -> (board, position in the relay)
_STAGES: dict[str, tuple[Board, int]] = {
    "Prospect": (Board.PROSPECT, 1),
    "Need Identification": (Board.PRE_PIPELINE, 2),
    "Walk-through Scheduled/ Conducted": (Board.PRE_PIPELINE, 3),
    "Qualified Prospect": (Board.PIPELINE, 4),
    "Proposal Sent": (Board.PIPELINE, 5),
    "Negotiated Proposal Sent": (Board.PIPELINE, 6),
    "Negetiated Proposal Sent": (Board.PIPELINE, 6),  # typo variant in the org's picklist
}

IN_SCOPE_STAGES: tuple[str, ...] = tuple(_STAGES)


def _normalise(stage: str) -> str:
    return " ".join(stage.split()).lower()


_BY_NORMALISED = {_normalise(name): value for name, value in _STAGES.items()}


def board_for(stage: str | None) -> Board | None:
    """Board for a Zoho stage, or None when the stage is out of scope."""
    if not stage:
        return None
    hit = _BY_NORMALISED.get(_normalise(stage))
    return hit[0] if hit else None


def stage_position(stage: str | None) -> int | None:
    """Position in the relay (1 = Prospect ... 6 = Negotiated), used for sorting and outlier checks."""
    if not stage:
        return None
    hit = _BY_NORMALISED.get(_normalise(stage))
    return hit[1] if hit else None

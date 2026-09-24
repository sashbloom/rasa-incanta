"""The five objectives a next best action can carry, and the three buckets they roll up
into for leadership summaries (stay warm, reposition, move to the next level)."""
from enum import Enum


class Objective(str, Enum):
    ADVANCE = "advance"  # progress to the next stage
    UNBLOCK = "unblock"  # resolve an objection or stakeholder gap
    REFRAME = "reframe"  # address a higher-priority problem the client raised
    NURTURE = "nurture"  # maintain engagement while the deal is on hold
    RE_ENGAGE = "re_engage"  # reactivate an inactive deal


class Bucket(str, Enum):
    STAY_WARM = "stay_warm"
    REPOSITION = "reposition"
    NEXT_LEVEL = "next_level"


BUCKET_BY_OBJECTIVE: dict[Objective, Bucket] = {
    Objective.NURTURE: Bucket.STAY_WARM,
    Objective.RE_ENGAGE: Bucket.STAY_WARM,
    Objective.REFRAME: Bucket.REPOSITION,
    Objective.ADVANCE: Bucket.NEXT_LEVEL,
    Objective.UNBLOCK: Bucket.NEXT_LEVEL,
}

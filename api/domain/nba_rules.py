"""Shape rules every next best action must pass before it is saved.

Action at most 60 words, why-now on one line, a known objective, and at least one
cited fact that really exists on the deal's context card. No evidence means no NBA.
"""
from api.domain.objectives import Objective

MAX_ACTION_WORDS = 60
MAX_WHY_NOW_WORDS = 30


def word_count(text: str) -> int:
    return len(text.split())


def check_nba(objective: str, action: str, why_now: str, evidence_ids: list[str], known_fact_ids: set[str]) -> list[str]:
    """Return the problems with a drafted NBA; an empty list means it can be saved."""
    problems: list[str] = []
    if objective not in {o.value for o in Objective}:
        problems.append(f"objective '{objective}' is not one of {[o.value for o in Objective]}")
    if not action.strip():
        problems.append("action is empty")
    elif word_count(action) > MAX_ACTION_WORDS:
        problems.append(f"action has {word_count(action)} words; the limit is {MAX_ACTION_WORDS}")
    if not why_now.strip():
        problems.append("why_now is empty")
    elif "\n" in why_now.strip():
        problems.append("why_now must be one line")
    elif word_count(why_now) > MAX_WHY_NOW_WORDS:
        problems.append(f"why_now has {word_count(why_now)} words; the limit is {MAX_WHY_NOW_WORDS}")
    if not evidence_ids:
        problems.append("no evidence cited; cite at least one fact id")
    unknown = [e for e in evidence_ids if e not in known_fact_ids]
    if unknown:
        problems.append(f"cited fact ids that are not on the card: {unknown}")
    return problems

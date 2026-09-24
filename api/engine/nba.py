"""Draft one next best action for one deal with Claude, then check it before it is saved.

Only the facts on the deal's context card go to the model (deal fields today; call and
mail excerpts from Brick 3), never whole mailboxes or transcripts. The model must cite
fact ids; a draft that cites nothing, cites a fact that is not on the card, or breaks
the length rules gets one retry with the problems spelled out, and is dropped after
that. No evidence means no NBA.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Literal

import anthropic
from pydantic import BaseModel

from api.domain.gaps import gap_copy
from api.domain.nba_rules import MAX_ACTION_WORDS, MAX_WHY_NOW_WORDS, check_nba
from api.engine.context import CardContent

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 2


class NbaDraft(BaseModel):
    objective: Literal["advance", "unblock", "reframe", "nurture", "re_engage"]
    action: str
    why_now: str
    evidence_ids: list[str]
    effort: Literal["low", "medium", "high"]


@dataclass
class NbaResult:
    draft: NbaDraft | None = None
    evidence: list[dict] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    error: str | None = None
    model: str | None = None

    @property
    def ok(self) -> bool:
        return self.draft is not None


SYSTEM_PROMPT = f"""You recommend next best actions for Practus business development. Practus is a \
consulting firm; its partners and engagement leads work open opportunities from first prospect \
to negotiated proposal.

For the deal you are given, write the single most useful action the deal team can take this week.

Objectives:
- advance: move the deal to its next stage.
- unblock: resolve an objection or a stakeholder gap that is holding it up.
- reframe: address a higher-priority problem the client has raised.
- nurture: keep an on-hold client engaged so the conversation resumes rather than restarts.
- re_engage: reactivate a deal that has gone quiet.

Rules:
- Use only the facts provided. Every claim in the action and the why-now must rest on at least \
one fact, and evidence_ids must list the ids of those facts exactly as given.
- The company and contact are exactly those on the Zoho record. Never suggest going to a more \
senior or different contact, and never name a company or person that is not in the facts.
- Some signals are missing (listed as gaps). Do not imply they exist: with no call logged, do not \
refer to what was said on a call; with no case study matched, do not cite a specific case study.
- The action is specific and concrete (who does what, with what), at most {MAX_ACTION_WORDS} words.
- why_now is one line, at most {MAX_WHY_NOW_WORDS} words, and says why this week.
- effort is low, medium or high for the Practus team.
- Plain, specific language. No hype."""


def build_user_message(deal_name: str, card: CardContent) -> str:
    payload = {
        "deal": deal_name,
        "facts": [
            {k: f[k] for k in ("id", "label", "value", "date") if f.get(k) is not None} for f in card.facts()
        ],
        "gaps": [gap_copy(g) for g in card.gaps],
    }
    return "Deal context as JSON:\n" + json.dumps(payload, ensure_ascii=False, indent=1)


def evidence_for(evidence_ids: list[str], card: CardContent) -> list[dict]:
    by_id = {f["id"]: f for f in card.facts()}
    return [
        {
            "fact_id": fid,
            "source": by_id[fid]["source"],
            "ref": by_id[fid]["label"],
            "date": by_id[fid]["date"],
            "excerpt": by_id[fid]["value"],
        }
        for fid in dict.fromkeys(evidence_ids)
        if fid in by_id
    ]


def generate_nba(client: Any, model: str, deal_name: str, card: CardContent) -> NbaResult:
    """Never raises: API failures and rejected drafts come back on the result."""
    known = {f["id"] for f in card.facts()}
    messages: list[dict] = [{"role": "user", "content": build_user_message(deal_name, card)}]
    problems: list[str] = []

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = client.messages.parse(
                model=model,
                max_tokens=16000,
                system=SYSTEM_PROMPT,
                thinking={"type": "adaptive"},
                messages=messages,
                output_format=NbaDraft,
            )
        except anthropic.APIStatusError as exc:
            logger.warning("Claude returned %s for %s", exc.status_code, deal_name)
            return NbaResult(error=f"Claude API error {exc.status_code}: {exc.message}", model=model)
        except anthropic.APIConnectionError as exc:
            logger.warning("Could not reach Claude for %s", deal_name)
            return NbaResult(error=f"Could not reach Claude: {exc}", model=model)

        if response.stop_reason == "refusal":
            return NbaResult(error="Claude declined to draft an action for this deal.", model=model)
        draft = response.parsed_output
        if draft is None:
            return NbaResult(error=f"Claude returned no usable draft (stop reason: {response.stop_reason}).", model=model)

        problems = check_nba(draft.objective, draft.action, draft.why_now, draft.evidence_ids, known)
        if not problems:
            return NbaResult(draft=draft, evidence=evidence_for(draft.evidence_ids, card), model=model)

        logger.info("Draft %d for %s rejected: %s", attempt, deal_name, problems)
        messages += [
            {"role": "assistant", "content": response.content},
            {"role": "user", "content": "That draft cannot be saved: " + "; ".join(problems) + ". Fix it and try again."},
        ]

    return NbaResult(problems=problems, error="No action met the evidence and length rules.", model=model)

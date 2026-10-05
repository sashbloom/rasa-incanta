"""Draft one next best action for one deal with Claude, then check it before it is saved.

Only the facts on the deal's context card go to the model (deal fields today; call and
mail excerpts from Brick 3), never whole mailboxes or transcripts. The model must cite
fact ids; a draft that cites nothing, cites a fact that is not on the card, or breaks
the length rules gets one retry with the problems spelled out, and is dropped after
that. No evidence means no NBA.

Prompt caching: everything that is the same for every deal (the instructions, the Ideas Treasury
when one is loaded, and the compose rules) is one system block marked for Anthropic's prompt cache,
and only the deal's context card goes after it. The run drafts one deal first so the rest read the
cache instead of each writing it. The block must stay byte-identical across calls: nothing per
deal, per run or per day may be interpolated into it.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import anthropic
from pydantic import BaseModel

from api.domain.gaps import gap_copy
from api.domain.nba_rules import MAX_ACTION_WORDS, MAX_WHY_NOW_WORDS, check_nba
from api.engine.context import SIGNALS, CardContent
from api.icp import tracing

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 2


class NbaDraft(BaseModel):
    objective: Literal["advance", "unblock", "reframe", "nurture", "re_engage"]
    action: str
    why_now: str
    evidence_ids: list[str]
    effort: Literal["low", "medium", "high"]
    treasury_ref: str | None = None


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


SYSTEM_PROMPT = """You recommend next best actions for Practus business development. Practus is a consulting firm; its partners and engagement leads work open opportunities from first prospect to negotiated proposal.

For the deal you are given, write the single most useful action the deal team can take this week.

The context comes in five signals, each a list of sourced facts:
- account_fit: the ICP assessment of the company (recommendation, client and Practus lenses, gates, criteria). Use it to judge how hard to push; it informs the objective but does not dictate it.
- stakeholder: who holds authority and how warm the relationship is, plus web research on the person in the outreach log (their role, public facts, a conversation hook).
- conversation: the Zoho outreach log, Read.ai meetings and Outlook mail, newest first.
- capability: Practus case studies that fit (with why) and Practus SMEs to bring in.
- deal_state: Zoho stage, days in stage, last update, amount, problem statements.

Objectives:
- advance: move the deal to its next stage.
- unblock: resolve an objection or a stakeholder gap that is holding it up.
- reframe: address a higher-priority problem the client has raised.
- nurture: keep an on-hold client engaged so the conversation resumes rather than restarts.
- re_engage: reactivate a deal that has gone quiet."""

COMPOSE_RULES = f"""Rules:
- Use only the facts provided. Every claim in the action and the why-now must rest on at least one fact, and evidence_ids must list the ids of those facts exactly as given. Prefer evidence from more than one signal when the facts support it.
- The company is exactly the one on the Zoho record. Only name people who appear in the facts; never suggest going to a more senior or different contact.
- The one exception is Practus's own people named in the Ideas Treasury (Venkat, Deepak, Vamesh, Vivek, Arun, Bimal): an action may bring them in. Client-side people still come only from the facts.
- The people on the deal are its EP involved and EL involved facts. When an action needs a Practus person to lead, join or send it, name them from those facts and cite that fact; do not reach for a stranger. A capability fact labelled "Suggested SME" is someone who is not on the deal: name them only when the deal has no EP or EL fact, and call them a suggested SME, not yet on the deal.
- Cite a case study as proof only when one is in the capability facts.
- Some signals are missing (listed as gaps). Do not imply they exist: with no call logged, do not refer to what was said on a call; with no mail, do not refer to an email; with no case study matched, do not cite a specific case study; with no ICP read, do not claim how the company scores.
- The action is specific and concrete (who does what, with what), at most {MAX_ACTION_WORDS} words.
- why_now is one line, at most {MAX_WHY_NOW_WORDS} words, and says why this week.
- effort is low, medium or high for the Practus team.
- Plain, specific language. No hype."""

# Mahak's ideas-treasury.md, copied verbatim. Ideas are numbered within sections ("## 4. ..." then
# "1. ..."), and an idea's id is section.item ("4.1").
TREASURY_FILE = Path(__file__).resolve().parent / "reference" / "ideas_treasury.md"
_SECTION = re.compile(r"^##\s+(\d+)\.")
_ITEM = re.compile(r"^(\d+)\.\s+")


@lru_cache(maxsize=1)
def load_treasury() -> tuple[str, frozenset[str]]:
    """Mahak's Ideas Treasury (text, idea ids), or ("", {}) without the file. In the text each idea
    carries its full id ("4.1 Offer non-billable ...") so the model can cite it as treasury_ref.
    Inspiration only: it never decides the objective or the action (CLAUDE.md)."""
    try:
        raw = TREASURY_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return "", frozenset()
    lines, ids, section = [], [], None
    for line in raw.splitlines():
        if heading := _SECTION.match(line):
            section = heading.group(1)
        elif section and (item := _ITEM.match(line)):
            ids.append(f"{section}.{item.group(1)}")
            line = f"{ids[-1]} {line[item.end():]}"
        lines.append(line)
    return "\n".join(lines), frozenset(ids)


@lru_cache(maxsize=1)
def system_blocks() -> tuple[dict, ...]:
    """The cached prefix, identical for every deal: instructions, the treasury, the compose rules."""
    treasury, _ = load_treasury()
    if treasury:
        middle = ("Ideas Treasury: Mahak's library of engagement plays. Use it for inspiration only; the facts "
                  "decide the action. Set treasury_ref to the number of the closest idea (for example \"4.1\") "
                  "when one genuinely resembles your action, otherwise null.\n\n" + treasury)
    else:
        middle = "treasury_ref: always null (no Ideas Treasury is loaded)."
    text = "\n\n".join((SYSTEM_PROMPT, middle, COMPOSE_RULES))
    return ({"type": "text", "text": text, "cache_control": {"type": "ephemeral"}},)


def build_user_message(deal_name: str, card: CardContent) -> str:
    """The deal's facts grouped by signal, plus its gaps. Only card facts go to the model."""
    def slim(f: dict) -> dict:
        return {k: f[k] for k in ("id", "label", "value", "date") if f.get(k) is not None}

    payload = {
        "deal": deal_name,
        "signals": {s: [slim(f) for f in (getattr(card, s) or {}).get("facts", [])] for s in SIGNALS},
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
            with tracing.observe_generation(f"nba: {deal_name} (attempt {attempt}/{MAX_ATTEMPTS})", model=model,
                                            input=messages, tags=["nba"]) as finish:
                response = client.messages.parse(
                    model=model,
                    max_tokens=16000,
                    system=list(system_blocks()),
                    thinking={"type": "adaptive"},
                    messages=messages,
                    output_format=NbaDraft,
                )
                finish(output=getattr(response.parsed_output, "model_dump", lambda: None)(),
                       usage=getattr(response, "usage", None))
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
        if draft.treasury_ref not in load_treasury()[1]:
            draft.treasury_ref = None  # a reference to an idea that is not in the treasury is dropped
        if not problems:
            return NbaResult(draft=draft, evidence=evidence_for(draft.evidence_ids, card), model=model)

        logger.info("Draft %d for %s rejected: %s", attempt, deal_name, problems)
        messages += [
            {"role": "assistant", "content": response.content},
            {"role": "user", "content": "That draft cannot be saved: " + "; ".join(problems) + ". Fix it and try again."},
        ]

    return NbaResult(problems=problems, error="No action met the evidence and length rules.", model=model)

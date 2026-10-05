"""The `account_fit` and `stakeholder` signals, from finished ICP scores in the shared ICP Postgres.

We no longer score companies ourselves: `sources/icp_shared.py` reads the newest finished run of each
company (icp.runs joined to icp.run_criterion_scores) and this module turns it into the two signals.
A company with no finished run has neither (the card gets the `no_icp` gap).

account_fit: the recommendation, both lenses, every fired gate, and each criterion with evidence
(score, label, trimmed rationale). stakeholder: authority (C1) and warmth and threading (C2), which are
left out of account_fit so they are not listed twice.

`company_key` and the criterion display names come from the ICP engine's pure helpers in api/icp/,
which is otherwise unwired: nothing here calls its scoring or any web research.
"""
from __future__ import annotations

import html

from api.icp import criteria_tables as ct
from api.icp.entity_resolver import normalize_for_matching
from api.sources.icp_shared import SharedScore

MAX_RATIONALE_CHARS = 280
STAKEHOLDER_CRITERIA = ("C1", "C2")


def company_key(name: str | None) -> str:
    return normalize_for_matching(name or "") or (name or "").strip().lower()


def _trim(text: str, limit: int = MAX_RATIONALE_CHARS) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "..."


def _name(cid: str) -> str:
    return html.unescape(ct.CRITERION_DISPLAY_NAMES.get(cid, cid))


def _label(label: str | None) -> str:
    return (label or "").replace("_", " ")


def _fact(key: str, label: str, value: str, day: str) -> dict:
    return {"id": f"icp.{key}", "source": "icp", "label": label, "value": value, "date": day}


def signals(score: SharedScore) -> tuple[dict, dict]:
    """(account_fit, stakeholder) for one finished shared run."""
    day = score.generated_at.date().isoformat()
    if score.status == "gate_1_stopped":
        return ({"status": "gate_1_stopped", "run_id": score.run_id, "facts": [
            _fact("gate_1", "Not scored", f"ICP could not score this company: {_trim(score.stop_reason or '')}", day)]}, {})

    rec = f"ICP recommendation: {score.recommendation}" + (" (provisional: too few client criteria had evidence)"
                                                           if score.provisional else "")
    if score.recommendation_reason:
        rec += f". {_trim(score.recommendation_reason)}"
    client = (f"Client lens: {score.client_verdict} (provisional)" if score.provisional
              else f"Client lens {score.client_total}/50, {score.client_verdict}")
    facts = [_fact("recommendation", "Recommendation", rec, day), _fact("client_lens", "Client lens", client, day)]
    if score.practus_total is not None:
        facts.append(_fact("practus_lens", "Practus lens", f"Practus lens {score.practus_total}/50, {score.practus_verdict}", day))
    facts += [_fact(f"gate_{g['gate_id']}", f"Gate {g['gate_id']}",
                    f"Gate {g['gate_id']} ({g['name']}) fired: {_trim(g['detail'] or '')}", day) for g in score.gates]
    for c in score.criteria:
        if c["criterion_id"] in STAKEHOLDER_CRITERIA or c["data_gap"] or c["is_na"] or c["raw_score"] is None:
            continue
        name = _name(c["criterion_id"])
        facts.append(_fact(f"criterion_{c['criterion_id']}", name,
                           f"{name} {c['raw_score']}/5 ({_label(c['condition_label'])}): {_trim(c['rationale'] or '')}", day))
    account_fit = {
        "status": "scored", "recommendation": score.recommendation, "provisional": score.provisional,
        "client_total": score.client_total, "client_verdict": score.client_verdict,
        "practus_total": score.practus_total, "practus_verdict": score.practus_verdict,
        "gates_fired": [g["gate_id"] for g in score.gates],
        "run_id": score.run_id, "computed_at": score.generated_at.isoformat(), "facts": facts,
    }

    stakeholder_facts = []
    by_id = {c["criterion_id"]: c for c in score.criteria}
    for cid, key in (("C1", "authority"), ("C2", "warmth")):
        c = by_id.get(cid)
        if c is None or c["data_gap"] or c["is_na"]:
            continue
        stakeholder_facts.append(_fact(key, _name(cid), f"{_name(cid)}: {_label(c['condition_label'])}. {_trim(c['rationale'] or '')}", day))
    return account_fit, ({"facts": stakeholder_facts} if stakeholder_facts else {})

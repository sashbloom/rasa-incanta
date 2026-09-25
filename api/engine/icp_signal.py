"""The `account_fit` and `stakeholder` signals, from the ICP bot's scoring (api/icp/).

A company is scored once and the result reused for ICP_CACHE_DAYS (CLAUDE.md: 4 weeks). Rows in
`company_icp` are append-only; failures are recorded but never reused, so they retry next run.

account_fit: the recommendation, both lenses (with group sub-verdicts), every fired gate, and
each criterion with evidence (score, label, trimmed rationale). stakeholder: authority (C1) and
warmth and threading (C2), which are left out of account_fit so they are not listed twice.
"""
from __future__ import annotations

import html
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.icp import criteria_tables as ct
from api.icp.entity_resolver import normalize_for_matching
from api.icp.pipeline import CompanyScore
from api.models import CompanyIcp

REUSABLE = ("scored", "gate_1_stopped")
MAX_RATIONALE_CHARS = 280
STAKEHOLDER_CRITERIA = ("C1", "C2")


def company_key(name: str | None) -> str:
    return normalize_for_matching(name or "") or (name or "").strip().lower()


def fresh_icp(session: Session, key: str, now: datetime, days: int) -> CompanyIcp | None:
    """The newest reusable scoring of this company younger than `days`, if any."""
    cutoff = now - timedelta(days=days)
    for row in session.scalars(select(CompanyIcp).where(CompanyIcp.company_key == key)
                               .order_by(CompanyIcp.computed_at.desc())):
        computed = row.computed_at if row.computed_at.tzinfo else row.computed_at.replace(tzinfo=timezone.utc)
        if computed < cutoff:
            return None
        if row.status in REUSABLE:
            return row
    return None


def _trim(text: str, limit: int = MAX_RATIONALE_CHARS) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "..."


def _name(cid: str) -> str:
    return html.unescape(ct.CRITERION_DISPLAY_NAMES.get(cid, cid))


def _label(label: str | None) -> str:
    return (label or "").replace("_", " ")


def _fact(key: str, label: str, value: str, day: str) -> dict:
    return {"id": f"icp.{key}", "source": "icp", "label": label, "value": value, "date": day}


def signals(result: CompanyScore, computed_at: datetime) -> tuple[dict, dict]:
    """(account_fit, stakeholder) for a finished scoring."""
    day = computed_at.date().isoformat()
    if result.status == "gate_1_stopped":
        return ({"status": "gate_1_stopped", "facts": [
            _fact("gate_1", "Not scored", f"ICP could not score this company: {_trim(result.stop_reason or '')}", day)]}, {})

    s = result.score
    rec = f"ICP recommendation: {s.recommendation}" + (" (provisional: too few client criteria had evidence)"
                                                        if s.provisional else "")
    if s.recommendation_reason:
        rec += f". {_trim(s.recommendation_reason)}"
    groups = ", ".join(f"{ct.GROUP_SHORT_NAMES.get(g.name, g.name)} {g.subtotal}/{g.max_points} {g.sub_verdict}"
                       for g in s.client_groups)
    client = (f"Client lens: {s.client_verdict} (provisional)" if s.provisional
              else f"Client lens {s.client_total}/50, {s.client_verdict}: {groups}")
    facts = [
        _fact("recommendation", "Recommendation", rec, day),
        _fact("client_lens", "Client lens", client, day),
        _fact("practus_lens", "Practus lens", f"Practus lens {s.practus_total}/50, {s.practus_verdict}", day),
    ]
    facts += [_fact(f"gate_{g.gate_id}", f"Gate {g.gate_id}", f"Gate {g.gate_id} ({g.name}) fired: {_trim(g.detail)}", day)
              for g in s.gates if g.fired]
    criteria = [c for g in s.client_groups for c in g.criteria] + list(s.practus_criteria)
    for c in criteria:
        if c.criterion_id in STAKEHOLDER_CRITERIA or c.data_gap or c.is_na or c.raw_score is None:
            continue
        facts.append(_fact(f"criterion_{c.criterion_id}", _name(c.criterion_id),
                           f"{_name(c.criterion_id)} {c.raw_score}/5 ({_label(c.condition_label)}): {_trim(c.rationale)}", day))
    account_fit = {
        "status": "scored", "recommendation": s.recommendation, "provisional": s.provisional,
        "client_total": s.client_total, "client_verdict": s.client_verdict,
        "practus_total": s.practus_total, "practus_verdict": s.practus_verdict,
        "gates_fired": [g.gate_id for g in s.gates if g.fired],
        "ownership_control": getattr(result.bundle.entity.ownership_control, "value", None),
        "computed_at": computed_at.isoformat(), "facts": facts,
    }

    stakeholder_facts = []
    for cid, key in (("C1", "authority"), ("C2", "warmth")):
        interp = result.interpretations.get(cid)
        if interp is None or interp.data_gap:
            continue
        stakeholder_facts.append(_fact(key, _name(cid),
                                       f"{_name(cid)}: {_label(interp.condition_label)}. {_trim(interp.rationale)}", day))
    stakeholder = {"facts": stakeholder_facts} if stakeholder_facts else {}
    return account_fit, stakeholder


def record(session: Session, company_name: str, computed_at: datetime, result: CompanyScore | None = None,
           error: str | None = None) -> CompanyIcp:
    """Append one scoring (or failure) to company_icp."""
    if result is None:
        row = CompanyIcp(company_key=company_key(company_name), company_name=company_name, computed_at=computed_at,
                         status="failed", error=error)
    else:
        account_fit, stakeholder = signals(result, computed_at)
        full = {"interpretations": {k: v.model_dump(mode="json") for k, v in result.interpretations.items()},
                "score": result.score.model_dump(mode="json") if result.score else None,
                "stop_reason": result.stop_reason}
        row = CompanyIcp(company_key=company_key(company_name), company_name=company_name, computed_at=computed_at,
                         status=result.status, account_fit=account_fit, stakeholder=stakeholder, result=full,
                         data_gaps=list(result.bundle.data_gaps))
    session.add(row)
    return row

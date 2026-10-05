"""Finished ICP scores, read from the Postgres shared with the ICP service. We are a reader only.

Tables (docs/ICP_Postgres_Schema.md): `icp.runs` (one row per finished run, joined here to
`icp.run_criterion_scores` for the criteria breakdown) and `icp.run_gates`. Nothing is ever written
back, and the connection is read-only. `icp.run_files` (binary report blobs) is never touched, and
neither are the heavy `runs` columns (`html_content`, `markdown_content`, `input_data`).

A company is matched on `engine.icp_signal.company_key` (case- and legal-suffix-insensitive) against
`runs.company_name`; its newest finished, usable run wins. A company with no such run simply has no
entry: the card gets the `no_icp` gap and the run goes on. Nothing here raises; an unset or
unreachable database comes back as `ok=False`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

import psycopg
from psycopg.rows import dict_row

from api.config import Settings

logger = logging.getLogger(__name__)

# A run we can use: it finished without error and either stopped at Gate 1 or produced a verdict.
RUNS = """
SELECT run_id, company_name, generated_at
FROM icp.runs
WHERE done AND error IS NULL AND (gate_1_stopped OR recommendation IS NOT NULL)
ORDER BY generated_at DESC"""

# icp.runs joined to its criteria. Explicit columns only: no report text, no input_data.
DETAIL = """
SELECT r.run_id, r.company_name, r.generated_at, r.gate_1_stopped, r.stop_reason, r.recommendation,
       r.recommendation_reason, r.client_total, r.client_verdict, r.practus_total, r.practus_verdict,
       r.provisional,
       c.criterion_id, c.group_name, c.raw_score, c.is_na, c.condition_label, c.rationale, c.data_gap
FROM icp.runs r
LEFT JOIN icp.run_criterion_scores c ON c.run_id = r.run_id
WHERE r.run_id = ANY(%(ids)s)
ORDER BY r.run_id, c.criterion_id"""

GATES = """
SELECT run_id, gate_id, name, fired, detail
FROM icp.run_gates
WHERE run_id = ANY(%(ids)s) AND fired
ORDER BY run_id, gate_id"""

CRITERION_FIELDS = ("criterion_id", "group_name", "raw_score", "is_na", "condition_label", "rationale", "data_gap")


@dataclass
class SharedScore:
    company_name: str
    run_id: str
    generated_at: datetime
    status: str  # "scored" or "gate_1_stopped"
    recommendation: str | None = None
    recommendation_reason: str | None = None
    client_total: int | None = None
    client_verdict: str | None = None
    practus_total: int | None = None
    practus_verdict: str | None = None
    provisional: bool = False
    stop_reason: str | None = None
    criteria: list[dict] = field(default_factory=list)  # CRITERION_FIELDS
    gates: list[dict] = field(default_factory=list)  # fired gates: gate_id, name, detail


@dataclass
class SharedResult:
    ok: bool
    scores: dict[str, SharedScore] = field(default_factory=dict)  # by company key
    error: str | None = None


def configured(settings: Settings) -> bool:
    return bool(settings.icp_shared_db_url.strip())


def _connect(settings: Settings) -> psycopg.Connection:
    conn = psycopg.connect(settings.icp_shared_db_url.strip(), sslmode="require", connect_timeout=10,
                           row_factory=dict_row)
    conn.read_only = True  # belt and braces: this connection cannot write even if a query tried
    return conn


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def load_scores(settings: Settings, companies: dict[str, str],
                connect_fn: Callable[[Settings], Any] = _connect) -> SharedResult:
    """The newest usable score of each company. `companies` maps company key -> name; only keys
    with a finished run appear in the result."""
    from api.engine.icp_signal import company_key  # local: icp_signal imports SharedScore from here

    if not configured(settings):
        return SharedResult(ok=False, error="ICP_SHARED_DB_URL is not set.")
    if not companies:
        return SharedResult(ok=True)
    try:
        with connect_fn(settings) as conn, conn.cursor() as cur:
            cur.execute(RUNS)
            newest: dict[str, str] = {}  # company key -> run id; rows come newest first
            for row in cur.fetchall():
                key = company_key(row["company_name"])
                if key in companies and key not in newest:
                    newest[key] = row["run_id"]
            if not newest:
                return SharedResult(ok=True)
            ids = list(newest.values())
            cur.execute(DETAIL, {"ids": ids})
            detail = cur.fetchall()
            cur.execute(GATES, {"ids": ids})
            gates = cur.fetchall()
    except Exception as exc:
        logger.warning("Shared ICP database could not be read (%s)", type(exc).__name__)
        return SharedResult(ok=False, error=type(exc).__name__)

    by_run: dict[str, SharedScore] = {}
    for row in detail:
        score = by_run.get(row["run_id"])
        if score is None:
            score = by_run[row["run_id"]] = SharedScore(
                company_name=row["company_name"], run_id=row["run_id"], generated_at=_aware(row["generated_at"]),
                status="gate_1_stopped" if row["gate_1_stopped"] else "scored",
                recommendation=row["recommendation"], recommendation_reason=row["recommendation_reason"],
                client_total=row["client_total"], client_verdict=row["client_verdict"],
                practus_total=row["practus_total"], practus_verdict=row["practus_verdict"],
                provisional=bool(row["provisional"]), stop_reason=row["stop_reason"])
        if row["criterion_id"]:
            score.criteria.append({k: row[k] for k in CRITERION_FIELDS})
    for gate in gates:
        if gate["run_id"] in by_run:
            by_run[gate["run_id"]].gates.append({k: gate[k] for k in ("gate_id", "name", "detail")})
    return SharedResult(ok=True, scores={key: by_run[run_id] for key, run_id in newest.items() if run_id in by_run})

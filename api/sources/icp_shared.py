"""The ICP scores shared between Practus agents: one Postgres, one append-only `company_icp` table.

Before scoring a company ourselves we look here for a score younger than ICP_CACHE_DAYS; after
scoring one we add it so other agents can reuse it. Only usable scorings are shared (`scored` and
`gate_1_stopped`, never failures), and a score made without Exa research is not reused once
EXA_API_KEY is set, same as our local cache. Rows are only ever inserted.

Nothing here raises: an unset or unreachable database comes back as `ok=False` and the run falls
back to the local `company_icp` table and its own scoring.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from api.config import Settings

logger = logging.getLogger(__name__)

AGENT = "rasa-incanta"
REUSABLE = ("scored", "gate_1_stopped")

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS company_icp (
    id uuid PRIMARY KEY,
    company_key text NOT NULL,
    company_name text NOT NULL,
    computed_at timestamptz NOT NULL,
    status text NOT NULL,
    account_fit jsonb NOT NULL DEFAULT '{}',
    stakeholder jsonb NOT NULL DEFAULT '{}',
    result jsonb NOT NULL DEFAULT '{}',
    data_gaps jsonb NOT NULL DEFAULT '[]',
    source_agent text NOT NULL
)"""
CREATE_INDEX = "CREATE INDEX IF NOT EXISTS ix_company_icp_key_computed ON company_icp (company_key, computed_at DESC)"

SELECT = """
SELECT company_name, computed_at, status, account_fit, stakeholder, result, data_gaps
FROM company_icp
WHERE company_key = %(key)s AND computed_at >= %(since)s AND status = ANY(%(statuses)s)
ORDER BY computed_at DESC
LIMIT 5"""

INSERT = """
INSERT INTO company_icp (id, company_key, company_name, computed_at, status, account_fit, stakeholder,
                         result, data_gaps, source_agent)
VALUES (%(id)s, %(key)s, %(name)s, %(computed_at)s, %(status)s, %(account_fit)s, %(stakeholder)s,
        %(result)s, %(data_gaps)s, %(agent)s)"""


@dataclass
class SharedScore:
    company_name: str
    computed_at: datetime
    status: str
    account_fit: dict
    stakeholder: dict
    result: dict
    data_gaps: list


@dataclass
class Lookup:
    ok: bool
    score: SharedScore | None = None
    error: str | None = None


def configured(settings: Settings) -> bool:
    return bool(settings.icp_shared_db_url.strip())


def _connect(settings: Settings) -> psycopg.Connection:
    return psycopg.connect(settings.icp_shared_db_url.strip(), connect_timeout=10, row_factory=dict_row)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def find(settings: Settings, key: str, now: datetime, days: int, *, require_exa: bool = False,
         connect_fn: Callable[[Settings], Any] = _connect) -> Lookup:
    """The newest reusable shared score of this company younger than `days`, if any."""
    if not configured(settings):
        return Lookup(ok=False, error="ICP_SHARED_DB_URL is not set.")
    try:
        with connect_fn(settings) as conn, conn.cursor() as cur:
            cur.execute(SELECT, {"key": key, "since": now - timedelta(days=days), "statuses": list(REUSABLE)})
            rows = cur.fetchall()
    except Exception as exc:
        logger.warning("Shared ICP database could not be read (%s)", type(exc).__name__)
        return Lookup(ok=False, error=f"{type(exc).__name__}")
    for row in rows:
        if require_exa and not (row["result"] or {}).get("exa_research"):
            continue
        return Lookup(ok=True, score=SharedScore(
            company_name=row["company_name"], computed_at=_aware(row["computed_at"]), status=row["status"],
            account_fit=row["account_fit"] or {}, stakeholder=row["stakeholder"] or {},
            result=row["result"] or {}, data_gaps=row["data_gaps"] or []))
    return Lookup(ok=True)


def publish(settings: Settings, key: str, score: SharedScore,
            connect_fn: Callable[[Settings], Any] = _connect) -> str | None:
    """Add a scoring for other agents. None on success, else why it could not be shared."""
    if not configured(settings):
        return "ICP_SHARED_DB_URL is not set."
    if score.status not in REUSABLE:
        return None
    try:
        with connect_fn(settings) as conn:
            with conn.cursor() as cur:
                cur.execute(CREATE_TABLE)
                cur.execute(CREATE_INDEX)
                cur.execute(INSERT, {
                    "id": uuid.uuid4(), "key": key, "name": score.company_name, "computed_at": score.computed_at,
                    "status": score.status, "account_fit": Jsonb(score.account_fit),
                    "stakeholder": Jsonb(score.stakeholder), "result": Jsonb(score.result),
                    "data_gaps": Jsonb(score.data_gaps), "agent": AGENT})
            conn.commit()
    except Exception as exc:
        logger.warning("Shared ICP database could not be written (%s)", type(exc).__name__)
        return type(exc).__name__
    return None

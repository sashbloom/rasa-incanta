"""Setu (the Wisible mirror, `wisible_data`): Practus case studies, read-only.

Case studies are `knowledge_chunks` rows with `source_type = 'case_study'`: the title is
`entity_name`, the prose (problem, service, impact) is `content`, and `metadata` carries
`industry`, `service_line` and `service_offered`. There is no stable id and titles can repeat,
so each chunk is kept as its own case study. Setu's chat API is not used: the ICP bot retired
it as unreliable and reads these tables directly, as we do.

Read-only by construction, like the Zoho source: every transaction is READ ONLY. A failure is
reported on the result, never raised, so a Setu outage only costs the capability signal.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg.rows import dict_row

from api.config import Settings

logger = logging.getLogger(__name__)

CASE_STUDIES_SQL = """
    SELECT entity_name, content,
           metadata->>'industry' AS industry,
           metadata->>'service_line' AS service_line,
           metadata->>'service_offered' AS service_offered
      FROM knowledge_chunks
     WHERE source_type = 'case_study'
     ORDER BY entity_name
"""


@dataclass(frozen=True)
class CaseStudy:
    name: str
    content: str = ""
    industry: str | None = None
    service_line: str | None = None
    service_offered: str | None = None


@dataclass
class SetuResult:
    case_studies: list[CaseStudy] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _clean(value: Any) -> str | None:
    text = " ".join(str(value).split()) if value is not None else ""
    return None if text.lower() in {"", "nan", "none", "null"} else text


def row_to_case_study(row: dict) -> CaseStudy | None:
    name = _clean(row.get("entity_name"))
    if not name:
        return None
    return CaseStudy(
        name=name,
        content=(row.get("content") or "").strip(),
        industry=_clean(row.get("industry")),
        service_line=_clean(row.get("service_line")),
        service_offered=_clean(row.get("service_offered")),
    )


Connect = Callable[[Settings], Any]


def connect(settings: Settings) -> psycopg.Connection:
    conn = psycopg.connect(**settings.setu_db, connect_timeout=15, row_factory=dict_row)
    conn.read_only = True  # every transaction on this connection is READ ONLY
    return conn


def fetch_case_studies(settings: Settings, connect_fn: Connect = connect) -> SetuResult:
    """Every Setu case study. Never raises: failures come back as `error`."""
    if settings.setu_db is None:
        return SetuResult(error="Setu is not configured: set SETU_DB_URL, or SETU_PGHOST, SETU_PGUSER and SETU_PGPASSWORD.")
    try:
        with connect_fn(settings) as conn, conn.cursor() as cur:
            cur.execute(CASE_STUDIES_SQL)
            rows = cur.fetchall()
    except Exception as exc:
        # Shown on the open API, so only the kind of failure; the detail stays in the server log.
        logger.exception("Setu read failed")
        return SetuResult(error=f"Setu read failed ({type(exc).__name__}). Details are in the server log.")
    studies = [cs for cs in (row_to_case_study(dict(r)) for r in rows) if cs]
    return SetuResult(case_studies=studies)

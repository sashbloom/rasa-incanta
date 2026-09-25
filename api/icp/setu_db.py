# Copied from icp-bot/backend/domains/mahak/people/mahak/icp/setu_db.py (read-only reference repo).
# Only imports and plumbing are adapted for Rasa Incanta; the logic is the ICP bot's.
"""Read-only Postgres client against `wisible_data`, the Postgres mirror
behind Setu's own chat agent. Schema confirmed directly against the live
database (2026-08-31): 8 tables — customers, employees, file_ingestion_log,
knowledge_chunks, org_units, projects, resource_allocations, timesheets.

Built specifically to replace the P3 "team" Setu chat question, which was
unreliable in production — see setu_questions.p3_team_question()'s
docstring for the full comparison (0/5 real runs via chat vs. this direct,
deterministic path). Industries/service_lines/grade are structured JSON
fields on `knowledge_chunks` here, not something that needs an LLM agent to
search and synthesize; this module just reads them.

Read-only by construction, same as zoho_db.py: the connection itself opens
with `default_transaction_read_only=on` as a client-side guarantee
independent of the DB-side grants.
"""

from __future__ import annotations

import re
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from api.icp.compat import get_settings

# icp-skill.md P3: "Grades in scope: EP - n · EL - n · TL - n." Confirmed
# live that `employees.role` also has 'Engagement Manager'/'Team Member' and
# knowledge_chunks grades also include 'EM - n'/'TM - n' — both explicitly
# out of scope for P3 fielding.
_ROLES_IN_SCOPE = ("Engagement Partner", "Engagement Leader", "Team Lead")
_GRADE_PREFIXES_IN_SCOPE = ("EP", "EL", "TL")
_ROLE_TO_GRADE_LABEL = {
    "Engagement Partner": "EP",
    "Engagement Leader": "EL",
    "Team Lead": "TL",
}


@contextmanager
def get_connection():
    settings = get_settings()
    dsn = settings.setu_db_dsn
    conn = psycopg.connect(dsn, connect_timeout=15, options="-c default_transaction_read_only=on", row_factory=dict_row)
    try:
        yield conn
    finally:
        conn.close()


def normalize_person_name(name: str) -> str:
    """Resume/skill-profile source files are frequently named after the
    person plus a suffix that doesn't match `employees.employee_name`
    exactly — confirmed live: "Fenil Bhimani2", "Divakar Gupta2",
    "Bhoomi Vaghani (2)", "CV_Sonal Goel". Strips trailing digits/
    parenthetical counters and common CV/resume-file noise so a fuzzy join
    still finds them, the same normalize-then-join pattern already used in
    entity_resolver.py/practus_history.py for company names."""
    text = (name or "").strip()
    text = re.sub(r"\s*\(\d+\)\s*$", "", text)
    text = re.sub(r"\d+\s*$", "", text)
    text = re.sub(r"^(cv[_\s]*|resume[_\s]*)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"[_\-]+", " ", text)
    return " ".join(text.split()).strip().lower()


def fetch_ep_el_tl_roster() -> list[dict]:
    """Every EP/EL/TL person, from BOTH `employees.role` and any
    knowledge_chunks `employee` chunk with an EP/EL/TL grade that's missing
    from `employees` entirely — icp-skill.md's own instruction: "Cross-check
    run_sql on employees.role — never the filter... Union both." One row
    per person: name, grade_label, status, employee_code (None for a
    knowledge-only person, who therefore can't be joined to staffing
    history in fetch_named_clients)."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT employee_code, employee_name, role, status FROM employees WHERE role = ANY(%s)",
            (list(_ROLES_IN_SCOPE),),
        )
        roster = [
            {
                "employee_code": row["employee_code"],
                "name": row["employee_name"],
                "grade_label": _ROLE_TO_GRADE_LABEL[row["role"]],
                "status": row["status"],
            }
            for row in cur.fetchall()
        ]
        known_names = {r["name"] for r in roster}

        cur.execute(
            "SELECT entity_name, metadata->>'grade' AS grade, content FROM knowledge_chunks "
            "WHERE source_type = 'employee' "
            "AND (metadata->>'grade' ILIKE 'EP%%' OR metadata->>'grade' ILIKE 'EL%%' OR metadata->>'grade' ILIKE 'TL%%')"
        )
        for row in cur.fetchall():
            if row["entity_name"] in known_names:
                continue
            grade = (row["grade"] or "").upper()
            grade_prefix = next((p for p in _GRADE_PREFIXES_IN_SCOPE if grade.startswith(p)), None)
            if not grade_prefix:
                continue
            roster.append(
                {
                    "employee_code": None,
                    "name": row["entity_name"],
                    "grade_label": grade_prefix,
                    "status": "Inactive" if "Status: Inactive" in (row["content"] or "") else "Active",
                }
            )
        return roster


def fetch_skill_profiles() -> dict[str, dict]:
    """normalize_person_name(name) -> {"industries": [...], "service_lines": [...]}.
    Confirmed live: only ~13 of 23 EP/EL/TL people have a skill_profile
    chunk at all — callers must treat a missing entry as "no documented
    profile", not as "no industries", the same distinction the original
    Desktop/EP-EL project's matching.py already draws."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT entity_name, metadata->'industries' AS industries, metadata->'service_lines' AS service_lines "
            "FROM knowledge_chunks WHERE source_type = 'skill_profile'"
        )
        return {
            normalize_person_name(row["entity_name"]): {
                "industries": row["industries"] or [],
                "service_lines": row["service_lines"] or [],
            }
            for row in cur.fetchall()
        }


def fetch_resume_text() -> dict[str, str]:
    """normalize_person_name(name) -> concatenated resume content, for
    prior-career keyword search — icp-skill.md P3's uplift explicitly
    requires checking for this ("prior-career experience... directly
    relevant"), and this free-text content is exactly where the reference
    report's "consultancy experience in... Wine-yard" match for the real
    Sula Wines deal actually lives (confirmed live, under the source file's
    literal entity_name "Fenil Bhimani2")."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT entity_name, content FROM knowledge_chunks WHERE source_type = 'resume'")
        merged: dict[str, list[str]] = {}
        for row in cur.fetchall():
            merged.setdefault(normalize_person_name(row["entity_name"]), []).append(row["content"] or "")
        return {name: "\n".join(chunks) for name, chunks in merged.items()}


def fetch_named_clients(employee_codes: list[str]) -> dict[str, list[str]]:
    """employee_code -> sorted real customer names actually staffed on, via
    resource_allocations -> projects -> customers — ground-truth delivery
    history, not a free-text inference."""
    codes = [c for c in employee_codes if c]
    if not codes:
        return {}
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT ra.employee_code, c.customer_name
            FROM resource_allocations ra
            JOIN projects p ON p.project_code = ra.project_code
            JOIN customers c ON c.customer_id = p.customer_id
            WHERE ra.employee_code = ANY(%s)
            """,
            (codes,),
        )
        grouped: dict[str, set[str]] = {}
        for row in cur.fetchall():
            grouped.setdefault(row["employee_code"], set()).add(row["customer_name"])
        return {code: sorted(names) for code, names in grouped.items()}


def fetch_partner_profiles() -> list[dict]:
    """Every `partner_profile` entity as one merged record: {entity_name,
    content}. Confirmed live: `partner_profile` chunks (515 chunks / 85
    distinct entities) carry NO structured industry/service_line tag at all
    — `metadata` is just `{"chunk": N}` — unlike `case_study` or
    `skill_profile`. Merged per entity_name the same way fetch_resume_text()
    merges a person's chunks, since a profile is chunked across multiple
    rows; kept as a list (not a dict keyed by normalized name) because,
    unlike people, there's no need to fuzzy-join partner names against
    another table — the raw entity_name is exactly what a human/LLM reader
    needs to see."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT entity_name, content FROM knowledge_chunks WHERE source_type = 'partner_profile'")
        merged: dict[str, list[str]] = {}
        for row in cur.fetchall():
            merged.setdefault(row["entity_name"], []).append(row["content"] or "")
        return [{"entity_name": name, "content": "\n".join(chunks)} for name, chunks in merged.items()]


def fetch_case_studies() -> list[dict]:
    """Every `case_study` chunk — name, full content (Problem/Service/
    Impact/Team, where recorded), and its structured industry/service_line
    tags. Confirmed live: `metadata` already carries clean `industry`/
    `service_line` values for most case studies (unlike skill_profile's
    JSON arrays, this is a single string per case study), and `content`
    already includes an explicit "Impact:" line where one was recorded —
    icp-skill.md P2: "Never invent an impact number — where Impact is
    blank, describe the scope of work instead" is directly checkable
    against this field rather than something a chat answer has to recall
    correctly on its own."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT entity_name, content, metadata->>'industry' AS industry, "
            "metadata->>'service_line' AS service_line FROM knowledge_chunks "
            "WHERE source_type = 'case_study'"
        )
        return cur.fetchall()

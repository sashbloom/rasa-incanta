"""Zoho CRM deals, read from the read-only Postgres copy of Zoho (`zoho_data`).

Read-only by construction: the connection is opened with `read_only = True`, so every
transaction is READ ONLY and the server rejects any write.

The copy keeps its tables in `public`: `deals` (one row per Potential, snake_case
columns such as `deal_name`, `stage`, `ep_involved`, `date_proposal_sent`), with the
owner in `users` (via `owner_id`) and the company in `accounts` (via `account_id`).
Column names are read from `information_schema` first, so a column the copy lacks
is simply absent from the deal instead of failing the query. That matters most for
the contact, whose shape we have not seen yet: `contact_name` on the deal, or a
`contacts` table joined by `contact_id`.

Company and contact come only from this record. Nothing here substitutes a
more senior contact or a different company.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.rows import dict_row

from api.config import Settings
from api.domain.stages import IN_SCOPE_STAGES

logger = logging.getLogger(__name__)

# Values that look like data but mean "missing" (seen in the ICP bot's sync of the same copy).
_PLACEHOLDERS = {
    "", "others", "not identified", "unidentified", "unidentified el", "unidentified ep",
    "n/a", "na", "-", "none", "null",
}

# Stage -> the column recording when the deal entered it (same mapping the ICP bot uses).
STAGE_ENTRY_COLUMN = {
    "prospect": "date_became_potential",
    "need identification": "date_became_need_identification",
    "walk-through scheduled/ conducted": "date_walkthrough_scheduled",
    "qualified prospect": "date_became_qualified_prospect",
    "proposal sent": "date_proposal_sent",
    "negotiated proposal sent": "date_proposal_sent",
    "negetiated proposal sent": "date_proposal_sent",
}

# Our field -> candidate column names on `deals`, first match wins.
_COLUMNS: dict[str, tuple[str, ...]] = {
    "name": ("deal_name", "potential_name", "name"),
    "stage": ("stage",),
    "sbu": ("sbu",),
    "city_state": ("city_state",),
    "lead_source": ("potential_lead_source", "lead_source"),
    "industry": ("industry_type", "industry"),
    "ep_involved": ("ep_involved",),
    "el_involved": ("el_involved",),
    "amount": ("amount",),
    "currency": ("currency",),
    "modified_at": ("modified_time", "last_activity_time"),
    "stage_modified_at": ("stage_modified_time",),
    "proposal_sent_on": ("date_on_which_proposal_sent", "date_proposal_sent"),
    "nature_of_potential": ("nature_of_potential",),
    "business_area": ("business_area",),
    "services": ("services",),
    "category": ("category",),
    "client_positioning": ("client_positioning",),
    "client_perception": ("client_perception",),
}
_PROBLEM_COLUMNS = (
    "problem_statement_1", "problem_statement_2", "problem_statement_3", "client_problem_statement",
)
_PROBLEM_AREA_COLUMNS = ("problem_area_1", "problem_area_2", "problem_area_3")


@dataclass(frozen=True)
class ZohoDeal:
    zoho_id: str
    name: str
    stage: str
    account_name: str | None = None
    contact_name: str | None = None
    owner_name: str | None = None
    sbu: str | None = None
    industry: str | None = None
    city_state: str | None = None
    lead_source: str | None = None
    ep_involved: list[str] = field(default_factory=list)
    el_involved: list[str] = field(default_factory=list)
    amount: float | None = None
    currency: str | None = None
    modified_at: datetime | None = None
    stage_entered_at: datetime | None = None
    proposal_sent_on: datetime | None = None
    nature_of_potential: str | None = None
    business_area: str | None = None
    services: str | None = None
    category: str | None = None
    client_positioning: str | None = None
    client_perception: str | None = None
    problem_statements: list[str] = field(default_factory=list)
    problem_areas: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


@dataclass
class ZohoResult:
    deals: list[ZohoDeal] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


# ---------------------------------------------------------------- value cleaning

def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return None if text.lower() in _PLACEHOLDERS else text


def clean_list(value: Any) -> list[str]:
    """Multi-select fields arrive as arrays, JSON-array strings, or ';'/',' separated text."""
    if value is None:
        return []
    items: list[Any]
    if isinstance(value, (list, tuple)):
        items = list(value)
    else:
        text = str(value).strip()
        if text.startswith("["):
            try:
                items = json.loads(text)
            except ValueError:
                items = [text.strip("[]")]
        else:
            items = text.replace(";", ",").split(",")
    cleaned = [clean_text(i.get("name") if isinstance(i, dict) else i) for i in items]
    return [c for c in cleaned if c]


def clean_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=timezone.utc)
    try:
        return clean_datetime(datetime.fromisoformat(str(value)))
    except ValueError:
        return None


def clean_amount(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def jsonable(value: Any) -> Any:
    """Make a database row safe to store in a JSON column."""
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _first(row: dict, names: tuple[str, ...]) -> Any:
    for name in names:
        if row.get(name) not in (None, ""):
            return row[name]
    return None


def row_to_deal(row: dict) -> ZohoDeal:
    """Map one row of the query below into a typed deal."""
    get = {key: _first(row, names) for key, names in _COLUMNS.items()}
    stage = clean_text(get["stage"]) or ""
    entry_column = STAGE_ENTRY_COLUMN.get(stage.lower())
    stage_entered_at = clean_datetime(row.get(entry_column)) if entry_column else None
    return ZohoDeal(
        zoho_id=str(row["id"]),
        name=clean_text(get["name"]) or f"Deal {row['id']}",
        stage=stage,
        account_name=clean_text(_first(row, ("_rel_account_name", "account_name"))),
        contact_name=clean_text(_first(row, ("_rel_contact_name", "contact_name"))),
        owner_name=clean_text(_first(row, ("_rel_owner_name", "owner_name", "owner"))),
        sbu=clean_text(get["sbu"]),
        industry=clean_text(get["industry"]),
        city_state=clean_text(get["city_state"]),
        lead_source=clean_text(get["lead_source"]),
        ep_involved=clean_list(get["ep_involved"]),
        el_involved=clean_list(get["el_involved"]),
        amount=clean_amount(get["amount"]),
        currency=clean_text(get["currency"]),
        modified_at=clean_datetime(get["modified_at"]),
        stage_entered_at=stage_entered_at or clean_datetime(get["stage_modified_at"]),
        proposal_sent_on=clean_datetime(get["proposal_sent_on"]),
        nature_of_potential=clean_text(get["nature_of_potential"]),
        business_area=clean_text(get["business_area"]),
        services=clean_text(get["services"]),
        category=clean_text(get["category"]),
        client_positioning=clean_text(get["client_positioning"]),
        client_perception=clean_text(get["client_perception"]),
        problem_statements=[p for p in (clean_text(row.get(c)) for c in _PROBLEM_COLUMNS) if p],
        problem_areas=[p for p in (clean_text(row.get(c)) for c in _PROBLEM_AREA_COLUMNS) if p],
        raw=jsonable(row),
    )


# ---------------------------------------------------------------- query

_COLUMNS_SQL = """
    SELECT table_name, column_name
      FROM information_schema.columns
     WHERE table_schema = 'public' AND table_name IN ('deals', 'users', 'accounts', 'contacts')
"""


def build_deals_query(columns: dict[str, set[str]]) -> str:
    """SELECT for in-scope deals, joining owner, account and contact only where the columns exist."""
    deals = columns.get("deals", set())
    users = columns.get("users", set())
    accounts = columns.get("accounts", set())
    contacts = columns.get("contacts", set())
    select = ["d.*"]
    joins: list[str] = []

    if "owner_id" in deals and {"id", "full_name"} <= users:
        select.append("u.full_name AS _rel_owner_name")
        joins.append("LEFT JOIN public.users u ON u.id = d.owner_id")
    if "account_id" in deals and {"id", "account_name"} <= accounts:
        select.append("a.account_name AS _rel_account_name")
        joins.append("LEFT JOIN public.accounts a ON a.id = d.account_id")
    if "contact_name" not in deals and "contact_id" in deals and "id" in contacts:
        contact_expr = None
        if "full_name" in contacts:
            contact_expr = "c.full_name"
        elif {"first_name", "last_name"} <= contacts:
            contact_expr = "concat_ws(' ', c.first_name, c.last_name)"
        if contact_expr:  # otherwise no usable name column; the card flags no_contact
            select.append(f"{contact_expr} AS _rel_contact_name")
            joins.append("LEFT JOIN public.contacts c ON c.id = d.contact_id")

    where = ["lower(btrim(d.stage)) = ANY(%(stages)s)"]
    if "is_deleted" in deals:
        where.append("NOT coalesce(d.is_deleted, false)")
    return (
        f"SELECT {', '.join(select)}\n  FROM public.deals d\n"
        + "".join(f"  {j}\n" for j in joins)
        + f" WHERE {' AND '.join(where)}"
    )


def in_scope_stage_keys() -> list[str]:
    return sorted({" ".join(s.split()).lower() for s in IN_SCOPE_STAGES})


Connect = Callable[[Settings], Any]


def connect(settings: Settings) -> psycopg.Connection:
    conn = psycopg.connect(
        host=settings.zoho_pg_host,
        port=settings.zoho_pg_port,
        dbname=settings.zoho_pg_database,
        user=settings.zoho_pg_user,
        password=settings.zoho_pg_password,
        sslmode=settings.zoho_pg_sslmode,
        connect_timeout=15,
        row_factory=dict_row,
    )
    conn.read_only = True  # every transaction on this connection is READ ONLY
    return conn


def fetch_deals(settings: Settings, connect_fn: Connect = connect) -> ZohoResult:
    """All in-scope deals from the Zoho copy. Never raises: failures come back as `error`."""
    if not settings.zoho_pg_configured:
        return ZohoResult(error="Zoho Postgres is not configured (ZOHO_PG_HOST, ZOHO_PG_USER, ZOHO_PG_PASSWORD).")
    try:
        with connect_fn(settings) as conn, conn.cursor() as cur:
            cur.execute(_COLUMNS_SQL)
            columns: dict[str, set[str]] = {}
            for row in cur.fetchall():
                columns.setdefault(row["table_name"], set()).add(row["column_name"])
            if "deals" not in columns:
                return ZohoResult(error="The Zoho copy has no public.deals table visible to this login.")
            cur.execute(build_deals_query(columns), {"stages": in_scope_stage_keys()})
            rows = cur.fetchall()
    except Exception as exc:  # one failing source never fails the whole run
        # The message is shown on the open API, so it names the kind of failure only; the
        # exception text (which can carry the host and login name) stays in the server log.
        logger.exception("Zoho Postgres read failed")
        return ZohoResult(error=f"Zoho Postgres read failed ({type(exc).__name__}). Details are in the server log.")

    deals: list[ZohoDeal] = []
    for row in rows:
        try:
            deals.append(row_to_deal(dict(row)))
        except Exception:
            logger.exception("Skipping a Zoho row that could not be read: id=%s", row.get("id"))
    return ZohoResult(deals=deals)

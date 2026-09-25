"""The settings object the copied ICP-bot modules expect, built from Rasa Incanta's own settings.

The modules in this package are copied from the ICP bot and call `get_settings()` for a handful of
attributes (see the ICP bot's `core/config.py`). Rather than edit every call site, they import
`get_settings` from here, which maps our variable names onto theirs. Nothing here reads the
environment itself: everything comes from `api.config.get_settings()`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from api.config import Settings as RasaSettings
from api.config import get_settings as rasa_settings

REFERENCE_DIR = Path(__file__).resolve().parent / "reference"


def pg_url(target: dict | None) -> str:
    """A postgresql:// URL for a psycopg target from Settings.zoho_db / setu_db. The ICP bot's
    database helpers parse the DSN as a URL (for a hostaddr fallback), so parts become a URL too."""
    if not target:
        return ""
    if "conninfo" in target:
        return target["conninfo"]
    auth = f"{quote(target['user'], safe='')}:{quote(target['password'], safe='')}"
    path = f"/{quote(target['dbname'], safe='')}" if target.get("dbname") else ""
    return f"postgresql://{auth}@{target['host']}:{target['port']}{path}?sslmode={target['sslmode']}"


@dataclass(frozen=True)
class IcpSettings:
    anthropic_api_key: str = ""
    anthropic_model_interpretation: str = "claude-sonnet-5"
    anthropic_model_narrative: str = "claude-sonnet-5"
    anthropic_model_research: str = "claude-sonnet-5"
    exa_api_key: str = ""
    zoho_url: str = ""
    setu_url: str = ""
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_baseurl: str = "https://cloud.langfuse.com"
    ms_mailboxes: list[str] = field(default_factory=list)
    icp_skill_file: str = str(REFERENCE_DIR / "icp-skill.md")
    icp_persona_file: str = str(REFERENCE_DIR / "Who am I.md")
    icp_client_names_file: str = str(REFERENCE_DIR / "Client Names 2020-2025.xlsx")

    # As in the ICP bot, an unconfigured database raises at once. A blank DSN would make psycopg
    # try localhost and sit out a connection timeout instead.
    @property
    def zoho_pg_dsn(self) -> str:
        if not self.zoho_url:
            raise RuntimeError("Zoho Postgres is not configured (ZOHO_DB_URL or ZOHO_PG_HOST/USER/PASSWORD).")
        return self.zoho_url

    @property
    def setu_db_dsn(self) -> str:
        if not self.setu_url:
            raise RuntimeError("Setu Postgres is not configured (SETU_DB_URL or SETU_PGHOST/USER/PASSWORD).")
        return self.setu_url


def from_settings(s: RasaSettings) -> IcpSettings:
    return IcpSettings(
        anthropic_api_key=s.anthropic_api_key,
        anthropic_model_interpretation=s.llm_model_icp,
        anthropic_model_narrative=s.llm_model_icp,
        anthropic_model_research=s.llm_model_icp,
        exa_api_key=s.exa_api_key,
        zoho_url=pg_url(s.zoho_db),
        setu_url=pg_url(s.setu_db),
        langfuse_public_key=s.langfuse_public_key,
        langfuse_secret_key=s.langfuse_secret_key,
        langfuse_baseurl=s.langfuse_baseurl,
        ms_mailboxes=[s.myrah_mailbox] if s.myrah_mailbox else [],
    )


def get_settings() -> IcpSettings:
    return from_settings(rasa_settings())


def Settings(_env_file=None, **overrides) -> IcpSettings:  # noqa: N802 - mirrors the ICP bot's Settings(...)
    """For the ICP bot's copied tests, which build `Settings(_env_file=None, exa_api_key="k")`.
    Unknown fields (ICP-bot settings we do not map) are ignored."""
    known = IcpSettings.__dataclass_fields__
    return IcpSettings(**{k: v for k, v in overrides.items() if k in known})

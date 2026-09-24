"""Application settings, read from environment variables (and the repo-root .env when running locally).

Secrets have no literal defaults and fail closed: an unset Zoho or Anthropic credential means
the run flags the gap instead of crashing. SESSION_SECRET and CGO_REPORTS_API_KEY feed the
identity code in api/auth.py, which is kept but not wired in while the board is open. Blank values count as
unset, so a .env copied from .env.example behaves like an empty environment.
"""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[1]
REPORT_ID = "rasa-incanta"
REPORT_PREFIX = f"/reports/{REPORT_ID}"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env", env_file_encoding="utf-8", env_ignore_empty=True, extra="ignore",
    )

    # Runtime
    environment: str = "local"  # local | production
    database_url: str = "sqlite:///./local.db"
    timezone: str = "Asia/Kolkata"
    static_dir: str = str(Path(__file__).resolve().parent / "static")  # the built React app

    # Our own login (api/auth.py, not wired in while the board is open). Unset = sign-in refused.
    session_secret: str = ""
    session_max_age_hours: int = 12

    # Practus Portal identity handoff (CGO reports standard; api/auth.py, not wired in).
    portal_identity_enabled: bool = False
    cgo_reports_api_key: str = ""  # shared by all reports, server-side only; unset = refuse

    # Language model (Brick 2 onwards)
    anthropic_api_key: str = ""
    llm_model_actions: str = "claude-sonnet-5"
    llm_model_extraction: str = "claude-haiku-4-5-20251001"

    # Zoho CRM, read-only Postgres copy `zoho_data` (Brick 2). Preferred over the API below.
    zoho_pg_host: str = ""
    zoho_pg_port: int = 5432
    zoho_pg_database: str = "zoho_data"
    zoho_pg_user: str = ""
    zoho_pg_password: str = ""
    zoho_pg_sslmode: str = "require"  # Azure Postgres needs require

    # Zoho CRM API through Myrah, read-only. Fallback only; India data centre by default.
    zoho_accounts_url: str = "https://accounts.zoho.in"
    zoho_api_domain: str = "https://www.zohoapis.in"
    zoho_client_id: str = ""
    zoho_client_secret: str = ""
    zoho_refresh_token: str = ""

    # Read.ai through Myrah, weekly import (Brick 3)
    readai_client_id: str = ""
    readai_client_secret: str = ""
    readai_refresh_token: str = ""

    # Outlook through Myrah, Microsoft Graph, read-only (Brick 3)
    ms_tenant_id: str = ""
    ms_client_id: str = ""
    ms_client_secret: str = ""
    myrah_mailbox: str = ""

    # Setu, read-only Postgres (Brick 3)
    setu_database_url: str = ""

    # ICP bot API, one input framework (Brick 3)
    icp_bot_base_url: str = ""
    icp_bot_api_key: str = ""

    @property
    def is_local(self) -> bool:
        return self.environment.strip().lower() == "local"

    @property
    def sqlalchemy_url(self) -> str:
        """Railway hands out postgres:// or postgresql:// URLs; SQLAlchemy needs the psycopg driver named."""
        url = self.database_url.strip()
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        if url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url[len("postgresql://"):]
        return url

    @property
    def zoho_pg_configured(self) -> bool:
        return bool(self.zoho_pg_host and self.zoho_pg_user and self.zoho_pg_password)

    def startup_warnings(self) -> list[str]:
        warnings: list[str] = []
        if self.portal_identity_enabled:
            warnings.append("PORTAL_IDENTITY_ENABLED is true but has no effect: the board is open to everyone.")
        if not self.is_local and self.sqlalchemy_url.startswith("sqlite"):
            warnings.append("DATABASE_URL points at SQLite outside local; set it to the Railway Postgres URL.")
        if not self.is_local and not self.anthropic_api_key:
            warnings.append("ANTHROPIC_API_KEY is empty; recommendation generation will be skipped.")
        if not self.is_local and not self.zoho_pg_configured:
            warnings.append("ZOHO_PG_HOST, ZOHO_PG_USER or ZOHO_PG_PASSWORD is empty; runs cannot pull deals.")
        return warnings


@lru_cache
def get_settings() -> Settings:
    return Settings()

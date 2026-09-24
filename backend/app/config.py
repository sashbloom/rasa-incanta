"""Application settings, read from environment variables (and backend/.env when running locally).

Every credential is optional at Brick 1 so the service can deploy before access lands.
Later bricks check for the values they need and flag a gap instead of crashing.
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Runtime
    environment: str = "local"  # local | production
    database_url: str = "sqlite:///./local.db"
    timezone: str = "Asia/Kolkata"

    # Language model (Brick 2 onwards)
    anthropic_api_key: str = ""
    llm_model_actions: str = "claude-sonnet-5"
    llm_model_extraction: str = "claude-haiku-4-5-20251001"

    # Zoho CRM through Myrah, read-only (Brick 2). India data centre by default.
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

    def startup_warnings(self) -> list[str]:
        warnings: list[str] = []
        if not self.is_local and self.sqlalchemy_url.startswith("sqlite"):
            warnings.append("DATABASE_URL points at SQLite outside local; set it to the Railway Postgres URL.")
        if not self.is_local and not self.anthropic_api_key:
            warnings.append("ANTHROPIC_API_KEY is empty; recommendation generation will be skipped.")
        return warnings


@lru_cache
def get_settings() -> Settings:
    return Settings()

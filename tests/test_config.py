from api.config import Settings


def test_railway_postgres_urls_get_the_psycopg_driver():
    assert Settings(database_url="postgres://u:p@h:5432/db").sqlalchemy_url == "postgresql+psycopg://u:p@h:5432/db"
    assert Settings(database_url="postgresql://u:p@h:5432/db").sqlalchemy_url == "postgresql+psycopg://u:p@h:5432/db"


def test_sqlite_url_is_left_alone():
    assert Settings(database_url="sqlite:///./local.db").sqlalchemy_url == "sqlite:///./local.db"


def test_warns_when_production_uses_sqlite():
    warnings = Settings(environment="production", database_url="sqlite:///./x.db", anthropic_api_key="k").startup_warnings()
    assert any("SQLite" in w for w in warnings)


def test_env_example_lists_every_setting_with_no_value():
    from tests.conftest import REPO_ROOT

    lines = [l for l in (REPO_ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
             if l and not l.startswith("#")]
    entries = dict(l.split("=", 1) for l in lines)
    assert {name.upper() for name in Settings.model_fields} <= set(entries)
    assert all(value == "" for value in entries.values()), "no values in .env.example"


def test_secrets_have_no_literal_defaults():
    s = Settings(_env_file=None)
    assert s.session_secret == "" and s.cgo_reports_api_key == "" and s.portal_identity_enabled is False
    assert s.anthropic_api_key == "" and s.zoho_pg_password == ""

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


URL = "postgresql://reader:p%40ss@db.example:5432/zoho_data?sslmode=require"


def test_zoho_connection_string_is_used_as_is():
    assert Settings(_env_file=None, zoho_db_url=URL).zoho_db == {"conninfo": URL}


def test_zoho_separate_parts_still_work():
    s = Settings(_env_file=None, zoho_pg_host="h", zoho_pg_user="u", zoho_pg_password="p")
    assert s.zoho_db == {"host": "h", "port": 5432, "user": "u", "password": "p", "sslmode": "require", "dbname": "zoho_data"}


def test_zoho_url_wins_over_parts_and_says_so():
    s = Settings(_env_file=None, zoho_db_url=URL, zoho_pg_host="h", zoho_pg_user="u", zoho_pg_password="p")
    assert s.zoho_db == {"conninfo": URL}
    assert any("ZOHO_DB_URL and ZOHO_PG_HOST are both set" in w for w in s.startup_warnings())


def test_zoho_unconfigured_needs_host_user_and_password():
    assert Settings(_env_file=None).zoho_db is None
    assert Settings(_env_file=None, zoho_pg_host="h", zoho_pg_user="u").zoho_db is None  # no password
    assert not Settings(_env_file=None).zoho_pg_configured


def test_setu_accepts_a_url_or_the_pg_variables(monkeypatch):
    for name, value in {"SETU_PGHOST": "setu.example", "SETU_PGPORT": "6432", "SETU_PGDATABASE": "wisible_data",
                        "SETU_PGUSER": "reader", "SETU_PGPASSWORD": "x", "SETU_PGSSLMODE": "require"}.items():
        monkeypatch.setenv(name, value)
    parts = Settings(_env_file=None).setu_db
    assert parts == {"host": "setu.example", "port": 6432, "user": "reader", "password": "x",
                     "sslmode": "require", "dbname": "wisible_data"}
    monkeypatch.setenv("SETU_DB_URL", "postgresql://r:x@setu.example/wisible_data")
    s = Settings(_env_file=None)
    assert s.setu_db == {"conninfo": "postgresql://r:x@setu.example/wisible_data"}
    assert any("SETU_DB_URL and SETU_PGHOST are both set" in w for w in s.startup_warnings())
    assert Settings(_env_file=None, setu_db_url="", setu_pghost="").setu_db is None

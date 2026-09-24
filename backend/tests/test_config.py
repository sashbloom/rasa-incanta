from app.config import Settings


def test_railway_postgres_urls_get_the_psycopg_driver():
    assert Settings(database_url="postgres://u:p@h:5432/db").sqlalchemy_url == "postgresql+psycopg://u:p@h:5432/db"
    assert Settings(database_url="postgresql://u:p@h:5432/db").sqlalchemy_url == "postgresql+psycopg://u:p@h:5432/db"


def test_sqlite_url_is_left_alone():
    assert Settings(database_url="sqlite:///./local.db").sqlalchemy_url == "sqlite:///./local.db"


def test_warns_when_production_uses_sqlite():
    warnings = Settings(environment="production", database_url="sqlite:///./x.db", anthropic_api_key="k").startup_warnings()
    assert any("SQLite" in w for w in warnings)

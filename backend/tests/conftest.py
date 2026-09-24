"""Every test gets its own throwaway SQLite database, migrated with the real Alembic migrations."""
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1]


def _clear_caches() -> None:
    from app.config import get_settings
    from app.db import get_engine, get_sessionmaker

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()


def alembic_config() -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    return cfg


@pytest.fixture()
def db_url(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'test.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("ENVIRONMENT", "local")
    _clear_caches()
    yield url
    _clear_caches()


@pytest.fixture()
def migrated(db_url):
    command.upgrade(alembic_config(), "head")
    return db_url


@pytest.fixture()
def client(migrated):
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client

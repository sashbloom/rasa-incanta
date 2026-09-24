"""Every test gets its own throwaway SQLite database, migrated with the real Alembic migrations,
and never reads the developer's .env."""
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from api.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[1]
Settings.model_config["env_file"] = None  # tests see only what they set

TEST_SESSION_SECRET = "test-session-secret-not-for-production"
TEST_PORTAL_KEY = "test-portal-key"


def _clear_caches() -> None:
    from api.config import get_settings
    from api.db import get_engine, get_sessionmaker

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()


def alembic_config() -> Config:
    cfg = Config(str(REPO_ROOT / "api" / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "api" / "migrations"))
    return cfg


@pytest.fixture()
def static_dir(tmp_path):
    """A stand-in for the Vite build: index.html plus one hashed asset."""
    root = tmp_path / "a" / "b" / "static"  # three levels below the secret, so ../../../ reaches it
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(
        '<!doctype html><script type="module" src="/reports/rasa-incanta/assets/index-abc.js"></script>'
        '<div id="root"></div>', encoding="utf-8")
    (root / "assets" / "index-abc.js").write_text("console.log('ri')", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("outside the build", encoding="utf-8")
    return root


@pytest.fixture()
def db_url(tmp_path, monkeypatch, static_dir):
    url = f"sqlite:///{tmp_path / 'test.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("ENVIRONMENT", "local")
    monkeypatch.setenv("SESSION_SECRET", TEST_SESSION_SECRET)
    monkeypatch.setenv("STATIC_DIR", str(static_dir))
    monkeypatch.delenv("PORTAL_IDENTITY_ENABLED", raising=False)
    monkeypatch.delenv("CGO_REPORTS_API_KEY", raising=False)
    _clear_caches()
    yield url
    _clear_caches()


@pytest.fixture()
def migrated(db_url):
    command.upgrade(alembic_config(), "head")
    return db_url


@pytest.fixture()
def client(migrated):
    from api.main import app

    with TestClient(app) as test_client:
        yield test_client

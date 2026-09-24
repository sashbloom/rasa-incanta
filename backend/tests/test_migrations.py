from alembic import command
from sqlalchemy import create_engine, inspect

from tests.conftest import alembic_config

EXPECTED_TABLES = {
    "users", "user_aliases", "runs", "deals", "deal_snapshots",
    "context_cards", "recommendations", "deal_reviews", "decisions",
}


def test_migration_creates_all_tables(migrated):
    tables = set(inspect(create_engine(migrated)).get_table_names())
    assert EXPECTED_TABLES <= tables


def test_models_and_migrations_are_in_sync(migrated):
    # Fails if a model changed without a matching migration.
    command.check(alembic_config())


def test_migration_downgrades_cleanly(migrated):
    command.downgrade(alembic_config(), "base")
    tables = set(inspect(create_engine(migrated)).get_table_names())
    assert not (EXPECTED_TABLES & tables)

"""Migration 0007 finds the old unique constraint by its columns, whatever it is called, and skips what
is not there. Each case rebuilds deal_reviews at revision 0006 in a different shape, then upgrades."""
import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text

from tests.conftest import alembic_config

TABLE_SQL = """
CREATE TABLE deal_reviews (
    id CHAR(32) NOT NULL, week_start DATE NOT NULL, deal_id CHAR(32) NOT NULL, user_id CHAR(32),
    rationale TEXT, own_action TEXT, status VARCHAR(20) NOT NULL, updated_at DATETIME NOT NULL,
    completed_at DATETIME, created_at DATETIME NOT NULL, PRIMARY KEY (id){extra},
    FOREIGN KEY(user_id) REFERENCES users (id), FOREIGN KEY(deal_id) REFERENCES deals (id)
)"""
OPEN_BOARD_INDEX = "CREATE UNIQUE INDEX uq_deal_reviews_open_board ON deal_reviews (week_start, deal_id) WHERE user_id IS NULL"

SHAPES = {
    "as 0001 and 0006 made it": (", UNIQUE (week_start, deal_id, user_id)", True),
    "the constraint has another name": (", CONSTRAINT some_other_name UNIQUE (week_start, deal_id, user_id)", True),
    "no unique constraint at all": ("", True),
    "no open-board index": (", UNIQUE (week_start, deal_id, user_id)", False),
    "neither": ("", False),
}


def rebuild_at_0006(url, constraint_sql, with_index):
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE deal_reviews"))
        conn.execute(text(TABLE_SQL.format(extra=constraint_sql)))
        if with_index:
            conn.execute(text(OPEN_BOARD_INDEX))
    engine.dispose()


@pytest.mark.parametrize("shape", SHAPES)
def test_upgrade_succeeds_whatever_the_old_constraint_is_called_or_whether_it_exists(db_url, shape):
    command.upgrade(alembic_config(), "0006")
    rebuild_at_0006(db_url, *SHAPES[shape])

    command.upgrade(alembic_config(), "head")

    inspector = inspect(create_engine(db_url))
    unique = {tuple(c["column_names"]): c["name"] for c in inspector.get_unique_constraints("deal_reviews")}
    assert list(unique) == [("week_start", "deal_id", "user_id", "version")]  # the old one is gone, the new one is there
    assert unique[("week_start", "deal_id", "user_id", "version")] == "uq_deal_reviews_week_deal_user_version"
    names = {i["name"] for i in inspector.get_indexes("deal_reviews")}
    assert "uq_deal_reviews_open_board_version" in names and "uq_deal_reviews_open_board" not in names
    assert "version" in {c["name"] for c in inspector.get_columns("deal_reviews")}


def test_upgrade_keeps_existing_reviews_as_version_one(db_url):
    command.upgrade(alembic_config(), "0006")
    engine = create_engine(db_url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO runs (id, week_start, kind, status, started_at, stats, created_at) VALUES "
                          "('r', '2026-09-21', 'manual', 'succeeded', '2026-09-24', '{}', '2026-09-24')"))
        conn.execute(text("INSERT INTO deals (id, zoho_id, name, stage, is_active, last_seen_at, ep_involved, el_involved, raw, "
                          "created_at) VALUES ('d', 'z', 'Deal', 'Prospect', 1, '2026-09-24', '[]', '[]', '{}', '2026-09-24')"))
        conn.execute(text("INSERT INTO deal_reviews (id, week_start, deal_id, rationale, status, updated_at, created_at) VALUES "
                          "('a', '2026-09-21', 'd', 'Because', 'done', '2026-09-24', '2026-09-24')"))
    command.upgrade(alembic_config(), "head")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT version, rationale FROM deal_reviews")).all() == [(1, "Because")]


def test_downgrade_and_upgrade_again_round_trips(migrated):
    command.downgrade(alembic_config(), "0006")
    command.upgrade(alembic_config(), "head")
    names = {i["name"] for i in inspect(create_engine(migrated)).get_indexes("deal_reviews")}
    assert "uq_deal_reviews_open_board_version" in names

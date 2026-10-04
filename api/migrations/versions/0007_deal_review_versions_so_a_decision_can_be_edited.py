"""deal review versions, so a decision can be edited by appending

An edit adds a deal_reviews row with the next version; the highest version is the current decision and
earlier ones stay as history. The one-review-per-week uniqueness (the unnamed constraint from 0001 and
the open-board index from 0006) becomes one-per-version.

The old objects are looked up in the database at migration time (SQLAlchemy's inspector, which reads
information_schema or the catalog) rather than assumed by name: whatever unique constraint covers
exactly (week_start, deal_id, user_id) is dropped, and a missing constraint or index is skipped, not an error.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-05 10:12:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0007'
down_revision: Union[str, Sequence[str], None] = '0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = 'deal_reviews'
OLD_UNIQUE_COLUMNS = {'week_start', 'deal_id', 'user_id'}
OLD_OPEN_BOARD_INDEX = 'uq_deal_reviews_open_board'  # 0006
NEW_UNIQUE = 'uq_deal_reviews_week_deal_user_version'
NEW_OPEN_BOARD_INDEX = 'uq_deal_reviews_open_board_version'

# SQLite reflects an unnamed constraint (0001's) with no name; this convention gives it one so batch
# mode can drop it. Every other dialect reports the real name, which is what gets dropped.
SQLITE_NAMING = {"uq": "uq_%(table_name)s_%(column_0_N_name)s"}
SQLITE_OLD_UNIQUE = 'uq_deal_reviews_week_start_deal_id_user_id'


def _old_unique_name(inspector: sa.Inspector) -> str | None:
    """The name of the unique constraint on exactly (week_start, deal_id, user_id), or None if there is none."""
    for constraint in inspector.get_unique_constraints(TABLE):
        if set(constraint['column_names']) == OLD_UNIQUE_COLUMNS:
            return constraint['name'] or SQLITE_OLD_UNIQUE
    return None


def _has_index(inspector: sa.Inspector, name: str) -> bool:
    return any(index['name'] == name for index in inspector.get_indexes(TABLE))


def _has_unique(inspector: sa.Inspector, name: str) -> bool:
    return any(constraint['name'] == name for constraint in inspector.get_unique_constraints(TABLE))


def _has_column(inspector: sa.Inspector, name: str) -> bool:
    return any(column['name'] == name for column in inspector.get_columns(TABLE))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    old_unique = _old_unique_name(inspector)
    old_index = _has_index(inspector, OLD_OPEN_BOARD_INDEX)
    add_version = not _has_column(inspector, 'version')
    add_unique = not _has_unique(inspector, NEW_UNIQUE)
    add_index = not _has_index(inspector, NEW_OPEN_BOARD_INDEX)

    with op.batch_alter_table(TABLE, naming_convention=SQLITE_NAMING if bind.dialect.name == 'sqlite' else None) as batch_op:
        if add_version:
            batch_op.add_column(sa.Column('version', sa.Integer(), server_default='1', nullable=False))
        if old_index:
            batch_op.drop_index(OLD_OPEN_BOARD_INDEX, sqlite_where=sa.text('user_id IS NULL'),
                                postgresql_where=sa.text('user_id IS NULL'))
        if old_unique:
            batch_op.drop_constraint(old_unique, type_='unique')
        if add_unique:
            batch_op.create_unique_constraint(NEW_UNIQUE, ['week_start', 'deal_id', 'user_id', 'version'])
        if add_index:
            batch_op.create_index(NEW_OPEN_BOARD_INDEX, ['week_start', 'deal_id', 'version'], unique=True,
                                  sqlite_where=sa.text('user_id IS NULL'), postgresql_where=sa.text('user_id IS NULL'))


def downgrade() -> None:
    # History is append-only: once a decision has been edited there are several rows per week, which the
    # old one-per-week constraint cannot hold, and we do not delete them to make it fit.
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT 1 FROM deal_reviews WHERE version > 1 LIMIT 1")).first():
        raise RuntimeError("Cannot downgrade: edited decisions have several versions, and history is never deleted.")
    inspector = sa.inspect(bind)
    has_new_index = _has_index(inspector, NEW_OPEN_BOARD_INDEX)
    has_new_unique = _has_unique(inspector, NEW_UNIQUE)
    has_version = _has_column(inspector, 'version')
    add_old_unique = _old_unique_name(inspector) is None
    add_old_index = not _has_index(inspector, OLD_OPEN_BOARD_INDEX)

    with op.batch_alter_table(TABLE) as batch_op:
        if has_new_index:
            batch_op.drop_index(NEW_OPEN_BOARD_INDEX, sqlite_where=sa.text('user_id IS NULL'),
                                postgresql_where=sa.text('user_id IS NULL'))
        if has_new_unique:
            batch_op.drop_constraint(NEW_UNIQUE, type_='unique')
        if has_version:
            batch_op.drop_column('version')
        if add_old_unique:
            batch_op.create_unique_constraint(SQLITE_OLD_UNIQUE, ['week_start', 'deal_id', 'user_id'])
        if add_old_index:
            batch_op.create_index(OLD_OPEN_BOARD_INDEX, ['week_start', 'deal_id'], unique=True,
                                  sqlite_where=sa.text('user_id IS NULL'), postgresql_where=sa.text('user_id IS NULL'))

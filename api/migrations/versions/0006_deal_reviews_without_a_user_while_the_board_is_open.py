"""deal reviews without a user while the board is open

The board has no sign-in, so a decision has no user to record. user_id becomes optional; a partial
unique index keeps it to one open-board review per deal and week (the existing unique constraint
cannot, because NULLs never collide).

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-04 18:57:24.308513

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0006'
down_revision: Union[str, Sequence[str], None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('deal_reviews', schema=None) as batch_op:
        batch_op.alter_column('user_id', existing_type=sa.Uuid(), nullable=True)
        batch_op.create_index('uq_deal_reviews_open_board', ['week_start', 'deal_id'], unique=True,
                              sqlite_where=sa.text('user_id IS NULL'), postgresql_where=sa.text('user_id IS NULL'))


def downgrade() -> None:
    # History is append-only: reviews made while the board was open have no user, so they cannot go
    # back to a NOT NULL column, and we do not delete them to make it so.
    if op.get_bind().execute(sa.text("SELECT 1 FROM deal_reviews WHERE user_id IS NULL LIMIT 1")).first():
        raise RuntimeError("Cannot downgrade: deal_reviews holds reviews with no user, and history is never deleted.")
    with op.batch_alter_table('deal_reviews', schema=None) as batch_op:
        batch_op.drop_index('uq_deal_reviews_open_board', sqlite_where=sa.text('user_id IS NULL'),
                            postgresql_where=sa.text('user_id IS NULL'))
        batch_op.alter_column('user_id', existing_type=sa.Uuid(), nullable=False)

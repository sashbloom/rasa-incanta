"""meeting transcripts, for past meetings uploaded by hand

Read.ai webhook only reaches forward, so past calls are uploaded (POST /api/transcripts/upload) with their
transcript text. Webhook meetings keep no transcript.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-05 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0008'
down_revision: Union[str, Sequence[str], None] = '0007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if 'transcript' not in {c['name'] for c in sa.inspect(op.get_bind()).get_columns('meetings')}:
        with op.batch_alter_table('meetings', schema=None) as batch_op:
            batch_op.add_column(sa.Column('transcript', sa.Text(), nullable=True))


def downgrade() -> None:
    # Uploaded transcripts are history and are never deleted to make a downgrade fit.
    if op.get_bind().execute(sa.text("SELECT 1 FROM meetings WHERE transcript IS NOT NULL LIMIT 1")).first():
        raise RuntimeError("Cannot downgrade: meetings holds uploaded transcripts, and history is never deleted.")
    with op.batch_alter_table('meetings', schema=None) as batch_op:
        batch_op.drop_column('transcript')

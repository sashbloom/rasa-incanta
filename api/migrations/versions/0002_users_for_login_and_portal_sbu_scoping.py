"""users for login and portal, sbu scoping

Users move to the CGO reports standard shape (username, password_hash, role, is_active,
ms_email) with per-user SBU access in user_allowed_sbus. Deals gain the SBU they are scoped by.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-24 15:32:37.407482

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0002'
down_revision: Union[str, Sequence[str], None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('user_allowed_sbus',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('sbu', sa.String(length=100), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('user_id', 'sbu')
    )
    with op.batch_alter_table('deals', schema=None) as batch_op:
        batch_op.add_column(sa.Column('sbu', sa.String(length=100), nullable=True))
        batch_op.create_index(batch_op.f('ix_deals_sbu'), ['sbu'], unique=False)

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('username', sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column('password_hash', sa.String(length=300), nullable=True))
        batch_op.add_column(sa.Column('ms_email', sa.String(length=320), nullable=True))
    # Existing rows keep their address as the username and get no usable password. ms_email
    # stays NULL on purpose: nobody is reachable through the portal until someone maps them.
    op.execute("UPDATE users SET username = lower(email), password_hash = '!' WHERE username IS NULL")
    op.execute("UPDATE users SET role = 'user' WHERE role NOT IN ('super_admin', 'admin', 'user')")
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.alter_column('username', existing_type=sa.String(length=100), nullable=False)
        batch_op.alter_column('password_hash', existing_type=sa.String(length=300), nullable=False)
        batch_op.create_unique_constraint('uq_users_username', ['username'])
        batch_op.create_check_constraint('ck_users_role', "role IN ('super_admin', 'admin', 'user')")
        batch_op.drop_column('email')
        batch_op.drop_column('display_name')
    # Unique ignoring case, and still many NULLs (unmapped accounts).
    op.create_index('uq_users_ms_email_lower', 'users', [sa.text('lower(ms_email)')], unique=True)


def downgrade() -> None:
    op.drop_index('uq_users_ms_email_lower', table_name='users')
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('display_name', sa.VARCHAR(length=200), nullable=True))
        batch_op.add_column(sa.Column('email', sa.VARCHAR(length=320), nullable=True))
    op.execute("UPDATE users SET email = coalesce(ms_email, username), display_name = username")
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.alter_column('email', existing_type=sa.VARCHAR(length=320), nullable=False)
        batch_op.alter_column('display_name', existing_type=sa.VARCHAR(length=200), nullable=False)
        batch_op.drop_constraint('ck_users_role', type_='check')
        batch_op.drop_constraint('uq_users_username', type_='unique')
        batch_op.create_unique_constraint('users_email_key', ['email'])
        batch_op.drop_column('ms_email')
        batch_op.drop_column('password_hash')
        batch_op.drop_column('username')

    with op.batch_alter_table('deals', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_deals_sbu'))
        batch_op.drop_column('sbu')

    op.drop_table('user_allowed_sbus')

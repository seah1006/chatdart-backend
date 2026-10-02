"""add warning_sent to user_memberships and memo to watchlist

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-05-19

"""
from alembic import op
import sqlalchemy as sa

revision = 'e5f6a7b8c9d0'
down_revision = 'd4e5f6a7b8c9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('user_memberships') as batch_op:
        batch_op.add_column(
            sa.Column('warning_sent', sa.Boolean(), nullable=False, server_default='0')
        )

    with op.batch_alter_table('watchlist') as batch_op:
        batch_op.add_column(
            sa.Column('memo', sa.String(length=500), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table('watchlist') as batch_op:
        batch_op.drop_column('memo')

    with op.batch_alter_table('user_memberships') as batch_op:
        batch_op.drop_column('warning_sent')

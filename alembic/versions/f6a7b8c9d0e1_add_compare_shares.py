"""add compare_shares table

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-05-20

"""
from alembic import op
import sqlalchemy as sa


revision = 'f6a7b8c9d0e1'
down_revision = 'e5f6a7b8c9d0'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'compare_shares',
        sa.Column('share_id', sa.String(length=16), primary_key=True),
        sa.Column('payload', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_compare_shares_share_id', 'compare_shares', ['share_id'])
    op.create_index('ix_compare_shares_expires_at', 'compare_shares', ['expires_at'])


def downgrade() -> None:
    op.drop_index('ix_compare_shares_expires_at', table_name='compare_shares')
    op.drop_index('ix_compare_shares_share_id', table_name='compare_shares')
    op.drop_table('compare_shares')

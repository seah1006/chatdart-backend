"""add search_logs table

Revision ID: a3f1c2d4e5b6
Revises: eb0b565e9d00
Create Date: 2026-04-21 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a3f1c2d4e5b6'
down_revision: Union[str, Sequence[str], None] = 'eb0b565e9d00'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'search_logs',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('stock_code', sa.String(length=20), nullable=False),
        sa.Column('company_name', sa.String(length=50), nullable=False),
        sa.Column('searched_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_search_logs_stock_code', 'search_logs', ['stock_code'])
    op.create_index('ix_search_logs_searched_at', 'search_logs', ['searched_at'])


def downgrade() -> None:
    op.drop_index('ix_search_logs_searched_at', table_name='search_logs')
    op.drop_index('ix_search_logs_stock_code', table_name='search_logs')
    op.drop_table('search_logs')

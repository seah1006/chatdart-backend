"""initial schema

Revision ID: eb0b565e9d00
Revises:
Create Date: 2026-04-14 14:20:53.238731

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'eb0b565e9d00'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'Financial_Statement',
        sa.Column('fs_id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('company_id', sa.String(length=20), nullable=False),
        sa.Column('company_name', sa.String(length=50), nullable=True),
        sa.Column('fiscal_year', sa.Integer(), nullable=False),
        sa.Column('revenue', sa.BigInteger(), nullable=True),
        sa.Column('cost_of_sales', sa.BigInteger(), nullable=True),
        sa.Column('gross_profit', sa.BigInteger(), nullable=True),
        sa.Column('sga', sa.BigInteger(), nullable=True),
        sa.Column('operating_profit', sa.BigInteger(), nullable=True),
        sa.Column('net_income', sa.BigInteger(), nullable=True),
        sa.Column('total_assets', sa.BigInteger(), nullable=True),
        sa.Column('total_liabilities', sa.BigInteger(), nullable=True),
        sa.Column('equity', sa.BigInteger(), nullable=True),
        sa.Column('cash', sa.BigInteger(), nullable=True),
        sa.Column('source_report', sa.String(length=200), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('fs_id'),
        sa.UniqueConstraint('company_id', 'fiscal_year', name='uq_company_fiscal_year'),
    )
    op.create_index(
        op.f('ix_Financial_Statement_company_id'),
        'Financial_Statement', ['company_id'], unique=False,
    )
    op.create_index(
        op.f('ix_Financial_Statement_fs_id'),
        'Financial_Statement', ['fs_id'], unique=False,
    )

    op.create_table(
        'collection_tasks',
        sa.Column('stock_code', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=True),
        sa.Column('message', sa.String(), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('stock_code'),
    )
    op.create_index(
        op.f('ix_collection_tasks_stock_code'),
        'collection_tasks', ['stock_code'], unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_collection_tasks_stock_code'), table_name='collection_tasks')
    op.drop_table('collection_tasks')
    op.drop_index(op.f('ix_Financial_Statement_fs_id'), table_name='Financial_Statement')
    op.drop_index(op.f('ix_Financial_Statement_company_id'), table_name='Financial_Statement')
    op.drop_table('Financial_Statement')

"""phase96 add income detail columns

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-07-13

"""
from alembic import op
import sqlalchemy as sa


revision = 'b8c9d0e1f2a3'
down_revision = 'a7b8c9d0e1f2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('Financial_Statement', sa.Column('other_income', sa.BigInteger(), nullable=True))
    op.add_column('Financial_Statement', sa.Column('other_expense', sa.BigInteger(), nullable=True))
    op.add_column('Financial_Statement', sa.Column('finance_income', sa.BigInteger(), nullable=True))
    op.add_column('Financial_Statement', sa.Column('finance_cost', sa.BigInteger(), nullable=True))
    op.add_column('Financial_Statement', sa.Column('income_before_tax', sa.BigInteger(), nullable=True))
    op.add_column('Financial_Statement', sa.Column('income_tax_expense', sa.BigInteger(), nullable=True))
    op.add_column('Financial_Statement', sa.Column('operating_cash_flow', sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column('Financial_Statement', 'operating_cash_flow')
    op.drop_column('Financial_Statement', 'income_tax_expense')
    op.drop_column('Financial_Statement', 'income_before_tax')
    op.drop_column('Financial_Statement', 'finance_cost')
    op.drop_column('Financial_Statement', 'finance_income')
    op.drop_column('Financial_Statement', 'other_expense')
    op.drop_column('Financial_Statement', 'other_income')

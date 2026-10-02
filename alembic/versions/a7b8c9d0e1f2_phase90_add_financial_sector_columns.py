"""phase90 add financial sector columns

Revision ID: a7b8c9d0e1f2
Revises: 9d8c7b6a5e4f
Create Date: 2026-07-09

"""
from alembic import op
import sqlalchemy as sa


revision = 'a7b8c9d0e1f2'
down_revision = '9d8c7b6a5e4f'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('Financial_Statement', sa.Column('net_interest_income', sa.BigInteger(), nullable=True))
    op.add_column('Financial_Statement', sa.Column('loan_loss_provision', sa.BigInteger(), nullable=True))
    op.add_column('Financial_Statement', sa.Column('insurance_liability', sa.BigInteger(), nullable=True))
    op.add_column('Financial_Statement', sa.Column('sector_detail', sa.String(length=20), nullable=True))


def downgrade() -> None:
    op.drop_column('Financial_Statement', 'sector_detail')
    op.drop_column('Financial_Statement', 'insurance_liability')
    op.drop_column('Financial_Statement', 'loan_loss_provision')
    op.drop_column('Financial_Statement', 'net_interest_income')

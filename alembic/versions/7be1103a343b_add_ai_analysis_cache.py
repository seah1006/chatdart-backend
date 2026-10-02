"""add ai_analysis_cache

Revision ID: 7be1103a343b
Revises: f6a7b8c9d0e1
Create Date: 2026-05-26 11:53:58.674247

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7be1103a343b'
down_revision: Union[str, Sequence[str], None] = 'f6a7b8c9d0e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'ai_analysis_cache',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('stock_code', sa.String(length=10), nullable=False),
        sa.Column('fiscal_year', sa.Integer(), nullable=False),
        sa.Column('prediction', sa.Float(), nullable=False),
        sa.Column('summary', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('stock_code', 'fiscal_year', name='uq_ai_cache_stock_fy'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('ai_analysis_cache')

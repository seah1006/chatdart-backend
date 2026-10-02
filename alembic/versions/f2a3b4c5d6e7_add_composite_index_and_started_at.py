"""add composite index on search_logs and started_at on collection_tasks

Revision ID: f2a3b4c5d6e7
Revises: c7e3f9b1d2a4
Create Date: 2026-04-21

변경 사항:
- search_logs: 복합 인덱스 (searched_at, stock_code) 추가
  → /popular, /search/popular의 시간 윈도우 필터 + 그룹 집계 쿼리 최적화
- collection_tasks: started_at 컬럼 추가
  → /collect/status 응답에 수집 시작 시각 포함
"""
from alembic import op
import sqlalchemy as sa

revision = 'f2a3b4c5d6e7'
down_revision = 'c7e3f9b1d2a4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        'ix_search_logs_searched_at_stock_code',
        'search_logs',
        ['searched_at', 'stock_code'],
        unique=False,
    )

    with op.batch_alter_table('collection_tasks') as batch_op:
        batch_op.add_column(
            sa.Column('started_at', sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table('collection_tasks') as batch_op:
        batch_op.drop_column('started_at')

    op.drop_index('ix_search_logs_searched_at_stock_code', table_name='search_logs')

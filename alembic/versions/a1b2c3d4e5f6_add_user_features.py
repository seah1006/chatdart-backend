"""add user features

Revision ID: a1b2c3d4e5f6
Revises: f2a3b4c5d6e7
Create Date: 2026-04-28

변경 사항:
- users 테이블 추가 (회원가입/로그인/JWT)
- watchlist 테이블 추가 (즐겨찾기)
- user_memberships 테이블 추가 (멤버십 구독 이력)
- payment_records 테이블 추가 (결제 기록)
- notices 테이블 추가 (공지사항)
- inquiries 테이블 추가 (문의하기)
- search_logs.user_id 컬럼 추가 (조회 히스토리, 현재는 NULL)
"""
from alembic import op
import sqlalchemy as sa

revision = 'a1b2c3d4e5f6'
down_revision = 'f2a3b4c5d6e7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # users — 다른 테이블이 FK로 참조하므로 반드시 먼저 생성
    op.create_table(
        'users',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('email', sa.String(length=255), nullable=False),
        sa.Column('hashed_password', sa.String(length=255), nullable=False),
        sa.Column('nickname', sa.String(length=50), nullable=True),
        sa.Column('membership_tier', sa.String(length=20), nullable=False, server_default='free'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='1'),
        sa.Column('is_admin', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('terms_agreed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('refresh_token_hash', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_users_id'), 'users', ['id'], unique=False)
    op.create_index(op.f('ix_users_email'), 'users', ['email'], unique=True)

    op.create_table(
        'watchlist',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('stock_code', sa.String(length=20), nullable=False),
        sa.Column('company_name', sa.String(length=200), nullable=False),
        sa.Column('added_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'stock_code', name='uq_watchlist_user_stock'),
    )
    op.create_index(op.f('ix_watchlist_user_id'), 'watchlist', ['user_id'], unique=False)

    op.create_table(
        'user_memberships',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('plan', sa.String(length=20), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='active'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_user_memberships_user_id'), 'user_memberships', ['user_id'], unique=False)

    op.create_table(
        'payment_records',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('plan', sa.String(length=20), nullable=False),
        sa.Column('amount', sa.Integer(), nullable=False),
        sa.Column('payment_id', sa.String(length=50), nullable=False),
        sa.Column('pg_transaction_id', sa.String(length=255), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='pending'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('payment_id', name='uq_payment_records_payment_id'),
        sa.UniqueConstraint('pg_transaction_id', name='uq_payment_records_pg_transaction_id'),
    )
    op.create_index(op.f('ix_payment_records_user_id'), 'payment_records', ['user_id'], unique=False)
    op.create_index(op.f('ix_payment_records_payment_id'), 'payment_records', ['payment_id'], unique=True)

    op.create_table(
        'notices',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('is_pinned', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('author_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['author_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'inquiries',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('answer', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='pending'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('answered_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_inquiries_user_id'), 'inquiries', ['user_id'], unique=False)

    # search_logs에 user_id 추가 (nullable — finance 엔드포인트 JWT 통합 전까지 NULL)
    with op.batch_alter_table('search_logs') as batch_op:
        batch_op.add_column(sa.Column('user_id', sa.Integer(), nullable=True))
    op.create_index('ix_search_logs_user_id', 'search_logs', ['user_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_search_logs_user_id', table_name='search_logs')
    with op.batch_alter_table('search_logs') as batch_op:
        batch_op.drop_column('user_id')

    op.drop_index(op.f('ix_inquiries_user_id'), table_name='inquiries')
    op.drop_table('inquiries')
    op.drop_table('notices')
    op.drop_index(op.f('ix_payment_records_payment_id'), table_name='payment_records')
    op.drop_index(op.f('ix_payment_records_user_id'), table_name='payment_records')
    op.drop_table('payment_records')
    op.drop_index(op.f('ix_user_memberships_user_id'), table_name='user_memberships')
    op.drop_table('user_memberships')
    op.drop_index(op.f('ix_watchlist_user_id'), table_name='watchlist')
    op.drop_table('watchlist')
    op.drop_index(op.f('ix_users_email'), table_name='users')
    op.drop_index(op.f('ix_users_id'), table_name='users')
    op.drop_table('users')

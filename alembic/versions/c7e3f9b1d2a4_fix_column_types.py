"""fix column types: company_name length, created_at timezone

Revision ID: c7e3f9b1d2a4
Revises: a3f1c2d4e5b6
Create Date: 2026-04-21

변경 사항:
- FinancialStatement.company_name: VARCHAR(50) → VARCHAR(200)
- FinancialStatement.created_at: TIMESTAMP → TIMESTAMPTZ
- SearchLog.company_name: VARCHAR(50) → VARCHAR(200)
"""
from alembic import op
import sqlalchemy as sa

revision = 'c7e3f9b1d2a4'
down_revision = 'a3f1c2d4e5b6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("Financial_Statement") as batch_op:
        batch_op.alter_column(
            "company_name",
            existing_type=sa.String(50),
            type_=sa.String(200),
            existing_nullable=True,
        )
        batch_op.alter_column(
            "created_at",
            existing_type=sa.DateTime(),
            type_=sa.DateTime(timezone=True),
            existing_nullable=False,
        )

    with op.batch_alter_table("search_logs") as batch_op:
        batch_op.alter_column(
            "company_name",
            existing_type=sa.String(50),
            type_=sa.String(200),
            existing_nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("search_logs") as batch_op:
        batch_op.alter_column(
            "company_name",
            existing_type=sa.String(200),
            type_=sa.String(50),
            existing_nullable=False,
        )

    with op.batch_alter_table("Financial_Statement") as batch_op:
        batch_op.alter_column(
            "created_at",
            existing_type=sa.DateTime(timezone=True),
            type_=sa.DateTime(),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "company_name",
            existing_type=sa.String(200),
            type_=sa.String(50),
            existing_nullable=True,
        )

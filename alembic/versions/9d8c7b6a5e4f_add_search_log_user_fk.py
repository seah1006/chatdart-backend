"""add search log user foreign key

Revision ID: 9d8c7b6a5e4f
Revises: 7be1103a343b
Create Date: 2026-05-29

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "9d8c7b6a5e4f"
down_revision: Union[str, Sequence[str], None] = "7be1103a343b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("search_logs") as batch_op:
        batch_op.create_foreign_key(
            "fk_search_logs_user_id_users",
            "users",
            ["user_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("search_logs") as batch_op:
        batch_op.drop_constraint("fk_search_logs_user_id_users", type_="foreignkey")

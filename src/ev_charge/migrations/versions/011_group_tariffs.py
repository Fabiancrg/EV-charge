"""Tariffs per group: a group uses the default tariffs, or its own

Revision ID: 011
Revises: 010
Create Date: 2026-11-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "011"
down_revision: str | None = "010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tariffs", sa.Column("group_id", sa.Integer()))  # NULL: the default tariffs
    op.create_index("ix_tariffs_group_id", "tariffs", ["group_id"])
    op.add_column("groups", sa.Column("own_tariffs", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("groups", sa.Column("creg_region", sa.String(20)))


def downgrade() -> None:
    with op.batch_alter_table("groups") as batch:
        batch.drop_column("creg_region")
        batch.drop_column("own_tariffs")
    op.execute("DELETE FROM tariffs WHERE group_id IS NOT NULL")
    op.drop_index("ix_tariffs_group_id", "tariffs")
    with op.batch_alter_table("tariffs") as batch:
        batch.drop_column("group_id")

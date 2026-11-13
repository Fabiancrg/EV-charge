"""Groups: cars, chargers and users belong to a group (a household, a team) that manages its own

Revision ID: 010
Revises: 009
Create Date: 2026-11-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "010"
down_revision: str | None = "009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Names the unnamed UNIQUE(name) of the first versions, so they can be dropped: names are unique per group now
NAMING = {"uq": "uq_%(table_name)s_%(column_0_name)s"}


def upgrade() -> None:
    groups = op.create_table(
        "groups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column("max_members", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("notes", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    # Everything that exists goes to a first group
    op.bulk_insert(groups, [{"id": 1, "name": "Default", "max_members": 20}])
    for table in ("cars", "chargers", "users"):
        op.add_column(table, sa.Column("group_id", sa.Integer()))
        op.create_index(f"ix_{table}_group_id", table, ["group_id"])
        op.execute(f"UPDATE {table} SET group_id = 1")
    op.add_column("users", sa.Column("group_manager", sa.Boolean(), nullable=False, server_default=sa.false()))
    for table in ("cars", "chargers"):
        with op.batch_alter_table(table, naming_convention=NAMING) as batch:
            batch.drop_constraint(f"uq_{table}_name", type_="unique")


def downgrade() -> None:
    for table in ("cars", "chargers"):
        with op.batch_alter_table(table, naming_convention=NAMING) as batch:
            batch.create_unique_constraint(f"uq_{table}_name", ["name"])
    with op.batch_alter_table("users") as batch:
        batch.drop_column("group_manager")
    for table in ("users", "chargers", "cars"):
        op.drop_index(f"ix_{table}_group_id", table)
        with op.batch_alter_table(table) as batch:
            batch.drop_column("group_id")
    op.drop_table("groups")

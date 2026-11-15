"""Secondary connection of a charger, read when the main one can't be

Revision ID: 014
Revises: 013
Create Date: 2026-11-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "014"
down_revision: str | None = "013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("chargers", sa.Column("connector2", sa.String(30)))
    op.add_column("chargers", sa.Column("connector2_secret", sa.Text()))


def downgrade() -> None:
    with op.batch_alter_table("chargers") as batch:
        batch.drop_column("connector2_secret")
        batch.drop_column("connector2")

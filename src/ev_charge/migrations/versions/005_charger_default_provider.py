"""Default provider of a charger's sessions (e.g. the home electricity supplier)

Revision ID: 005
Revises: 004
Create Date: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "005"
down_revision: str | None = "004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("chargers", sa.Column("default_provider", sa.String(200)))


def downgrade() -> None:
    with op.batch_alter_table("chargers") as batch:
        batch.drop_column("default_provider")

"""Odometer of a car read from Home Assistant at the end of a charge

Revision ID: 015
Revises: 014
Create Date: 2026-11-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "015"
down_revision: str | None = "014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("cars", sa.Column("odometer_secret", sa.Text()))
    op.add_column("cars", sa.Column("odometer_error", sa.Text()))


def downgrade() -> None:
    with op.batch_alter_table("cars") as batch:
        batch.drop_column("odometer_error")
        batch.drop_column("odometer_secret")

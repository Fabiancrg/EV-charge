"""Charger connectors (cloud API polling, live session, history) and the external reference of sessions

Revision ID: 004
Revises: 003
Create Date: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "004"
down_revision: str | None = "003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Plain ADD COLUMN (no table rebuild: the chargers table is referenced by charge_sessions)
    # No foreign key at database level (SQLite cannot add one to an existing table): routes/cars.py clears it
    op.add_column("chargers", sa.Column("default_car_id", sa.Integer()))
    op.add_column("chargers", sa.Column("default_location", sa.String(200)))
    op.add_column("chargers", sa.Column("connector", sa.String(30)))
    op.add_column("chargers", sa.Column("connector_secret", sa.Text()))
    op.add_column("chargers", sa.Column("poll_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("chargers", sa.Column("external_id", sa.String(100)))
    op.add_column("chargers", sa.Column("live_state", sa.Text()))
    op.add_column("chargers", sa.Column("last_poll_at", sa.DateTime(timezone=True)))
    op.add_column("chargers", sa.Column("last_poll_error", sa.Text()))
    op.add_column("charge_sessions", sa.Column("external_ref", sa.String(100)))
    op.create_index("ix_charge_sessions_external_ref", "charge_sessions", ["external_ref"])


def downgrade() -> None:
    op.drop_index("ix_charge_sessions_external_ref", "charge_sessions")
    with op.batch_alter_table("charge_sessions") as batch:
        batch.drop_column("external_ref")
    with op.batch_alter_table("chargers") as batch:
        for col in ("last_poll_error", "last_poll_at", "live_state", "external_id", "poll_enabled", "connector_secret", "connector", "default_location", "default_car_id"):
            batch.drop_column(col)

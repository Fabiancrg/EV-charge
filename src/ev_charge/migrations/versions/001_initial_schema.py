"""Initial schema: cars and charge sessions

Revision ID: 001
Revises:
Create Date: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cars",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column("make", sa.String(100)),
        sa.Column("model", sa.String(100)),
        sa.Column("year", sa.Integer()),
        sa.Column("license_plate", sa.String(30)),
        sa.Column("vin", sa.String(30)),
        sa.Column("color", sa.String(50)),
        sa.Column("battery_kwh", sa.Float()),
        sa.Column("max_ac_kw", sa.Float()),
        sa.Column("max_dc_kw", sa.Float()),
        sa.Column("wltp_range_km", sa.Integer()),
        sa.Column("wltp_consumption", sa.Float()),
        sa.Column("lease_company", sa.String(100)),
        sa.Column("lease_contract", sa.String(100)),
        sa.Column("lease_start", sa.Date()),
        sa.Column("lease_end", sa.Date()),
        sa.Column("lease_km_per_year", sa.Integer()),
        sa.Column("lease_monthly_cost", sa.Float()),
        sa.Column("initial_odometer", sa.Integer()),
        sa.Column("notes", sa.Text()),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "charge_sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("car_id", sa.Integer(), sa.ForeignKey("cars.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("charge_date", sa.Date(), nullable=False),
        sa.Column("odometer", sa.Integer()),
        sa.Column("location", sa.String(200)),
        sa.Column("provider", sa.String(200)),
        sa.Column("kwh", sa.Float(), nullable=False),
        sa.Column("start_time", sa.Time()),
        sa.Column("end_time", sa.Time()),
        sa.Column("to_reimburse", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("cost", sa.Float()),
        sa.Column("notes", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_charge_sessions_car_id", "charge_sessions", ["car_id"])
    op.create_index("ix_charge_sessions_charge_date", "charge_sessions", ["charge_date"])


def downgrade() -> None:
    op.drop_table("charge_sessions")
    op.drop_table("cars")

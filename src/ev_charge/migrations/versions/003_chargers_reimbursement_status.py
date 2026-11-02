"""Chargers (priced at the home tariff, reimbursed by a company) and the reimbursement status of sessions

Revision ID: 003
Revises: 002
Create Date: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "003"
down_revision: str | None = "002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chargers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column("brand", sa.String(100)),
        sa.Column("model", sa.String(100)),
        sa.Column("serial_number", sa.String(100)),
        sa.Column("reimbursement_company", sa.String(100)),
        sa.Column("uses_home_tariff", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("notes", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    with op.batch_alter_table("charge_sessions") as batch:
        batch.add_column(sa.Column("charger_id", sa.Integer()))
        batch.add_column(sa.Column("reimbursement_status", sa.String(20)))
        batch.create_foreign_key("fk_charge_sessions_charger", "chargers", ["charger_id"], ["id"], ondelete="RESTRICT")
        batch.create_index("ix_charge_sessions_charger_id", ["charger_id"])
    # Sessions already flagged "to be reimbursed" start as "not recorded" (bulk-change them on the Sessions page)
    op.execute("UPDATE charge_sessions SET reimbursement_status = 'not_recorded' WHERE to_reimburse")
    # Pricing now follows the charger: the "home locations" list is no longer used
    op.execute("DELETE FROM settings WHERE key = 'home_locations'")


def downgrade() -> None:
    with op.batch_alter_table("charge_sessions") as batch:
        batch.drop_index("ix_charge_sessions_charger_id")
        batch.drop_constraint("fk_charge_sessions_charger", type_="foreignkey")
        batch.drop_column("reimbursement_status")
        batch.drop_column("charger_id")
    op.drop_table("chargers")

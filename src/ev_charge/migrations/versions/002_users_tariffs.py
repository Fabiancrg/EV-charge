"""Users and roles, sign-in sessions, home charging tariffs, settings

Revision ID: 002
Revises: 001
Create Date: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "002"
down_revision: str | None = "001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(50), nullable=False, unique=True),
        sa.Column("full_name", sa.String(100)),
        sa.Column("email", sa.String(200)),
        sa.Column("role", sa.String(20), nullable=False, server_default="driver"),
        sa.Column("password_hash", sa.String(300), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_login", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "user_cars",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("car_id", sa.Integer(), sa.ForeignKey("cars.id", ondelete="CASCADE"), primary_key=True),
    )
    op.create_table(
        "auth_sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])
    op.create_table(
        "tariffs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date()),
        sa.Column("price_per_kwh", sa.Float(), nullable=False),
        sa.Column("label", sa.String(100)),
        sa.Column("notes", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_tariffs_valid_from", "tariffs", ["valid_from"])
    op.create_table(
        "settings",
        sa.Column("key", sa.String(50), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
    )
    with op.batch_alter_table("charge_sessions") as batch:
        batch.add_column(sa.Column("created_by_id", sa.Integer()))
        batch.create_foreign_key("fk_charge_sessions_created_by", "users", ["created_by_id"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    with op.batch_alter_table("charge_sessions") as batch:
        batch.drop_constraint("fk_charge_sessions_created_by", type_="foreignkey")
        batch.drop_column("created_by_id")
    op.drop_table("settings")
    op.drop_table("tariffs")
    op.drop_table("auth_sessions")
    op.drop_table("user_cars")
    op.drop_table("users")

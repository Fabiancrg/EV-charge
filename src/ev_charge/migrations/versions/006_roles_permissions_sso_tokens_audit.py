"""Roles and permissions (global and per car), single sign-on link, API tokens, sign-in details, audit log

Revision ID: 006
Revises: 005
Create Date: 2026-11-02

The former roles become built-in roles: "admin" → Administrator, "driver" → Driver (global role and
role on each car they had).
"""

import json
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "006"
down_revision: str | None = "005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copy of the built-in roles at this revision (permissions.py may evolve)
BUILTIN_ROLES = [
    ("Administrator", "Everything, on every car", ["*"], True),
    ("Driver", "Records, edits and imports the sessions of the cars given to them", ["car:view", "sessions:create", "sessions:edit", "sessions:import", "reimbursement:manage", "tokens:create"], False),
    ("Viewer", "Sees the cars given to them, their sessions and reports", ["car:view"], False),
    ("Accountant", "Sees every car and follows the reimbursements (status, statements)", ["car:view", "reimbursement:manage"], True),
]


def upgrade() -> None:
    roles = op.create_table(
        "roles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(50), nullable=False, unique=True),
        sa.Column("description", sa.String(300)),
        sa.Column("permissions", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("all_cars", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("builtin", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    now = datetime.now(UTC)
    op.bulk_insert(
        roles,
        [{"id": i + 1, "name": n, "description": d, "permissions": json.dumps(p), "all_cars": a, "builtin": True, "created_at": now} for i, (n, d, p, a) in enumerate(BUILTIN_ROLES)],
    )
    # Plain ADD COLUMN (the users table is referenced by other tables): no foreign key at database level
    op.add_column("users", sa.Column("role_id", sa.Integer()))
    op.add_column("users", sa.Column("oidc_issuer", sa.String(300)))
    op.add_column("users", sa.Column("oidc_subject", sa.String(300)))
    op.create_index("ix_users_oidc_subject", "users", ["oidc_subject"])
    op.execute("UPDATE users SET role_id = CASE WHEN role = 'admin' THEN 1 ELSE 2 END")
    op.add_column("user_cars", sa.Column("role_id", sa.Integer()))
    op.execute("UPDATE user_cars SET role_id = 2")
    for col in (sa.Column("last_seen_at", sa.DateTime(timezone=True)), sa.Column("ip", sa.String(64)), sa.Column("user_agent", sa.String(300)), sa.Column("method", sa.String(20))):
        op.add_column("auth_sessions", col)
    op.create_table(
        "api_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("prefix", sa.String(16), nullable=False),
        sa.Column("permissions", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("revoked", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_api_tokens_user_id", "api_tokens", ["user_id"])
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_id", sa.Integer()),
        sa.Column("username", sa.String(100)),
        sa.Column("action", sa.String(50), nullable=False),
        sa.Column("target", sa.String(200)),
        sa.Column("details", sa.Text()),
        sa.Column("ip", sa.String(64)),
    )
    op.create_index("ix_audit_log_at", "audit_log", ["at"])
    op.create_index("ix_audit_log_user_id", "audit_log", ["user_id"])
    op.create_index("ix_audit_log_action", "audit_log", ["action"])


def downgrade() -> None:
    op.drop_table("audit_log")
    op.drop_table("api_tokens")
    with op.batch_alter_table("auth_sessions") as batch:
        for col in ("method", "user_agent", "ip", "last_seen_at"):
            batch.drop_column(col)
    with op.batch_alter_table("user_cars") as batch:
        batch.drop_column("role_id")
    op.drop_index("ix_users_oidc_subject", "users")
    with op.batch_alter_table("users") as batch:
        for col in ("oidc_subject", "oidc_issuer", "role_id"):
            batch.drop_column(col)
    op.drop_table("roles")

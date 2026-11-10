"""A line of a reimbursement document can cover several sessions: the link moves to the session

Revision ID: 009
Revises: 008
Create Date: 2026-11-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "009"
down_revision: str | None = "008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("charge_sessions", sa.Column("reimbursement_line_id", sa.Integer()))
    op.create_index("ix_charge_sessions_reimbursement_line_id", "charge_sessions", ["reimbursement_line_id"])
    op.execute(
        "UPDATE charge_sessions SET reimbursement_line_id = (SELECT l.id FROM reimbursement_lines l WHERE l.session_id = charge_sessions.id) "
        "WHERE id IN (SELECT session_id FROM reimbursement_lines WHERE session_id IS NOT NULL)"
    )
    # Rebuilt without session_id and its unique constraint
    with op.batch_alter_table("reimbursement_lines", recreate="always", table_args=[], reflect_kwargs={"resolve_fks": False}) as batch:
        batch.drop_column("session_id")


def downgrade() -> None:
    op.add_column("reimbursement_lines", sa.Column("session_id", sa.Integer()))
    op.execute("UPDATE reimbursement_lines SET session_id = (SELECT MIN(s.id) FROM charge_sessions s WHERE s.reimbursement_line_id = reimbursement_lines.id)")
    op.drop_index("ix_charge_sessions_reimbursement_line_id", "charge_sessions")
    with op.batch_alter_table("charge_sessions") as batch:
        batch.drop_column("reimbursement_line_id")

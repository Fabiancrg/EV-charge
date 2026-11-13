"""Language of each user, of a sign-up request and of an invitation (for their e-mails)

Revision ID: 013
Revises: 012
Create Date: 2026-11-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "013"
down_revision: str | None = "012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table in ("users", "signup_requests", "invitations"):
        op.add_column(table, sa.Column("language", sa.String(5)))


def downgrade() -> None:
    for table in ("invitations", "signup_requests", "users"):
        with op.batch_alter_table(table) as batch:
            batch.drop_column("language")

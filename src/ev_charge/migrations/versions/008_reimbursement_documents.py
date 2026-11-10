"""Reimbursement documents (invoices from the reimbursing companies), their lines and payment

Revision ID: 008
Revises: 007
Create Date: 2026-11-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "008"
down_revision: str | None = "007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reimbursement_documents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(30), nullable=False),
        sa.Column("number", sa.String(100), nullable=False),
        sa.Column("issuer", sa.String(200)),
        sa.Column("document_date", sa.Date()),
        sa.Column("due_date", sa.Date()),
        sa.Column("period", sa.String(20)),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("kwh", sa.Float(), nullable=False),
        sa.Column("filename", sa.String(255)),
        sa.Column("charger_id", sa.Integer(), sa.ForeignKey("chargers.id", ondelete="SET NULL")),
        sa.Column("paid_date", sa.Date()),
        sa.Column("paid_amount", sa.Float()),
        sa.Column("notes", sa.Text()),
        sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source", "number", name="uq_reimbursement_documents_source_number"),
    )
    op.create_table(
        "reimbursement_lines",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("document_id", sa.Integer(), sa.ForeignKey("reimbursement_documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("external_id", sa.String(100)),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime()),
        sa.Column("kwh", sa.Float(), nullable=False),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("charge_sessions.id", ondelete="SET NULL"), unique=True),
    )
    op.create_index("ix_reimbursement_lines_document_id", "reimbursement_lines", ["document_id"])


def downgrade() -> None:
    op.drop_index("ix_reimbursement_lines_document_id", "reimbursement_lines")
    op.drop_table("reimbursement_lines")
    op.drop_table("reimbursement_documents")

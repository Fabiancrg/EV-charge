"""API tokens managed by administrators: their own access (a role or permissions) and the cars they may access

Revision ID: 007
Revises: 006
Create Date: 2026-11-02

Existing (personal) tokens keep their permissions and get the cars their creator could access.
The "tokens:create" permission becomes "tokens:manage" (Settings → API tokens), given to no built-in
role but Administrator ("*").
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "007"
down_revision: str | None = "006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("api_tokens", sa.Column("description", sa.String(300)))
    op.add_column("api_tokens", sa.Column("role_id", sa.Integer()))
    op.add_column("api_tokens", sa.Column("car_ids", sa.Text()))
    conn = op.get_bind()
    for rid, perms in conn.execute(sa.text("SELECT id, permissions FROM roles")).all():
        current = json.loads(perms or "[]")
        if "tokens:create" in current:
            conn.execute(sa.text("UPDATE roles SET permissions = :p WHERE id = :i"), {"p": json.dumps([p for p in current if p != "tokens:create"]), "i": rid})
    roles = {rid: (json.loads(perms or "[]"), bool(all_cars)) for rid, perms, all_cars in conn.execute(sa.text("SELECT id, permissions, all_cars FROM roles")).all()}
    for tid, uid in conn.execute(sa.text("SELECT id, user_id FROM api_tokens")).all():
        role_id = conn.execute(sa.text("SELECT role_id FROM users WHERE id = :u"), {"u": uid}).scalar()
        perms, all_cars = roles.get(role_id, ([], False))
        if all_cars or "*" in perms:
            continue  # all cars (car_ids stays NULL)
        cars = [c for (c,) in conn.execute(sa.text("SELECT car_id FROM user_cars WHERE user_id = :u AND role_id IS NOT NULL"), {"u": uid}).all()]
        conn.execute(sa.text("UPDATE api_tokens SET car_ids = :c WHERE id = :t"), {"c": json.dumps(cars), "t": tid})


def downgrade() -> None:
    with op.batch_alter_table("api_tokens") as batch:
        for col in ("car_ids", "role_id", "description"):
            batch.drop_column(col)

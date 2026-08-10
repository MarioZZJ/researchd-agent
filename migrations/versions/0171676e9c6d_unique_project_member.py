"""unique project member

Revision ID: 0171676e9c6d
Revises: 4ca065a16724
Create Date: 2026-08-11 03:42:41.605701
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0171676e9c6d'
down_revision: Union[str, None] = '4ca065a16724'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SQLite cannot ALTER TABLE to add a constraint; rebuild the table via
    # batch mode. Existing duplicates are checked first: the migration FAILS
    # loudly instead of silently dropping rows.
    conn = op.get_bind()
    dup = conn.execute(
        sa.text(
            "SELECT project_id, platform_user_id, COUNT(*) AS n "
            "FROM project_members GROUP BY project_id, platform_user_id HAVING n > 1"
        )
    ).fetchall()
    if dup:
        rows = ", ".join(f"{r[0]}={r[1]}" for r in dup[:5])
        raise RuntimeError(
            f"cannot add unique constraint: duplicate members exist ({rows}); "
            "deduplicate project_members first"
        )
    with op.batch_alter_table("project_members") as batch_op:
        batch_op.create_unique_constraint(
            "uq_project_member_user", ["project_id", "platform_user_id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("project_members") as batch_op:
        batch_op.drop_constraint("uq_project_member_user", type_="unique")

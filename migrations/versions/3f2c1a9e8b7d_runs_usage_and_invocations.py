"""runs.usage_json + invocations table (model-call traceability)

Revision ID: 3f2c1a9e8b7d
Revises: 0171676e9c6d
Create Date: 2026-08-11 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '3f2c1a9e8b7d'
down_revision: Union[str, None] = '0171676e9c6d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # every worker/auditor run records what it actually consumed (or that it
    # is unavailable — never fabricated)
    with op.batch_alter_table("runs") as batch_op:
        batch_op.add_column(sa.Column("usage_json", sa.JSON(), nullable=True))
    # reporter persists the FULL previous state snapshot (not just a hash) so
    # the next tick can compute a real semantic diff
    with op.batch_alter_table("projection_states") as batch_op:
        batch_op.add_column(sa.Column("snapshot_json", sa.JSON(), nullable=True))
    # planner invocations have no task/run row (planner is project-level):
    # this table makes EVERY model call traceable end-to-end
    op.create_table(
        "invocations",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("invocation_id", sa.String(64), nullable=False, unique=True),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False, index=True),
        sa.Column("task_id", sa.String(64), nullable=True),
        sa.Column("run_id", sa.String(64), nullable=True),
        sa.Column("context_id", sa.String(64), nullable=True),
        sa.Column("profile_name", sa.String(128), nullable=True),
        sa.Column("resolved_model", sa.String(256), nullable=True),
        sa.Column("reasoning_effort", sa.String(32), nullable=True),
        sa.Column("skills_json", sa.JSON(), nullable=True),
        sa.Column("budget_json", sa.JSON(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("usage_json", sa.JSON(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("invocations")
    with op.batch_alter_table("projection_states") as batch_op:
        batch_op.drop_column("snapshot_json")
    with op.batch_alter_table("runs") as batch_op:
        batch_op.drop_column("usage_json")

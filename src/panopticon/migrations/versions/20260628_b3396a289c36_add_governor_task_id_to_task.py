"""add governor_task_id to task

Revision ID: b3396a289c36
Revises: 5066c0371860
Create Date: 2026-06-28 15:19:47.791171
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3396a289c36"
down_revision: str | None = "6b0aa0ff9270"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("task", schema=None) as batch_op:
        batch_op.add_column(sa.Column("governor_task_id", sa.String(), nullable=True))
        batch_op.create_foreign_key(
            "fk_task_governor_task_id", "task", ["governor_task_id"], ["id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("task", schema=None) as batch_op:
        batch_op.drop_constraint("fk_task_governor_task_id", type_="foreignkey")
        batch_op.drop_column("governor_task_id")

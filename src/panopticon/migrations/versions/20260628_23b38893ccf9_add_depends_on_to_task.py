"""add depends_on to task

Revision ID: 23b38893ccf9
Revises: 5066c0371860
Create Date: 2026-06-28 15:29:02.944538
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "23b38893ccf9"
down_revision: str | None = "b3396a289c36"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Task IDs that must complete before work on this task should begin (tracking only).
    with op.batch_alter_table("task", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("depends_on_task_ids", sa.JSON(), nullable=False, server_default="[]")
        )


def downgrade() -> None:
    with op.batch_alter_table("task", schema=None) as batch_op:
        batch_op.drop_column("depends_on_task_ids")

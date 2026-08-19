"""add task sort_weight

Revision ID: 2ed7040257db
Revises: 2c508a18a45e
Create Date: 2026-08-12 15:09:36.492503
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2ed7040257db"
down_revision: str | None = "2c508a18a45e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("task", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("sort_weight", sa.Integer(), server_default="0", nullable=False)
        )


def downgrade() -> None:
    with op.batch_alter_table("task", schema=None) as batch_op:
        batch_op.drop_column("sort_weight")

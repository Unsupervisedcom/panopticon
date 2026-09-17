"""add task push record

Revision ID: c9b523b10fff
Revises: f634214d376c
Create Date: 2026-09-17 19:05:43.378649
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9b523b10fff"
down_revision: str | None = "f634214d376c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("task", schema=None) as batch_op:
        batch_op.add_column(sa.Column("push", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("task", schema=None) as batch_op:
        batch_op.drop_column("push")

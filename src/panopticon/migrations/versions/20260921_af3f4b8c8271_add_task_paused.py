"""add task paused

Revision ID: af3f4b8c8271
Revises: 2ed7040257db
Create Date: 2026-09-21 15:33:25.185991
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "af3f4b8c8271"
down_revision: str | None = "2ed7040257db"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # `server_default` is required, not cosmetic: the column is NOT NULL and every existing row
    # needs a value at ALTER time. Autogenerate omits it (it only sees the ORM default, which is
    # Python-side and applies to new inserts only), so adding it here is the "please adjust" step —
    # without it this migration fails on any non-empty task table.
    with op.batch_alter_table("task", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("paused", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    with op.batch_alter_table("task", schema=None) as batch_op:
        batch_op.drop_column("paused")

"""add updated_at to task

Revision ID: ed8efc0b01ac
Revises: 9644848c9433
Create Date: 2026-06-25 23:27:21.760363
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ed8efc0b01ac"
down_revision: str | None = "9644848c9433"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("task", schema=None) as batch_op:
        batch_op.add_column(sa.Column("updated_at", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("task", schema=None) as batch_op:
        batch_op.drop_column("updated_at")

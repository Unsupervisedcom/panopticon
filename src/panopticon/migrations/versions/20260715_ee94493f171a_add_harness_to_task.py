"""add harness to task

Revision ID: ee94493f171a
Revises: 8a9ab3fe49b5
Create Date: 2026-07-15 09:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ee94493f171a"
down_revision: str | None = "8a9ab3fe49b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("task", schema=None) as batch_op:
        batch_op.add_column(sa.Column("harness", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("task", schema=None) as batch_op:
        batch_op.drop_column("harness")

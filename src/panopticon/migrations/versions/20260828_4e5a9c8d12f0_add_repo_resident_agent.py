"""add repo resident agent

Revision ID: 4e5a9c8d12f0
Revises: 8a9ab3fe49b5
Create Date: 2026-08-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4e5a9c8d12f0"
down_revision: str | None = "8a9ab3fe49b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("repo", schema=None) as batch_op:
        batch_op.add_column(sa.Column("resident_agent", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("repo", schema=None) as batch_op:
        batch_op.drop_column("resident_agent")

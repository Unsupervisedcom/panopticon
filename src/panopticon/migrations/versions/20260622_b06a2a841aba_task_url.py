"""task url

Revision ID: b06a2a841aba
Revises: 67745f3c1197
Create Date: 2026-06-22 16:49:32.789829
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b06a2a841aba"
down_revision: str | None = "67745f3c1197"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # An optional external URL for the task (its PR, an issue, …); the dashboard's `p` hotkey opens it.
    with op.batch_alter_table("task", schema=None) as batch_op:
        batch_op.add_column(sa.Column("url", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("task", schema=None) as batch_op:
        batch_op.drop_column("url")

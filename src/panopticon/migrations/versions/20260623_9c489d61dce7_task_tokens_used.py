"""task tokens_used

Revision ID: 9c489d61dce7
Revises: b06a2a841aba
Create Date: 2026-06-23 16:00:55.517763
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9c489d61dce7"
down_revision: str | None = "b06a2a841aba"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Cumulative tokens the container's claude has used; the Stop hook reports it (None until then).
    with op.batch_alter_table("task", schema=None) as batch_op:
        batch_op.add_column(sa.Column("tokens_used", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("task", schema=None) as batch_op:
        batch_op.drop_column("tokens_used")

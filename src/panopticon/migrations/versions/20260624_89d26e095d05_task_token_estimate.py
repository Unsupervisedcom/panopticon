"""task token_estimate

Revision ID: 89d26e095d05
Revises: 848d53aeb6c7
Create Date: 2026-06-24 15:09:33.469134
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "89d26e095d05"
down_revision: str | None = "848d53aeb6c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The agent's forecast of total tokens this task will consume, set in planning (None until then).
    with op.batch_alter_table("task", schema=None) as batch_op:
        batch_op.add_column(sa.Column("token_estimate", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("task", schema=None) as batch_op:
        batch_op.drop_column("token_estimate")

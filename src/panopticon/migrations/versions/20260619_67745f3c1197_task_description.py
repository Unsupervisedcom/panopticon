"""task description

Revision ID: 67745f3c1197
Revises: 63c13e069b98
Create Date: 2026-06-19 15:10:29.914247
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '67745f3c1197'
down_revision: str | None = '63c13e069b98'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The free-text description captured at task creation (nullable, immutable thereafter).
    with op.batch_alter_table('task', schema=None) as batch_op:
        batch_op.add_column(sa.Column('description', sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('task', schema=None) as batch_op:
        batch_op.drop_column('description')

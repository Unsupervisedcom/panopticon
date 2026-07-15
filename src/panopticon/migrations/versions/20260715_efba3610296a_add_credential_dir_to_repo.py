"""add credential_dir to repo

Revision ID: efba3610296a
Revises: ee94493f171a
Create Date: 2026-07-15 09:31:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "efba3610296a"
down_revision: str | None = "ee94493f171a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("repo", schema=None) as batch_op:
        batch_op.add_column(sa.Column("credential_dir", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("repo", schema=None) as batch_op:
        batch_op.drop_column("credential_dir")

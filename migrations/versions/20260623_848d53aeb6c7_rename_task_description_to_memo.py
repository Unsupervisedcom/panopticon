"""rename task description to memo

Revision ID: 848d53aeb6c7
Revises: 9c489d61dce7
Create Date: 2026-06-23 23:40:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '848d53aeb6c7'
down_revision: str | None = '9c489d61dce7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Rename the free-text intent column captured at task creation: description -> memo. A rename
    # (not drop+add) so any existing values are preserved.
    with op.batch_alter_table('task', schema=None) as batch_op:
        batch_op.alter_column('description', new_column_name='memo')


def downgrade() -> None:
    with op.batch_alter_table('task', schema=None) as batch_op:
        batch_op.alter_column('memo', new_column_name='description')

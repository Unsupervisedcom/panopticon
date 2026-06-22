"""repo capabilities and image_layer

Revision ID: 63c13e069b98
Revises: a0a748c95d54
Create Date: 2026-06-19 02:17:09.828589
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '63c13e069b98'
down_revision: str | None = 'a0a748c95d54'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # `capabilities` is NOT NULL; give it a server default ('{}') so the column can be added to a
    # `repo` table that already has rows (the ORM supplies dict() going forward — this is just to
    # backfill existing data on upgrade). The drift guard compares type + nullability, not defaults.
    with op.batch_alter_table('repo', schema=None) as batch_op:
        batch_op.add_column(sa.Column('image_layer', sa.String(), nullable=True))
        batch_op.add_column(
            sa.Column('capabilities', sa.JSON(), nullable=False, server_default=sa.text("'{}'"))
        )


def downgrade() -> None:
    with op.batch_alter_table('repo', schema=None) as batch_op:
        batch_op.drop_column('capabilities')
        batch_op.drop_column('image_layer')

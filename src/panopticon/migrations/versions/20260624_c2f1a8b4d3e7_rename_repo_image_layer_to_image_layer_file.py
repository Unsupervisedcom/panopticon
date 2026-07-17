"""rename repo image_layer to image_layer_file

Revision ID: c2f1a8b4d3e7
Revises: 89d26e095d05
Create Date: 2026-06-24 17:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c2f1a8b4d3e7"
down_revision: str | None = "89d26e095d05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The repo Dockerfile layer column now holds a *file reference* (a name under the layers dir)
    # rather than inline content: image_layer -> image_layer_file. A rename (not drop+add) so any
    # existing values carry over (operators repoint them at a layer file).
    with op.batch_alter_table("repo", schema=None) as batch_op:
        batch_op.alter_column("image_layer", new_column_name="image_layer_file")


def downgrade() -> None:
    with op.batch_alter_table("repo", schema=None) as batch_op:
        batch_op.alter_column("image_layer_file", new_column_name="image_layer")

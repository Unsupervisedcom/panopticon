"""relativize repo env_file

Revision ID: 8a9ab3fe49b5
Revises: faf3403c8b56
Create Date: 2026-07-12 16:19:03.525015
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import PurePosixPath

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8a9ab3fe49b5"
down_revision: str | None = "faf3403c8b56"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# The `repo.env_file` column now holds a *name relative to the secrets dir* rather than an absolute
# host path, so it resolves on whichever host runs the task — remote-runner support (ADR 0007). No
# schema change (the column stays a nullable String); this is a data migration that strips any
# stored value down to its basename. Values already relative are untouched. A nested subpath under
# the secrets dir would lose its directory here — that rare case needs operator re-pointing.
def upgrade() -> None:
    repo = sa.table("repo", sa.column("id", sa.String), sa.column("env_file", sa.String))
    conn = op.get_bind()
    rows = conn.execute(sa.select(repo.c.id, repo.c.env_file)).fetchall()
    for repo_id, env_file in rows:
        if not env_file:
            continue
        name = PurePosixPath(env_file.replace("\\", "/")).name
        if name != env_file:
            conn.execute(repo.update().where(repo.c.id == repo_id).values(env_file=name))


def downgrade() -> None:
    # Irreversible: the original absolute path isn't recoverable from the basename. Leaving the
    # relative name in place is the safe no-op (it's still a valid env_file reference).
    pass

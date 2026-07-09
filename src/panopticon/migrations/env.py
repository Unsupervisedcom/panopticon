"""Alembic migration environment for panopticon's task store.

Wired to the SQLAlchemy adapter's :data:`~panopticon.taskservice.store_sqlalchemy.metadata` so
``alembic revision --autogenerate`` diffs migrations against the live ORM schema (the single
source of truth). The database URL is resolved the same way the task service resolves it — from
``$PANOPTICON_DB`` (default ``~/.panopticon/panopticon.db``) — or overridden per-invocation with
``alembic -x db=<url>``, so the migration tooling and the running service never disagree on the
target database.

LLM-free, like the rest of the control plane.
"""

from __future__ import annotations

import os
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

from panopticon.taskservice.__main__ import DEFAULT_DB, migrate_db_to_home
from panopticon.taskservice.store_sqlalchemy import metadata

config = context.config

# Resolve the target database: `-x db=<url>` wins, then $PANOPTICON_DB, then the service default.
_db_url = context.get_x_argument(as_dictionary=True).get("db") or os.environ.get(
    "PANOPTICON_DB", DEFAULT_DB
)
config.set_main_option("sqlalchemy.url", _db_url)

# Migrate legacy ./panopticon.db to ~/.panopticon/ before alembic opens the file — `make host`
# runs `make migrate` before starting the service, so migration must fire here too.
migrate_db_to_home(_db_url)

# For SQLite file DBs, ensure the parent directory exists before SQLAlchemy tries to open the file.
_SQLITE_PREFIX = "sqlite:///"
if _db_url.startswith(_SQLITE_PREFIX):
    _db_path = _db_url[len(_SQLITE_PREFIX):]
    if _db_path and _db_path != ":memory:":
        Path(_db_path).parent.mkdir(parents=True, exist_ok=True)

# Autogenerate + `upgrade head` on a fresh DB both diff against the ORM's declared schema.
target_metadata = metadata


def run_migrations_offline() -> None:
    """Emit SQL to a script without a live DB connection (``alembic upgrade --sql``)."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # SQLite can't ALTER most columns in place; batch mode recreates the table instead.
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live DB connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

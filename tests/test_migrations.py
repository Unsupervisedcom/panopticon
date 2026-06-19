"""Migration tests — the Alembic migrations are kept honest against the ORM schema.

The SQLAlchemy adapter bootstraps fresh databases with ``metadata.create_all`` (zero-config dev +
the in-memory test engine); Alembic owns versioned evolution (ADR 0001 §3). These two MUST agree,
or a deployment migrated with ``alembic upgrade head`` would diverge from what the code expects.

So we apply the migrations to one empty database and ``create_all`` to another, then compare the
two schemas by reflection. This is the migration analogue of ``test_store``'s domain/persistence
sync guard: regenerate the migration (``make migrate-revision``) whenever the ORM schema changes
and this test will hold the line. No LLM, no network.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from panopticon.taskservice.store_sqlalchemy import metadata

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _alembic_config(db_url: str) -> Config:
    """An Alembic config pinned to this repo's migrations and a specific database URL."""
    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    # Absolute paths so the test is independent of the working directory pytest runs from.
    cfg.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    # env.py resolves the URL from `-x db=...` first; this is how we point it at the temp DB.
    cfg.cmd_opts = type("opts", (), {"x": [f"db={db_url}"]})()  # mimic `alembic -x db=<url>`
    return cfg


def _schema_snapshot(db_url: str) -> dict[str, Any]:
    """A reflection-based, comparable snapshot of every table's columns, PKs and FKs."""
    engine = create_engine(db_url)
    try:
        insp = inspect(engine)
        snapshot: dict[str, Any] = {}
        for table in sorted(insp.get_table_names()):
            if table == "alembic_version":  # Alembic's bookkeeping table; not part of the schema.
                continue
            columns = {
                col["name"]: {"type": str(col["type"]), "nullable": col["nullable"]}
                for col in insp.get_columns(table)
            }
            pk = sorted(insp.get_pk_constraint(table)["constrained_columns"])
            fks = sorted(
                (tuple(fk["constrained_columns"]), fk["referred_table"], tuple(fk["referred_columns"]))
                for fk in insp.get_foreign_keys(table)
            )
            snapshot[table] = {"columns": columns, "pk": pk, "fks": fks}
        return snapshot
    finally:
        engine.dispose()


def test_migrations_match_orm_schema(tmp_path: Path) -> None:
    """`alembic upgrade head` on an empty DB yields the same schema as `metadata.create_all`."""
    migrated_url = f"sqlite:///{tmp_path / 'migrated.db'}"
    command.upgrade(_alembic_config(migrated_url), "head")

    create_all_url = f"sqlite:///{tmp_path / 'create_all.db'}"
    engine = create_engine(create_all_url)
    metadata.create_all(engine)
    engine.dispose()

    assert _schema_snapshot(migrated_url) == _schema_snapshot(create_all_url)


def test_migrations_roundtrip(tmp_path: Path) -> None:
    """upgrade → downgrade → upgrade leaves no schema behind on downgrade and is repeatable."""
    url = f"sqlite:///{tmp_path / 'roundtrip.db'}"
    cfg = _alembic_config(url)

    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    assert _schema_snapshot(url) == {}  # downgrade tears every table back down

    command.upgrade(cfg, "head")  # and it's idempotent enough to re-apply cleanly
    assert set(_schema_snapshot(url)) == {"repo", "task", "history", "responsibility"}


def test_single_head() -> None:
    """Exactly one migration head — branching revisions are a merge hazard, fail loudly."""
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(_alembic_config("sqlite://"))
    assert len(script.get_heads()) == 1, "multiple Alembic heads; merge them"

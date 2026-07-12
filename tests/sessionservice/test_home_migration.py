"""Tests for automatic migration of legacy data to the XDG data dir.

Two upgrade hops are covered:
- CWD-relative (pre-#251) → XDG
- ``~/.panopticon/`` (#251) → XDG

The task service and alembic both call these migration helpers on startup so that existing
users' data moves to the new location transparently on first run after upgrading.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# migrate_db_to_home
# ---------------------------------------------------------------------------

def _make_fake_db_url(tmp_path: Path) -> str:
    return "sqlite:///" + str(tmp_path / "home" / ".panopticon" / "panopticon.db")


def test_migrate_db_moves_old_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    old = tmp_path / "panopticon.db"
    old.write_bytes(b"sqlite-data")

    fake_url = _make_fake_db_url(tmp_path)
    monkeypatch.setattr("panopticon.taskservice.__main__.DB_URL", fake_url)

    from panopticon.taskservice.__main__ import migrate_db_to_home
    migrate_db_to_home(fake_url)

    new = Path(fake_url[len("sqlite:///"):])
    assert new.is_file()
    assert new.read_bytes() == b"sqlite-data"
    assert not old.exists()


def test_migrate_db_skips_when_new_already_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    old = tmp_path / "panopticon.db"
    old.write_bytes(b"old")

    fake_url = _make_fake_db_url(tmp_path)
    new = Path(fake_url[len("sqlite:///"):])
    new.parent.mkdir(parents=True)
    new.write_bytes(b"existing")
    monkeypatch.setattr("panopticon.taskservice.__main__.DB_URL", fake_url)

    from panopticon.taskservice.__main__ import migrate_db_to_home
    migrate_db_to_home(fake_url)

    assert old.exists()           # not moved
    assert new.read_bytes() == b"existing"  # not overwritten


def test_migrate_db_noop_when_old_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    fake_url = _make_fake_db_url(tmp_path)
    monkeypatch.setattr("panopticon.taskservice.__main__.DB_URL", fake_url)

    from panopticon.taskservice.__main__ import migrate_db_to_home
    migrate_db_to_home(fake_url)  # no error, no file created

    assert not Path(fake_url[len("sqlite:///"):]).exists()


def test_migrate_db_skips_custom_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    old = tmp_path / "panopticon.db"
    old.write_bytes(b"data")

    fake_url = _make_fake_db_url(tmp_path)
    monkeypatch.setattr("panopticon.taskservice.__main__.DB_URL", fake_url)

    from panopticon.taskservice.__main__ import migrate_db_to_home
    migrate_db_to_home("sqlite:////custom/other.db")  # custom URL, not the default

    assert old.exists()  # left alone


# ---------------------------------------------------------------------------
# _migrate_legacy_to_home — artifacts and layers
# ---------------------------------------------------------------------------

def _setup_legacy_dirs(tmp_path: Path) -> tuple[Path, Path]:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "tasks").mkdir()
    (artifacts / "tasks" / "a.txt").write_text("artifact")

    layers = tmp_path / "layers"
    layers.mkdir()
    (layers / "repo.dockerfile").write_text("FROM base")

    return artifacts, layers


def test_migrate_artifacts_moves_old_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _setup_legacy_dirs(tmp_path)

    fake_home = tmp_path / "home"
    fake_artifacts = str(fake_home / ".panopticon" / "artifacts")
    fake_layers = str(fake_home / ".panopticon" / "layers")
    fake_db = _make_fake_db_url(tmp_path)
    monkeypatch.setattr("panopticon.taskservice.__main__.DB_URL", fake_db)
    monkeypatch.setattr("panopticon.taskservice.__main__.ARTIFACTS_DIR", fake_artifacts)
    monkeypatch.setattr("panopticon.taskservice.__main__.LAYERS_DIR", fake_layers)

    from panopticon.taskservice.__main__ import _migrate_legacy_to_home
    _migrate_legacy_to_home(fake_db, fake_artifacts, fake_layers)

    new_artifacts = Path(fake_artifacts)
    assert new_artifacts.is_dir()
    assert (new_artifacts / "tasks" / "a.txt").read_text() == "artifact"
    assert not (tmp_path / "artifacts").exists()


def test_migrate_layers_moves_old_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _setup_legacy_dirs(tmp_path)

    fake_home = tmp_path / "home"
    fake_artifacts = str(fake_home / ".panopticon" / "artifacts")
    fake_layers = str(fake_home / ".panopticon" / "layers")
    fake_db = _make_fake_db_url(tmp_path)
    monkeypatch.setattr("panopticon.taskservice.__main__.DB_URL", fake_db)
    monkeypatch.setattr("panopticon.taskservice.__main__.ARTIFACTS_DIR", fake_artifacts)
    monkeypatch.setattr("panopticon.taskservice.__main__.LAYERS_DIR", fake_layers)

    from panopticon.taskservice.__main__ import _migrate_legacy_to_home
    _migrate_legacy_to_home(fake_db, fake_artifacts, fake_layers)

    new_layers = Path(fake_layers)
    assert new_layers.is_dir()
    assert (new_layers / "repo.dockerfile").read_text() == "FROM base"
    assert not (tmp_path / "layers").exists()


def test_migrate_skips_artifacts_when_new_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _setup_legacy_dirs(tmp_path)

    fake_home = tmp_path / "home"
    new_artifacts = fake_home / ".panopticon" / "artifacts"
    new_artifacts.mkdir(parents=True)
    (new_artifacts / "existing.txt").write_text("keep me")

    fake_artifacts = str(new_artifacts)
    fake_layers = str(fake_home / ".panopticon" / "layers")
    fake_db = _make_fake_db_url(tmp_path)
    monkeypatch.setattr("panopticon.taskservice.__main__.DB_URL", fake_db)
    monkeypatch.setattr("panopticon.taskservice.__main__.ARTIFACTS_DIR", fake_artifacts)
    monkeypatch.setattr("panopticon.taskservice.__main__.LAYERS_DIR", fake_layers)

    from panopticon.taskservice.__main__ import _migrate_legacy_to_home
    _migrate_legacy_to_home(fake_db, fake_artifacts, fake_layers)

    assert (tmp_path / "artifacts").exists()  # old not moved
    assert (new_artifacts / "existing.txt").read_text() == "keep me"  # new untouched


def test_migrate_skips_custom_artifacts_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "artifacts").mkdir()

    fake_home = tmp_path / "home"
    fake_artifacts = str(fake_home / ".panopticon" / "artifacts")
    fake_db = _make_fake_db_url(tmp_path)
    monkeypatch.setattr("panopticon.taskservice.__main__.DB_URL", fake_db)
    monkeypatch.setattr("panopticon.taskservice.__main__.ARTIFACTS_DIR", fake_artifacts)
    monkeypatch.setattr("panopticon.taskservice.__main__.LAYERS_DIR", str(fake_home / ".panopticon" / "layers"))

    from panopticon.taskservice.__main__ import _migrate_legacy_to_home
    _migrate_legacy_to_home(fake_db, "/custom/artifacts", str(fake_home / ".panopticon" / "layers"))

    assert (tmp_path / "artifacts").exists()  # left alone — custom path in use


# ---------------------------------------------------------------------------
# Second-hop migration: ~/.panopticon/ → XDG
# ---------------------------------------------------------------------------

def _make_xdg_db_url(tmp_path: Path) -> str:
    return "sqlite:///" + str(tmp_path / "xdg" / "panopticon" / "panopticon.db")


def test_migrate_db_moves_dot_panopticon_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """DB in ~/.panopticon/ (post-#251) is migrated to the XDG location."""
    monkeypatch.chdir(tmp_path)
    fake_home = tmp_path / "home"
    old = fake_home / ".panopticon" / "panopticon.db"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"sqlite-data-from-251")

    fake_url = _make_xdg_db_url(tmp_path)
    monkeypatch.setattr("panopticon.taskservice.__main__.DB_URL", fake_url)
    _run_migrate_db_second_hop(tmp_path, fake_url)

    new = Path(fake_url[len("sqlite:///"):])
    assert new.is_file()
    assert new.read_bytes() == b"sqlite-data-from-251"
    assert not old.exists()


def _run_migrate_db_second_hop(tmp_path: Path, fake_url: str) -> None:
    """Run migrate_db_to_home with Path.home() redirected to tmp_path/home."""
    import panopticon.taskservice.__main__ as m
    real_home = Path.home

    def fake_home() -> Path:
        return tmp_path / "home"

    original = Path.home
    try:
        Path.home = staticmethod(fake_home)  # type: ignore[method-assign]
        m.migrate_db_to_home(fake_url)
    finally:
        Path.home = staticmethod(original)  # type: ignore[method-assign]


def test_migrate_artifacts_moves_dot_panopticon_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Artifacts in ~/.panopticon/ (post-#251) are migrated to the XDG location."""
    fake_home = tmp_path / "home"
    old_artifacts = fake_home / ".panopticon" / "artifacts"
    old_artifacts.mkdir(parents=True)
    (old_artifacts / "tasks").mkdir()
    (old_artifacts / "tasks" / "b.txt").write_text("from-251")

    xdg_artifacts = str(tmp_path / "xdg" / "panopticon" / "artifacts")
    xdg_layers = str(tmp_path / "xdg" / "panopticon" / "layers")
    xdg_db = _make_xdg_db_url(tmp_path)
    monkeypatch.setattr("panopticon.taskservice.__main__.DB_URL", xdg_db)
    monkeypatch.setattr("panopticon.taskservice.__main__.ARTIFACTS_DIR", xdg_artifacts)
    monkeypatch.setattr("panopticon.taskservice.__main__.LAYERS_DIR", xdg_layers)

    import panopticon.taskservice.__main__ as m

    def fake_home_fn() -> Path:
        return fake_home

    original = Path.home
    try:
        Path.home = staticmethod(fake_home_fn)  # type: ignore[method-assign]
        m._migrate_legacy_to_home(xdg_db, xdg_artifacts, xdg_layers)
    finally:
        Path.home = staticmethod(original)  # type: ignore[method-assign]

    new = Path(xdg_artifacts)
    assert new.is_dir()
    assert (new / "tasks" / "b.txt").read_text() == "from-251"
    assert not old_artifacts.exists()


def test_migrate_layers_moves_dot_panopticon_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Layers in ~/.panopticon/ (post-#251) are migrated to the XDG location."""
    fake_home = tmp_path / "home"
    old_layers = fake_home / ".panopticon" / "layers"
    old_layers.mkdir(parents=True)
    (old_layers / "repo.dockerfile").write_text("FROM base-251")

    xdg_artifacts = str(tmp_path / "xdg" / "panopticon" / "artifacts")
    xdg_layers = str(tmp_path / "xdg" / "panopticon" / "layers")
    xdg_db = _make_xdg_db_url(tmp_path)
    monkeypatch.setattr("panopticon.taskservice.__main__.DB_URL", xdg_db)
    monkeypatch.setattr("panopticon.taskservice.__main__.ARTIFACTS_DIR", xdg_artifacts)
    monkeypatch.setattr("panopticon.taskservice.__main__.LAYERS_DIR", xdg_layers)

    import panopticon.taskservice.__main__ as m

    def fake_home_fn() -> Path:
        return fake_home

    original = Path.home
    try:
        Path.home = staticmethod(fake_home_fn)  # type: ignore[method-assign]
        m._migrate_legacy_to_home(xdg_db, xdg_artifacts, xdg_layers)
    finally:
        Path.home = staticmethod(original)  # type: ignore[method-assign]

    new = Path(xdg_layers)
    assert new.is_dir()
    assert (new / "repo.dockerfile").read_text() == "FROM base-251"
    assert not old_layers.exists()


# ---------------------------------------------------------------------------
# hooks and secrets: ~/.panopticon/ → XDG config dir
# ---------------------------------------------------------------------------

def _run_migrate_with_fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run _migrate_legacy_to_home with patched home and XDG env vars."""
    fake_home = tmp_path / "home"
    xdg_config = tmp_path / "config"
    xdg_db = "sqlite:///" + str(tmp_path / "xdg-data" / "panopticon" / "panopticon.db")
    xdg_artifacts = str(tmp_path / "xdg-data" / "panopticon" / "artifacts")
    xdg_layers = str(tmp_path / "xdg-data" / "panopticon" / "layers")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))
    monkeypatch.setattr("panopticon.taskservice.__main__.DB_URL", xdg_db)
    monkeypatch.setattr("panopticon.taskservice.__main__.ARTIFACTS_DIR", xdg_artifacts)
    monkeypatch.setattr("panopticon.taskservice.__main__.LAYERS_DIR", xdg_layers)

    import panopticon.taskservice.__main__ as m
    original = Path.home
    try:
        Path.home = staticmethod(lambda: fake_home)  # type: ignore[method-assign]
        m._migrate_legacy_to_home(xdg_db, xdg_artifacts, xdg_layers)
    finally:
        Path.home = staticmethod(original)  # type: ignore[method-assign]

    return fake_home, xdg_config  # type: ignore[return-value]


def test_migrate_hooks_moves_dot_panopticon_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """hooks/ in ~/.panopticon/ is migrated to $XDG_CONFIG_HOME/panopticon/hooks/."""
    fake_home = tmp_path / "home"
    old_hooks = fake_home / ".panopticon" / "hooks"
    old_hooks.mkdir(parents=True)
    (old_hooks / "post-commit").write_text("#!/bin/sh")

    xdg_config = tmp_path / "config"
    xdg_db = "sqlite:///" + str(tmp_path / "xdg-data" / "panopticon" / "panopticon.db")
    xdg_artifacts = str(tmp_path / "xdg-data" / "panopticon" / "artifacts")
    xdg_layers = str(tmp_path / "xdg-data" / "panopticon" / "layers")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))
    monkeypatch.setattr("panopticon.taskservice.__main__.DB_URL", xdg_db)
    monkeypatch.setattr("panopticon.taskservice.__main__.ARTIFACTS_DIR", xdg_artifacts)
    monkeypatch.setattr("panopticon.taskservice.__main__.LAYERS_DIR", xdg_layers)

    import panopticon.taskservice.__main__ as m
    original = Path.home
    try:
        Path.home = staticmethod(lambda: fake_home)  # type: ignore[method-assign]
        m._migrate_legacy_to_home(xdg_db, xdg_artifacts, xdg_layers)
    finally:
        Path.home = staticmethod(original)  # type: ignore[method-assign]

    new = xdg_config / "panopticon" / "hooks"
    assert new.is_dir()
    assert (new / "post-commit").read_text() == "#!/bin/sh"
    assert not old_hooks.exists()


def test_migrate_secrets_moves_dot_panopticon_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """secrets/ in ~/.panopticon/ is migrated to $XDG_CONFIG_HOME/panopticon/secrets/."""
    fake_home = tmp_path / "home"
    old_secrets = fake_home / ".panopticon" / "secrets"
    old_secrets.mkdir(parents=True)
    (old_secrets / "myrepo.env").write_text("SECRET=hunter2")

    xdg_config = tmp_path / "config"
    xdg_db = "sqlite:///" + str(tmp_path / "xdg-data" / "panopticon" / "panopticon.db")
    xdg_artifacts = str(tmp_path / "xdg-data" / "panopticon" / "artifacts")
    xdg_layers = str(tmp_path / "xdg-data" / "panopticon" / "layers")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))
    monkeypatch.setattr("panopticon.taskservice.__main__.DB_URL", xdg_db)
    monkeypatch.setattr("panopticon.taskservice.__main__.ARTIFACTS_DIR", xdg_artifacts)
    monkeypatch.setattr("panopticon.taskservice.__main__.LAYERS_DIR", xdg_layers)

    import panopticon.taskservice.__main__ as m
    original = Path.home
    try:
        Path.home = staticmethod(lambda: fake_home)  # type: ignore[method-assign]
        m._migrate_legacy_to_home(xdg_db, xdg_artifacts, xdg_layers)
    finally:
        Path.home = staticmethod(original)  # type: ignore[method-assign]

    new = xdg_config / "panopticon" / "secrets"
    assert new.is_dir()
    assert (new / "myrepo.env").read_text() == "SECRET=hunter2"
    assert not old_secrets.exists()


def test_migrate_hooks_skips_when_new_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """hooks/ migration is skipped when the XDG target already exists."""
    fake_home = tmp_path / "home"
    old_hooks = fake_home / ".panopticon" / "hooks"
    old_hooks.mkdir(parents=True)
    (old_hooks / "post-commit").write_text("old")

    xdg_config = tmp_path / "config"
    new_hooks = xdg_config / "panopticon" / "hooks"
    new_hooks.mkdir(parents=True)
    (new_hooks / "existing").write_text("keep")

    xdg_db = "sqlite:///" + str(tmp_path / "xdg-data" / "panopticon" / "panopticon.db")
    xdg_artifacts = str(tmp_path / "xdg-data" / "panopticon" / "artifacts")
    xdg_layers = str(tmp_path / "xdg-data" / "panopticon" / "layers")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))
    monkeypatch.setattr("panopticon.taskservice.__main__.DB_URL", xdg_db)
    monkeypatch.setattr("panopticon.taskservice.__main__.ARTIFACTS_DIR", xdg_artifacts)
    monkeypatch.setattr("panopticon.taskservice.__main__.LAYERS_DIR", xdg_layers)

    import panopticon.taskservice.__main__ as m
    original = Path.home
    try:
        Path.home = staticmethod(lambda: fake_home)  # type: ignore[method-assign]
        m._migrate_legacy_to_home(xdg_db, xdg_artifacts, xdg_layers)
    finally:
        Path.home = staticmethod(original)  # type: ignore[method-assign]

    assert old_hooks.exists()          # not moved
    assert (new_hooks / "existing").read_text() == "keep"  # not overwritten


# ---------------------------------------------------------------------------
# Session service: CWD-relative → XDG (pre-#251 sources)
# ---------------------------------------------------------------------------

def test_migrate_session_cache_from_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """clone cache in CWD (pre-#251) is migrated to the XDG cache dir."""
    monkeypatch.chdir(tmp_path)
    old_cache = tmp_path / "cache"
    old_cache.mkdir()
    (old_cache / "repo1").mkdir()

    fake_home = tmp_path / "home"
    xdg_cache = tmp_path / "xcache"

    import panopticon.sessionservice._migration as mig
    fake_clone_cache = str(xdg_cache / "panopticon" / "repos")
    fake_tasks = str(xdg_cache / "panopticon" / "tasks")
    monkeypatch.setattr(mig, "CLONE_CACHE_DIR", fake_clone_cache)
    monkeypatch.setattr(mig, "TASKS_DIR", fake_tasks)

    original = Path.home
    try:
        Path.home = staticmethod(lambda: fake_home)  # type: ignore[method-assign]
        mig.migrate_session_dirs(fake_clone_cache, fake_tasks)
    finally:
        Path.home = staticmethod(original)  # type: ignore[method-assign]

    new = Path(fake_clone_cache)
    assert new.is_dir()
    assert (new / "repo1").is_dir()
    assert not old_cache.exists()


def test_migrate_session_cache_from_dot_panopticon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """clone cache in ~/.panopticon/cache (#251) is migrated to the XDG cache dir."""
    monkeypatch.chdir(tmp_path)
    fake_home = tmp_path / "home"
    old_cache = fake_home / ".panopticon" / "cache"
    old_cache.mkdir(parents=True)
    (old_cache / "repo2").mkdir()

    xdg_cache = tmp_path / "xcache"
    import panopticon.sessionservice._migration as mig
    fake_clone_cache = str(xdg_cache / "panopticon" / "repos")
    fake_tasks = str(xdg_cache / "panopticon" / "tasks")
    monkeypatch.setattr(mig, "CLONE_CACHE_DIR", fake_clone_cache)
    monkeypatch.setattr(mig, "TASKS_DIR", fake_tasks)

    original = Path.home
    try:
        Path.home = staticmethod(lambda: fake_home)  # type: ignore[method-assign]
        mig.migrate_session_dirs(fake_clone_cache, fake_tasks)
    finally:
        Path.home = staticmethod(original)  # type: ignore[method-assign]

    new = Path(fake_clone_cache)
    assert new.is_dir()
    assert (new / "repo2").is_dir()
    assert not old_cache.exists()

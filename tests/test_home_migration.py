"""Tests for automatic migration of legacy CWD-relative data to ~/.panopticon/.

The task service and alembic both call these migration helpers on startup so that existing
users' data moves to the new home-dir location transparently on first run after upgrading.
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
    monkeypatch.setattr("panopticon.taskservice.__main__.DEFAULT_DB", fake_url)

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
    monkeypatch.setattr("panopticon.taskservice.__main__.DEFAULT_DB", fake_url)

    from panopticon.taskservice.__main__ import migrate_db_to_home
    migrate_db_to_home(fake_url)

    assert old.exists()           # not moved
    assert new.read_bytes() == b"existing"  # not overwritten


def test_migrate_db_noop_when_old_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    fake_url = _make_fake_db_url(tmp_path)
    monkeypatch.setattr("panopticon.taskservice.__main__.DEFAULT_DB", fake_url)

    from panopticon.taskservice.__main__ import migrate_db_to_home
    migrate_db_to_home(fake_url)  # no error, no file created

    assert not Path(fake_url[len("sqlite:///"):]).exists()


def test_migrate_db_skips_custom_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    old = tmp_path / "panopticon.db"
    old.write_bytes(b"data")

    fake_url = _make_fake_db_url(tmp_path)
    monkeypatch.setattr("panopticon.taskservice.__main__.DEFAULT_DB", fake_url)

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
    monkeypatch.setattr("panopticon.taskservice.__main__.DEFAULT_DB", fake_db)
    monkeypatch.setattr("panopticon.taskservice.__main__.DEFAULT_ARTIFACTS", fake_artifacts)
    monkeypatch.setattr("panopticon.taskservice.__main__.DEFAULT_LAYERS", fake_layers)

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
    monkeypatch.setattr("panopticon.taskservice.__main__.DEFAULT_DB", fake_db)
    monkeypatch.setattr("panopticon.taskservice.__main__.DEFAULT_ARTIFACTS", fake_artifacts)
    monkeypatch.setattr("panopticon.taskservice.__main__.DEFAULT_LAYERS", fake_layers)

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
    monkeypatch.setattr("panopticon.taskservice.__main__.DEFAULT_DB", fake_db)
    monkeypatch.setattr("panopticon.taskservice.__main__.DEFAULT_ARTIFACTS", fake_artifacts)
    monkeypatch.setattr("panopticon.taskservice.__main__.DEFAULT_LAYERS", fake_layers)

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
    monkeypatch.setattr("panopticon.taskservice.__main__.DEFAULT_DB", fake_db)
    monkeypatch.setattr("panopticon.taskservice.__main__.DEFAULT_ARTIFACTS", fake_artifacts)
    monkeypatch.setattr("panopticon.taskservice.__main__.DEFAULT_LAYERS", str(fake_home / ".panopticon" / "layers"))

    from panopticon.taskservice.__main__ import _migrate_legacy_to_home
    _migrate_legacy_to_home(fake_db, "/custom/artifacts", str(fake_home / ".panopticon" / "layers"))

    assert (tmp_path / "artifacts").exists()  # left alone — custom path in use

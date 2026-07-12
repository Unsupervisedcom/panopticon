"""Unit tests for the secrets-dir helpers in :mod:`panopticon.core.dirs`.

``env_file`` is stored as a name relative to the secrets dir so it resolves on whichever host runs
the task (ADR 0007 / remote runners). ``secrets_file_path`` resolves such a name to an absolute
path against a given root (the runner's own); ``relativize_secrets_file`` is the inverse used to
normalize operator input to a stored name.
"""

from __future__ import annotations

import pytest

from panopticon.core.dirs import relativize_secrets_file, secrets_file_path


def test_secrets_file_path_joins_name_onto_root() -> None:
    assert secrets_file_path("r1.env", secrets_dir="/host/secrets") == "/host/secrets/r1.env"


def test_secrets_file_path_none_or_empty_is_none() -> None:
    assert secrets_file_path(None, secrets_dir="/host/secrets") is None
    assert secrets_file_path("", secrets_dir="/host/secrets") is None


@pytest.mark.parametrize("name", ["../evil.env", "/etc/passwd", "a/../../b.env"])
def test_secrets_file_path_rejects_escapes(name: str) -> None:
    with pytest.raises(ValueError):
        secrets_file_path(name, secrets_dir="/host/secrets")


def test_relativize_absolute_under_secrets_dir_becomes_subpath() -> None:
    assert relativize_secrets_file("/host/secrets/r1.env", secrets_dir="/host/secrets") == "r1.env"
    assert (
        relativize_secrets_file("/host/secrets/sub/r1.env", secrets_dir="/host/secrets")
        == "sub/r1.env"
    )


def test_relativize_absolute_elsewhere_becomes_basename() -> None:
    assert relativize_secrets_file("/some/other/r1.env", secrets_dir="/host/secrets") == "r1.env"


def test_relativize_relative_path_is_kept() -> None:
    assert relativize_secrets_file("r1.env", secrets_dir="/host/secrets") == "r1.env"


def test_relativize_blank_is_empty() -> None:
    assert relativize_secrets_file("   ", secrets_dir="/host/secrets") == ""


def test_secrets_file_path_roundtrips_with_relativize() -> None:
    root = "/host/secrets"
    resolved = secrets_file_path("r1.env", secrets_dir=root)
    assert resolved is not None
    assert relativize_secrets_file(resolved, secrets_dir=root) == "r1.env"

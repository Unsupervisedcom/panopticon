"""Unit tests for the config-dir path helpers in :mod:`panopticon.core.dirs`.

``env_file`` is stored as a name relative to the secrets dir so it resolves on whichever host runs
the task (ADR 0007 / remote runners). ``secrets_file_path`` resolves such a name to an absolute
path against a given root (the runner's own); ``relativize_secrets_file`` is the inverse used to
normalize operator input to a stored name. ``hook_file`` follows the same pattern against the hooks
dir (``hook_file_path``; see ``docs/hooks.md``).
"""

from __future__ import annotations

import pytest

from panopticon.core.dirs import (
    hook_file_path,
    relativize_hook_file,
    relativize_layers_file,
    relativize_secrets_file,
    secrets_file_path,
)


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


def test_relativize_layers_file_normalizes_like_secrets() -> None:
    """``image_layer_file`` normalizes to a name relative to the layers dir (ADR 0005), the same
    way ``env_file`` does against the secrets dir."""
    root = "/host/layers"
    # absolute path inside the layers dir → subpath (nested names allowed)
    assert relativize_layers_file("/host/layers/r1.dockerfile", layers_dir=root) == "r1.dockerfile"
    assert (
        relativize_layers_file("/host/layers/team/base.dockerfile", layers_dir=root)
        == "team/base.dockerfile"
    )
    # absolute path elsewhere → basename
    assert relativize_layers_file("/other/r1.dockerfile", layers_dir=root) == "r1.dockerfile"
    # relative path kept; blank → empty
    assert relativize_layers_file("r1.dockerfile", layers_dir=root) == "r1.dockerfile"
    assert relativize_layers_file("   ", layers_dir=root) == ""


def test_hook_file_path_joins_name_onto_root() -> None:
    assert hook_file_path("prep.sh", hooks_dir="/host/hooks") == "/host/hooks/prep.sh"


def test_hook_file_path_none_or_empty_is_none() -> None:
    assert hook_file_path(None, hooks_dir="/host/hooks") is None
    assert hook_file_path("", hooks_dir="/host/hooks") is None


@pytest.mark.parametrize("name", ["../evil.sh", "/etc/cron.d/evil", "a/../../b.sh"])
def test_hook_file_path_rejects_escapes(name: str) -> None:
    with pytest.raises(ValueError):
        hook_file_path(name, hooks_dir="/host/hooks")


def test_relativize_hook_absolute_under_hooks_dir_becomes_subpath() -> None:
    assert relativize_hook_file("/host/hooks/prep.sh", hooks_dir="/host/hooks") == "prep.sh"
    assert relativize_hook_file("/host/hooks/sub/prep.sh", hooks_dir="/host/hooks") == "sub/prep.sh"


def test_relativize_hook_absolute_elsewhere_becomes_basename() -> None:
    assert relativize_hook_file("/some/other/prep.sh", hooks_dir="/host/hooks") == "prep.sh"


def test_relativize_hook_relative_path_is_kept() -> None:
    assert relativize_hook_file("prep.sh", hooks_dir="/host/hooks") == "prep.sh"


def test_relativize_hook_blank_is_empty() -> None:
    assert relativize_hook_file("   ", hooks_dir="/host/hooks") == ""


def test_hook_file_path_roundtrips_with_relativize() -> None:
    root = "/host/hooks"
    resolved = hook_file_path("prep.sh", hooks_dir=root)
    assert resolved is not None
    assert relativize_hook_file(resolved, hooks_dir=root) == "prep.sh"

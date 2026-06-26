"""Filesystem layer store: read layer files by name, rooted + escape-guarded."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from panopticon.core.layers import InvalidLayerName
from panopticon.taskservice.layers_fs import FilesystemLayerStore


def test_get_reads_a_file_under_the_root(tmp_path: Path) -> None:
    (tmp_path / "r1.layer").write_text("RUN pip install uv")
    assert asyncio.run(FilesystemLayerStore(tmp_path).get("r1.layer")) == b"RUN pip install uv"


def test_get_reads_a_nested_name(tmp_path: Path) -> None:
    (tmp_path / "acme").mkdir()
    (tmp_path / "acme" / "tools.layer").write_text("RUN make")
    assert asyncio.run(FilesystemLayerStore(tmp_path).get("acme/tools.layer")) == b"RUN make"


def test_get_missing_file_returns_none(tmp_path: Path) -> None:
    assert asyncio.run(FilesystemLayerStore(tmp_path).get("absent.layer")) is None


@pytest.mark.parametrize("name", ["../escape.layer", "/etc/passwd", "a/../../escape"])
def test_get_rejects_names_escaping_the_root(tmp_path: Path, name: str) -> None:
    with pytest.raises(InvalidLayerName):
        asyncio.run(FilesystemLayerStore(tmp_path).get(name))

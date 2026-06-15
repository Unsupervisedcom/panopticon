"""The runner (execution-backend) interface: it's abstract and the stub conforms."""

from __future__ import annotations

import pytest

from panopticon.sessionservice.runner import Runner
from panopticon.sessionservice.stub_runner import StubRunner


def test_runner_is_abstract() -> None:
    with pytest.raises(TypeError):
        Runner()  # type: ignore[abstract]


def test_stub_runner_conforms_to_runner() -> None:
    assert issubclass(StubRunner, Runner)

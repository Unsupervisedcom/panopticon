"""Bootstrap: ensure the base image exists before starting the stack.

The key operation is :meth:`Bootstrap.ensure_image` — it runs ``docker image inspect``
to probe for the image and only triggers a build when it's absent, streaming the build
output so there's no silent multi-minute gap. An injectable :class:`CommandRunner` makes
the decision logic unit-testable without a real Docker daemon.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from typing import Protocol


class CommandRunner(Protocol):
    """Runs an external command.

    When ``check=True`` (the default) and the process exits non-zero, raises
    :class:`subprocess.CalledProcessError`. When ``capture_output=False`` (the default),
    stdout and stderr flow to the caller's terminal — used for streaming ``docker build``
    progress so there's no silent pause.
    """

    def __call__(
        self,
        args: Sequence[str],
        *,
        check: bool = True,
        capture_output: bool = False,
    ) -> subprocess.CompletedProcess[str]: ...


def _subprocess_run(
    args: Sequence[str],
    *,
    check: bool = True,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), check=check, capture_output=capture_output, text=True)


class Bootstrap:
    """Idempotent startup helper: build the base image only when it is absent."""

    def __init__(self, run: CommandRunner = _subprocess_run) -> None:
        self._run = run

    def image_exists(self, image: str) -> bool:
        """Return True when ``image`` is present in the local Docker image store."""
        result = self._run(
            ["docker", "image", "inspect", image],
            check=False,
            capture_output=True,
        )
        return result.returncode == 0

    def build_image(self, image: str, *, dockerfile: str = "docker/Dockerfile") -> None:
        """Build ``image`` from ``dockerfile``, streaming progress to the terminal."""
        self._run(
            ["docker", "build", "--tag", image, "--file", dockerfile, "."],
            check=True,
            capture_output=False,  # stream Docker's build output — never a silent pause
        )

    def ensure_image(self, image: str) -> bool:
        """Build only when the image is absent. Returns True when a build was triggered."""
        if self.image_exists(image):
            return False
        self.build_image(image)
        return True

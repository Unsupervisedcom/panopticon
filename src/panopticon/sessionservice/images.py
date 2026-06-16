"""Composable task images (ADR 0005): a task's image = **base → workflow → repo** layers.

The base is minimal and general (the agent runtime); a workflow contributes a layer with what
its skills need (e.g. `gh`); a repo contributes its toolchain/setup. We compose them by writing
a Dockerfile that `FROM`s the base and appends the layers, tag it `panopticon-<workflow>-<repo>`,
and `docker build` it behind the injectable command-runner (so it's unit-testable without a
daemon). LLM-free. The runner builds the composed image, then spawns the task on it.
"""

from __future__ import annotations

import tempfile
from collections.abc import Sequence
from pathlib import Path

from panopticon.sessionservice.local_runner import DEFAULT_IMAGE, CommandRunner, _subprocess_run


def image_tag(workflow: str, repo_id: str) -> str:
    """The composed image's tag for a (workflow, repo) pair (ADR 0005 naming)."""
    return f"panopticon-{workflow}-{repo_id}"


def compose_dockerfile(base: str, layers: Sequence[str]) -> str:
    """A Dockerfile that starts from ``base`` and appends each non-empty layer fragment."""
    body = "\n\n".join(layer.strip() for layer in layers if layer.strip())
    return f"FROM {base}\n" + (f"\n{body}\n" if body else "")


class ImageBuilder:
    """Builds composed task images on the local Docker daemon (one host)."""

    def __init__(self, *, base: str = DEFAULT_IMAGE, run: CommandRunner = _subprocess_run) -> None:
        self._base = base
        self._run = run

    def build(self, workflow: str, repo_id: str, layers: Sequence[str]) -> str:
        """Compose base → ``layers`` and `docker build` it; return the image tag."""
        tag = image_tag(workflow, repo_id)
        dockerfile = compose_dockerfile(self._base, layers)
        with tempfile.TemporaryDirectory() as context:
            (Path(context) / "Dockerfile").write_text(dockerfile)
            self._run(["docker", "build", "--tag", tag, context])
        return tag

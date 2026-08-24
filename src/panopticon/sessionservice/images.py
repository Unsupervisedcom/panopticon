"""Composable task images (ADR 0005): a task's image = **base → workflow → repo** layers.

The base is minimal and general (the agent runtime); a workflow contributes a layer with what
its skills need (e.g. `gh`); a repo contributes its toolchain/setup. We compose them by writing
a Dockerfile that `FROM`s the base and appends the layers, tag it `panopticon-<workflow>-<repo>`,
and `docker build` it behind the injectable command-runner (so it's unit-testable without a
daemon). LLM-free. The runner builds the composed image, then spawns the task on it.
"""

from __future__ import annotations

import importlib.resources
import logging
import tempfile
from collections.abc import Sequence
from pathlib import Path

from panopticon.core.models import DEFAULT_AGENT_CLI
from panopticon.sessionservice.local_runner import (
    DEFAULT_IMAGE,
    CommandRunner,
    _subprocess_run,
    base_image,
)

_log = logging.getLogger(__name__)


def image_tag(workflow: str, repo_id: str, agent_cli: str = DEFAULT_AGENT_CLI) -> str:
    """The composed image's tag for a (workflow, repo, CLI) triple (ADR 0005 naming + ADR 0014 §4).

    The CLI dimension keeps a repo built for two CLIs from colliding on one tag."""
    return f"panopticon-{agent_cli}-{workflow}-{repo_id}"


def compose_dockerfile(base: str, layers: Sequence[str]) -> str:
    """A Dockerfile that starts from ``base`` and appends each non-empty layer fragment."""
    body = "\n\n".join(layer.strip() for layer in layers if layer.strip())
    return f"FROM {base}\n" + (f"\n{body}\n" if body else "")


class ImageBuilder:
    """Builds composed task images on the local Docker daemon (one host)."""

    def __init__(self, *, base: str = DEFAULT_IMAGE, run: CommandRunner = _subprocess_run) -> None:
        self._base = base
        self._run = run

    def build(
        self,
        workflow: str,
        repo_id: str,
        layers: Sequence[str],
        *,
        agent_cli: str = DEFAULT_AGENT_CLI,
        verbose: bool = False,
    ) -> str:
        """Compose the CLI's base → ``layers`` and `docker build` it; return the image tag.

        ``agent_cli`` selects the per-CLI base variant (:func:`base_image`, ADR 0014 §4) the layers
        compose onto, and is part of the tag so the same repo built for two CLIs doesn't collide.
        ``verbose`` streams docker build output to the caller's stdout/stderr (visible in the
        runner's tmux session) instead of capturing it."""
        tag = image_tag(workflow, repo_id, agent_cli)
        dockerfile = compose_dockerfile(base_image(agent_cli), layers)
        with tempfile.TemporaryDirectory() as context:
            (Path(context) / "Dockerfile").write_text(dockerfile)
            self._run(["docker", "build", "--tag", tag, context], verbose=verbose)
        return tag

    def _build_base(self, base: str, cli: str, *, verbose: bool) -> None:
        """`docker build` the bundled base Dockerfile as ``base`` for ``cli`` (ADR 0014 §4).

        The ``AGENT_CLI`` build arg selects which agent CLI the bundled Dockerfile installs, so the
        same file yields a genuinely different image per CLI (``panopticon-base-<cli>``)."""
        import panopticon
        import panopticon.docker as _docker_pkg

        dockerfile_ref = importlib.resources.files(_docker_pkg) / "Dockerfile"
        with importlib.resources.as_file(dockerfile_ref) as dockerfile_path:
            self._run(
                [
                    "docker",
                    "build",
                    "--tag",
                    base,
                    "--build-arg",
                    f"PANOPTICON_VERSION={panopticon.__version__}",
                    "--build-arg",
                    f"AGENT_CLI={cli}",
                    "--file",
                    str(dockerfile_path),
                    str(dockerfile_path.parent),
                ],
                verbose=verbose,
            )

    def build_base(self, *, agent_cli: str | None = None, verbose: bool = False) -> None:
        """Build the base image unconditionally from the bundled Dockerfile.

        ``agent_cli`` selects the per-CLI base variant to tag + install (:func:`base_image`, ADR
        0014 §4); ``None`` builds this builder's configured base (the claude default)."""
        base = base_image(agent_cli) if agent_cli else self._base
        self._build_base(base, agent_cli or DEFAULT_AGENT_CLI, verbose=verbose)

    def build_base_if_missing(self, *, agent_cli: str | None = None, verbose: bool = False) -> bool:
        """Probe for the base image; build it from the bundled Dockerfile if absent.

        ``agent_cli`` selects the per-CLI base variant to probe + build (:func:`base_image`, ADR
        0014 §4); ``None`` uses this builder's configured base (the claude default). Uses
        ``docker image inspect`` (fast, ~100 ms) to check presence. If the image is missing builds
        it using the Dockerfile bundled with the installed package (``panopticon.docker``). Returns
        ``True`` if a build was triggered, ``False`` if the image was already present."""
        base = base_image(agent_cli) if agent_cli else self._base
        result = self._run(["docker", "image", "inspect", base], check=False)
        if result.strip() in ("", "[]"):
            _log.warning("base image %r not found — building automatically", base)
            self._build_base(base, agent_cli or DEFAULT_AGENT_CLI, verbose=verbose)
            return True
        return False

"""The ``kubectl`` command seam shared by the Kubernetes backend (ADR 0014).

Everything panopticon does against a cluster goes through ``kubectl``: it is already the interactive
surface (``kubectl exec -it … tmux attach``), it carries the operator's kubeconfig and context
selection for free, and it keeps panopticon dependency-free of a Kubernetes client library. The
executor is a **Protocol** so tests assert on the argv and the piped manifest instead of talking to a
cluster. LLM-free.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from typing import Protocol


class CommandRunner(Protocol):
    """Runs a ``kubectl`` command and returns its stdout; ``check`` raises on a non-zero exit.

    ``stdin`` feeds a manifest to ``kubectl apply --filename -``. This is why the Kubernetes backend
    does not reuse :class:`~panopticon.sessionservice.local_runner.CommandRunner`, which has no
    stdin (Docker never needs one)."""

    def __call__(
        self, args: Sequence[str], *, check: bool = True, stdin: str | None = None
    ) -> str: ...


def subprocess_run(args: Sequence[str], *, check: bool = True, stdin: str | None = None) -> str:
    """The production :class:`CommandRunner`."""
    return subprocess.run(
        list(args), check=check, input=stdin, capture_output=True, text=True
    ).stdout


def kubectl_argv(
    kubectl: Sequence[str], context: str | None, *args: str, namespace: str | None = None
) -> list[str]:
    """Build a ``kubectl`` argv with the configured base command, ``--context``, and ``--namespace``.

    One builder so every call agrees on flag order and no call site forgets the context — a command
    that silently runs against the wrong cluster is the failure mode worth designing out.
    """
    argv = list(kubectl)
    if context:
        argv += ["--context", context]
    if namespace:
        argv += ["--namespace", namespace]
    return [*argv, *args]

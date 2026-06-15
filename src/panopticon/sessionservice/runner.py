"""The execution-backend interface (ADR 0006/0008): spawn and stop task containers.

A *runner* turns a task into a running task container that connects back to the task service
(liveness) and a host tmux session the terminal controller can attach to. The container runs
the agent and decides its own slug; the runner only manages the container/tmux lifecycle and
injects the repo's secrets — it stays **LLM-free** (the determinism invariant).

Concrete adapters implement this behind the same contract:
* :class:`~panopticon.sessionservice.stub_runner.StubRunner` — runs the entrypoint in-process,
  no Docker (the walking skeleton);
* the local Docker+tmux runner (Slice 2) — a real host process.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class Runner(ABC):
    """Spawns task containers and owns their tmux sessions (ADR 0008)."""

    @abstractmethod
    def spawn(self, task_id: str) -> str:
        """Start a container working on ``task_id``; return its container id.

        The container self-registers with the task service for liveness and chooses its slug
        in-container — the runner passes neither work nor slug in.
        """

    @abstractmethod
    def stop(self, container_id: str) -> None:
        """Stop the container and tear down its tmux session. Idempotent."""

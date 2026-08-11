"""A spike that runs on this machine with no container — the ``runner_type = "host"`` demo.

Point the task service at this directory (``--workflows-path dev/workflows``) and the workflow
registers itself, exactly as dropping a module in ``~/.config/panopticon/workflows/`` would. It is
:class:`~panopticon.workflows.spike.Spike`'s lifecycle with the execution backend swapped: the same
single ungated state, but the agent runs in a host tmux pane rather than a container — so the task
starts without building an image and works with whatever toolchain the operator's shell provides
(a repo's ``devenv``/``direnv``, the system PATH).

The trade is isolation: the agent runs **as the operator**, with the operator's filesystem,
credentials and network. Use it for repos whose toolchain is awkward to containerize, and for
proving the loop end to end without a working container runtime; use ``spike`` when you want the
container boundary back.
"""

from __future__ import annotations

from typing import ClassVar

from panopticon.core.state import Complete, InitialState
from panopticon.core.workflow import Workflow


class HostSpike(Workflow):
    """ITERATING → {COMPLETE, DROPPED}, executed directly on the host (no container)."""

    name: ClassVar[str] = "host-spike"
    when_to_use: ClassVar[str] = (
        "Open-ended agent work that runs directly on this machine with the operator's own "
        "toolchain and no container — and, deliberately, no isolation."
    )
    runner_type: ClassVar[str] = "host"

    class Iterating(InitialState):
        label = "ITERATING"
        description = "Open-ended agent work until the user marks the task complete."
        transitions = (Complete,)  # + DROPPED inherited from State

    initial = Iterating

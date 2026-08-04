"""A spike that runs in a cluster instead of on this machine — the ADR 0014 demo workflow.

Point the task service at this directory (``--workflows-path dev/workflows``) and the workflow
registers itself, exactly as dropping a module in ``~/.config/panopticon/workflows/`` would. It is
:class:`~panopticon.workflows.spike.Spike`'s lifecycle with the execution backend swapped: the same
single ungated state, but each task becomes a Job in the ``panopticon`` agent's namespace (``dev/k8s-agent.yaml``), under
that agent's service account, quota, and credentials.

Set ``PANOPTICON_OPERATOR_AGENT`` to run it as a different agent-operator ``Agent``.
"""

from __future__ import annotations

import os
from typing import ClassVar

from panopticon.core.state import Complete, InitialState
from panopticon.core.workflow import Workflow


class KubernetesSpike(Workflow):
    """ITERATING → {COMPLETE, DROPPED}, executed as an agent-operator Agent's Job."""

    name: ClassVar[str] = "k8s-spike"
    when_to_use: ClassVar[str] = (
        "Open-ended agent work that runs in the cluster as an agent-operator Agent, "
        "with that agent's identity, credentials and resource quota."
    )
    runner_type: ClassVar[str] = "kubernetes"
    operator_agent: ClassVar[str | None] = os.environ.get("PANOPTICON_OPERATOR_AGENT", "panopticon")

    class Iterating(InitialState):
        label = "ITERATING"
        description = "Open-ended agent work until the user marks the task complete."
        transitions = (Complete,)  # + DROPPED inherited from State

    initial = Iterating

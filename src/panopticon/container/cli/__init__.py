"""The **agent-CLI adapter package** (ADR 0014): the :class:`AgentCLI` seam + registry, and one
module per CLI.

Panopticon drives one agent CLI today (`claude`); Milestone 3 adds others (Codex first). The
launcher (:mod:`panopticon.container.agent`) is CLI-agnostic — it drives a deterministic *bootstrap*
(render skills + turn-flip hooks, wire MCP, seed trust) then a *launch* (exec the real CLI) against
an :class:`AgentCLI` adapter, holding no ``claude`` literal. :mod:`~panopticon.container.cli.base`
holds the ABC + the name-keyed registry; each concrete adapter is its own module
(:mod:`~panopticon.container.cli.claude` and :mod:`~panopticon.container.cli.codex`). A second CLI drops in by
implementing the ABC and registering under its name (the drop-in shape of workflow discovery, ADR 0004).

The package lives **inside** ``container/`` — the sole LLM-bearing package — so the determinism
invariant holds (ADR 0014 §6): the control plane runs no CLI-specific logic.
"""

from panopticon.container.cli.base import (
    DEFAULT_AGENT_CLI,
    AgentCLI,
    get_agent_cli,
    register_agent_cli,
)

__all__ = ["DEFAULT_AGENT_CLI", "AgentCLI", "get_agent_cli", "register_agent_cli"]

"""The **agent-CLI adapter package** (ADR 0014). See :mod:`~panopticon.container.cli.base` for the
:class:`AgentCLI` seam + registry; concrete adapters are :mod:`~panopticon.container.cli.claude` and
:mod:`~panopticon.container.cli.codex`.
"""

from panopticon.container.cli.base import (
    DEFAULT_AGENT_CLI,
    AgentCLI,
    get_agent_cli,
    register_agent_cli,
    registered_agent_clis,
)

__all__ = [
    "DEFAULT_AGENT_CLI",
    "AgentCLI",
    "get_agent_cli",
    "register_agent_cli",
    "registered_agent_clis",
]

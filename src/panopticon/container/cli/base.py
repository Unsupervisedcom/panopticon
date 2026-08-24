"""The **agent-CLI adapter seam** (ADR 0014): the contract, one method per claude-specific decision.

This module holds only the :class:`AgentCLI` ABC + the registry that maps a CLI name to its adapter;
each concrete adapter is a sibling module (:mod:`panopticon.container.cli.claude` and
:mod:`panopticon.container.cli.codex`). The launcher (:mod:`panopticon.container.agent`) is CLI-agnostic — it drives a deterministic
*bootstrap* (render skills + turn-flip hooks, wire MCP, seed trust) then a *launch* (exec the real
CLI) against an adapter, holding no ``claude`` literal. A second CLI drops in by implementing the ABC
and registering under its name (the same shape as workflow discovery, ADR 0004).

The bootstrap/launch split (AGENTS.md "No LLMs in tests") is preserved: every rendering method is
deterministic and unit-tested with fakes; only :meth:`AgentCLI.launch` execs the real CLI and is
injected in tests. The package lives **only** inside ``container/`` — the sole LLM-bearing package —
so the determinism invariant holds (ADR 0014 §6): the control plane runs no CLI-specific logic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar, Protocol, TextIO


class _Client(Protocol):
    """The slice of the task-service client the bootstrap needs (kept structural so tests fake it)."""

    def list_skills(self, task_id: str) -> list[dict[str, str]]: ...
    def list_operations(self, task_id: str) -> dict[str, str]: ...


class AgentCLI(ABC):
    """One adapter per agent CLI: the seam that captures its every CLI-specific decision (ADR 0014).

    Subclasses set :attr:`name` (the registry key) and :attr:`config_dirname` (the config dir under
    the container home, e.g. ``.claude``) and implement each seam. The launcher resolves an adapter
    by name and calls: the ``render_*``/``write_*``/``trust_workspace`` bootstrap methods, then
    :meth:`launch`.
    """

    #: Registry key — the CLI name the runner passes in (``PANOPTICON_AGENT_CLI``).
    name: ClassVar[str]
    #: The CLI's config dir, relative to the container home (the launcher mounts it per-task).
    config_dirname: ClassVar[str]

    @abstractmethod
    def render_skills(self, client: _Client, task_id: str, home: Path) -> list[Path]:
        """Render the active workflow's skills to the CLI's command surface. Returns the paths."""

    @abstractmethod
    def render_operations(self, client: _Client, task_id: str, home: Path) -> list[Path]:
        """Render the workflow's core operations (advance/drop/…) as CLI commands. Returns paths."""

    @abstractmethod
    def write_settings(self, home: Path) -> Path:
        """Wire the turn-flip hooks (Stop/UserPromptSubmit/…) into the CLI's settings. Returns path."""

    @abstractmethod
    def write_mcp_config(self, config_dir: Path, service_url: str) -> Path:
        """Point the CLI at the task service's MCP server (``<service_url>/mcp``). Returns the path."""

    @abstractmethod
    def write_workflow_overview(self, config_dir: Path, overview: str) -> Path | None:
        """Deliver the whole-workflow map into the agent's context (system prompt). ``None`` if empty."""

    @abstractmethod
    def trust_workspace(self, config_dir: Path, cwd: Path) -> Path:
        """Pre-accept the CLI's first-run/trust dialogs for ``cwd`` (no operator in the container)."""

    @abstractmethod
    def auth_missing_detail(self, env: Mapping[str, str]) -> str | None:
        """The failure detail if the CLI's auth env var is absent, else ``None`` (auth is present)."""

    @abstractmethod
    def resolve_model(self, tier: str) -> str:
        """Map the control plane's abstract model **tier** to this CLI's concrete model id (§3a)."""

    @abstractmethod
    def read_hook_payload(self, stdin: TextIO) -> dict[str, Any]:
        """Tolerantly parse the turn-flip hook's stdin payload (empty/invalid → ``{}``)."""

    @abstractmethod
    def has_live_background_task(self, payload: dict[str, Any]) -> bool:
        """Whether the hook payload reports still-running background work (gates the turn flip)."""

    @abstractmethod
    def launch(self, config_dir: Path) -> None:
        """Exec the real CLI in the foreground; return when it exits."""


#: The adapter registry, keyed by CLI name. Adding a CLI is: implement :class:`AgentCLI`, register
#: it here — no launcher or control-plane edit (ADR 0014 §2).
_REGISTRY: dict[str, type[AgentCLI]] = {}

#: The CLI the launcher assumes when the runner passes none, so existing containers are unchanged.
DEFAULT_AGENT_CLI = "claude"


def register_agent_cli(cls: type[AgentCLI]) -> type[AgentCLI]:
    """Register an :class:`AgentCLI` subclass under its :attr:`~AgentCLI.name` (usable as a decorator)."""
    _REGISTRY[cls.name] = cls
    return cls


def get_agent_cli(name: str | None = None) -> AgentCLI:
    """Resolve the adapter for ``name`` (defaulting to :data:`DEFAULT_AGENT_CLI` when unset)."""
    _load_builtin_adapters()
    key = name or DEFAULT_AGENT_CLI
    try:
        return _REGISTRY[key]()
    except KeyError:
        raise KeyError(f"unknown agent CLI {key!r}; registered: {sorted(_REGISTRY)}") from None


def _load_builtin_adapters() -> None:
    """Register the built-in adapters (imported lazily so this module holds only the contract)."""
    from panopticon.container.cli.claude import ClaudeAgentCLI
    from panopticon.container.cli.codex import CodexAgentCLI

    register_agent_cli(ClaudeAgentCLI)
    register_agent_cli(CodexAgentCLI)

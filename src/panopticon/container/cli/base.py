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

import json
from abc import ABC, abstractmethod
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar, Protocol, TextIO

from panopticon.core.features import (
    CODEX_AGENT_CLI,
    agent_cli_unavailable_detail,
    codex_enabled,
)
from panopticon.core.models import MODEL_TIERS


def resolve_tier(tier: str, tiers: Mapping[str, str]) -> str:
    """Resolve an abstract model **tier** to a concrete model id, failing loud on an unresolved tier.

    ``tiers`` is the CLI adapter's tier→model map. Resolution has three cases (ADR 0014 §3a):

    - ``tier`` is in ``tiers`` → return its concrete model id (the normal path).
    - ``tier`` is a **reserved** tier name (:data:`~panopticon.core.models.MODEL_TIERS`) but absent
      from ``tiers`` → raise. An unresolved tier historically leaked straight to ``--model`` (a stale
      container running pre-resolution code did exactly this — the bug this guards); refuse it
      loudly instead of launching the wrong model silently.
    - anything else → a concrete model id set directly (or a tier already persisted as its resolved
      name); pass it through unchanged so back-compat holds.
    """
    if tier in tiers:
        return tiers[tier]
    if tier in MODEL_TIERS:
        raise ValueError(
            f"model tier {tier!r} is not mapped by this CLI adapter (known tiers: {sorted(tiers)}); "
            "refusing to pass an unresolved tier through to --model. This usually means a stale "
            "container image running pre-resolution code — rebuild with `make clean && make build`."
        )
    return tier


#: The quote characters an operator might wrap an env-file value in (dotenv habit).
_QUOTES = ('"', "'")


def unquote_secret(value: str) -> str:
    """An env-file value as the operator *meant* it: whitespace- and quote-stripped.

    The two runners disagree about dotenv quoting, on the very same file. :class:`ShellRunner`
    **sources** the repo's ``env_file`` (``set -a; . <file>; set +a``), so the shell strips quoting
    for it. :class:`LocalRunner` hands the file to ``docker run --env-file``, which does **no**
    dotenv parsing at all: everything after the first ``=`` becomes the value, quotes included, with
    no trimming — so ``KEY="v"`` reaches the container as the five characters ``"v"``, and a
    CRLF-terminated file leaves a trailing ``\\r``. Neither claude nor codex strips either, so the
    credential is silently malformed and every API call fails with an opaque 401.

    This is the one place that reconciles them: strip surrounding whitespace, remove **one** matching
    leading/trailing quote pair, strip again. Deliberately conservative —

    - both ends must carry the *same* quote character, so an unbalanced ``"v`` (or a value that
      merely contains quotes) is left alone: an unbalanced quote isn't a recognizable mistake;
    - only one pair is removed, so ``'"v"'`` yields ``"v"`` rather than guessing at intent.

    No real credential begins and ends with the same quote character, so this cannot corrupt a
    valid one.
    """
    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in _QUOTES:
        stripped = stripped[1:-1].strip()
    return stripped


def secret_from_env(env: Mapping[str, str], name: str) -> str | None:
    """``env[name]`` normalized by :func:`unquote_secret`, or ``None`` when unset or empty.

    The empty case matters: ``KEY=""`` in the env-file reaches the container as the two-character
    string ``""``, which is *truthy* — so a presence check on the raw value reads a deliberately
    blank credential as present-but-broken. Normalizing first reads it as absent, which is what the
    operator wrote.
    """
    return unquote_secret(env.get(name) or "") or None


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
    #: The control plane's abstract model **tiers** mapped to this CLI's concrete model ids (ADR
    #: 0014 §3a). Subclasses set the mapping; :meth:`resolve_model` reads it. Unknown tiers pass
    #: through unchanged so a raw model id set directly still reaches ``--model`` verbatim.
    MODEL_TIERS: ClassVar[Mapping[str, str]]
    #: Credential env vars (injected from the repo's ``env_file``) whose values are normalized by
    #: :func:`unquote_secret` before the CLI — and everything it shells out to — sees them.
    #: Subclasses list their own; the default is empty, so an adapter opts in explicitly.
    SECRET_ENV_VARS: ClassVar[tuple[str, ...]] = ()

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
    def trust_workspace(self, config_dir: Path, cwd: Path, env: Mapping[str, str]) -> Path:
        """Pre-accept the CLI's first-run/trust dialogs for ``cwd`` (no operator in the container).

        Takes ``env`` because not every first-run gate is keyed to the workspace: claude gates a
        ``ANTHROPIC_API_KEY`` injected from the repo's ``env_file`` behind an approval dialog keyed
        to the key's own *value*, so pre-accepting it needs the credential, not just the path.
        """

    @abstractmethod
    def auth_missing_detail(self, env: Mapping[str, str], config_dir: Path) -> str | None:
        """The failure detail if the CLI can't authenticate, else ``None``.

        Auth is present when the CLI's env var is set **or** a persisted credential already sits on
        the per-task config volume (``config_dir``) — so a container carried across respawn isn't
        wrongly failed. Presence check only; validity surfaces at the CLI's first call.
        """

    @abstractmethod
    def write_credentials(self, config_dir: Path, env: Mapping[str, str]) -> Path | None:
        """Materialize any on-disk credentials the CLI needs from the env (idempotent).

        Returns the written path, or ``None`` when the CLI reads its credentials straight from the
        env (claude) or a credential file is already present. Never clobbers an existing one.
        """

    def launch_env(self, env: Mapping[str, str]) -> dict[str, str]:
        """The overlay of normalized credential values to launch the CLI with (changed keys only).

        Each of this adapter's :attr:`SECRET_ENV_VARS` that is set and not already normalized maps
        to its :func:`unquote_secret` form; everything else is omitted, so merging this over the
        process env is a no-op for a correctly written env-file. :meth:`launch` merges it, which also
        covers every tool the CLI shells out to (``gh`` in the forge skills, say) — they inherit the
        corrected env from their parent.

        Concrete and non-abstract: the default :attr:`SECRET_ENV_VARS` is empty, so an adapter that
        needs nothing normalized inherits correct behaviour, and this stays unit-testable without
        widening the abstract surface.
        """
        overlay: dict[str, str] = {}
        for var in self.SECRET_ENV_VARS:
            raw = env.get(var)
            if raw is None:
                continue
            normalized = unquote_secret(raw)
            if normalized != raw:
                overlay[var] = normalized
        return overlay

    def resolve_model(self, tier: str) -> str:
        """Map the control plane's abstract model **tier** to this CLI's concrete model id (§3a).

        The control plane stores a CLI-agnostic tier (e.g. ``"primary"``); adapters declare their
        :attr:`MODEL_TIERS` mapping and this is the only place a tier becomes a provider model name,
        keeping model vocabulary out of ``core``/``workflows``. Reserved tiers that are absent from
        the mapping raise, so a stale image running pre-resolution code fails loud rather than leaking
        the raw tier to ``--model`` (see :func:`resolve_tier`).
        """
        return resolve_tier(tier, self.MODEL_TIERS)

    def read_hook_payload(self, stdin: TextIO) -> dict[str, Any]:
        """Tolerantly parse the hook's stdin JSON; empty/invalid input yields an empty payload."""
        try:
            raw = stdin.read()
        except (OSError, ValueError):
            return {}
        if not raw or not raw.strip():
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

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
    """Resolve the adapter for ``name`` (defaulting to :data:`DEFAULT_AGENT_CLI` when unset).

    A name that isn't registered raises :class:`KeyError`; when it names a CLI that exists but is
    **feature-flagged off** (codex, ADR 0014 §7) the error says so and names the flag, since
    "unknown agent CLI 'codex'" alone would send the operator hunting for a typo.

    The flag is checked **here**, not only at registration: the registry is process-global, so a
    codex adapter registered while the flag was on (a long-lived process, a test) must not keep
    resolving after it goes off. Only codex is gated — a third-party adapter registered via
    :func:`register_agent_cli` resolves as before (ADR 0014 §2).
    """
    _load_builtin_adapters()
    key = name or DEFAULT_AGENT_CLI
    if key == CODEX_AGENT_CLI and not codex_enabled():
        raise KeyError(agent_cli_unavailable_detail(key))
    try:
        return _REGISTRY[key]()
    except KeyError:
        detail = agent_cli_unavailable_detail(key) or f"unknown agent CLI {key!r}"
        raise KeyError(f"{detail}; registered: {sorted(_REGISTRY)}") from None


def _load_builtin_adapters() -> None:
    """Register the built-in adapters (imported lazily so this module holds only the contract).

    Codex is registered **only when its feature flag is on** (``PANOPTICON_ENABLE_CODEX``, ADR 0014
    §8) — the runner carries the host's flag into the container, so the registry here agrees with
    what the control plane will let a task select."""
    from panopticon.container.cli.claude import ClaudeAgentCLI

    register_agent_cli(ClaudeAgentCLI)
    if codex_enabled():
        from panopticon.container.cli.codex import CodexAgentCLI

        register_agent_cli(CodexAgentCLI)

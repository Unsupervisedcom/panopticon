"""Feature flags: the host-side switches that gate work not yet ready to be on for everyone.

One flag lives here today — :data:`CODEX_FLAG` (``PANOPTICON_ENABLE_CODEX``), which gates the
**codex** agent CLI (ADR 0014 §7). Codex is the newest adapter and unproven next to claude, so it
ships **off**: with the flag unset, codex isn't selectable on a repo or task, isn't spawnable, isn't
built by ``make build``, and isn't registered in the container's adapter registry. The flag is a
*gate*, not a revert — every line of codex support is still here, and setting
``PANOPTICON_ENABLE_CODEX=1`` on the control plane restores today's behaviour exactly.

The flag is read from the **process env**, and every reader here takes ``env`` as an argument
(defaulting to :data:`os.environ`) so the control plane, the runner, the dashboard and the container
all answer the same question from their own environment — and so tests never depend on the
developer's shell. LLM-free and clock-free: this is policy, not I/O.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from panopticon.core.models import DEFAULT_AGENT_CLI

#: The env var gating codex support (ADR 0014 §7). Unset (the default) means codex is off.
CODEX_FLAG = "PANOPTICON_ENABLE_CODEX"

#: The agent CLI gated by :data:`CODEX_FLAG` — named once so the gate and its error messages agree.
CODEX_AGENT_CLI = "codex"

#: The env values read as "on", lowercased. Anything else (including unset, ``""``, ``0``, ``no``)
#: is off, so the flag fails **closed** — a typo disables codex rather than silently enabling it.
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def flag_enabled(value: str | None) -> bool:
    """Whether a raw env value reads as "on" (:data:`_TRUTHY`, case- and whitespace-insensitive)."""
    return (value or "").strip().lower() in _TRUTHY


def codex_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Whether codex support is enabled in ``env`` (default :data:`os.environ`). Off by default."""
    return flag_enabled((env if env is not None else os.environ).get(CODEX_FLAG))


def available_agent_clis(env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """The agent CLIs selectable in ``env``: claude always, codex only behind :data:`CODEX_FLAG`.

    The single source of truth for "which CLIs may be chosen" — the task service validates against
    it, the dashboard decides whether to offer a choice at all, and the container's registry
    registers against it.
    """
    if codex_enabled(env):
        return (DEFAULT_AGENT_CLI, CODEX_AGENT_CLI)
    return (DEFAULT_AGENT_CLI,)


def agent_cli_unavailable_detail(
    agent_cli: str | None, env: Mapping[str, str] | None = None
) -> str | None:
    """Why ``agent_cli`` can't be used in ``env``, or ``None`` when it can.

    ``None`` (no CLI named — "use the default") is always available. A disabled-but-known CLI names
    the flag that would enable it; an unknown one lists what is available. Returning the detail
    rather than raising lets callers choose their own failure shape (the task service raises
    :class:`ValueError` → HTTP 400; the spawner lets it surface as the lifecycle failure detail).
    """
    if agent_cli is None:
        return None
    available = available_agent_clis(env)
    if agent_cli in available:
        return None
    if agent_cli == CODEX_AGENT_CLI:
        return (
            f"agent CLI {agent_cli!r} is disabled; set {CODEX_FLAG}=1 on the control plane "
            "(and its runner) to enable it"
        )
    return f"unknown agent CLI {agent_cli!r}; available: {list(available)}"


def require_available_agent_cli(
    agent_cli: str | None, env: Mapping[str, str] | None = None
) -> None:
    """Raise :class:`ValueError` unless ``agent_cli`` is selectable in ``env`` (``None`` is fine)."""
    if detail := agent_cli_unavailable_detail(agent_cli, env):
        raise ValueError(detail)

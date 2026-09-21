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
import os
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
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


#: How soon after launch a **non-zero** exit counts as "the resume itself was refused" rather than a
#: session that ran and then failed (:meth:`AgentCLI.should_retry_after_resume`). The observed
#: refusals are sub-second — the CLI prints ``No conversation found to continue`` and exits before it
#: draws anything — so this is generous for a slow container while staying far from a session the
#: agent actually worked in.
RESUME_FAILURE_WINDOW_SECONDS = 30.0

#: How many times :meth:`AgentCLI.launch` quarantines a refused resume target and relaunches before
#: it clears *every* remaining candidate and starts fresh. Bounds the launch loop at three execs.
MAX_RESUME_FALLBACKS = 2

#: Appended to a quarantined session file. Renamed, never deleted — the file stays on the per-task
#: config volume as evidence, and stops being a resume candidate.
QUARANTINE_SUFFIX = ".broken"


def _run_process(argv: list[str], env: Mapping[str, str]) -> int:  # pragma: no cover - the real CLI
    """Run the agent CLI in the foreground and return its exit code (the LLM-bearing exec).

    The one line :class:`AgentCLI.launch` cannot cover in tests; it is injected there as a fake so
    the surrounding resume/fallback policy *is* covered (AGENTS.md "No LLMs in tests").
    """
    return subprocess.run(argv, env=dict(env)).returncode


#: The process runner :meth:`AgentCLI.launch` drives — injected as a fake in tests.
ProcessRunner = Callable[[list[str], Mapping[str, str]], int]


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
    #: The env var naming this CLI's config dir (``CLAUDE_CONFIG_DIR`` / ``CODEX_HOME``). The runner
    #: mounts that dir as a per-task volume, so it is also where session history lives — get this
    #: wrong and resume silently breaks (ADR 0014 §4a). :meth:`launch` sets it.
    CONFIG_ENV_VAR: ClassVar[str]

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
    def launch_argv(
        self,
        config_dir: Path,
        cwd: Path,
        *,
        initial_prompt: str | None = None,
        turn: str | None = None,
        starting_model: str | None = None,
    ) -> list[str]:
        """The CLI's argv, resuming :meth:`resume_target` when there is one, else a first run.

        A first run carries ``starting_model`` (resolved via :meth:`resolve_model`) and, when set,
        ``initial_prompt`` as the CLI's first message; a resume carries neither — the agent is
        already mid-task — but does carry an interrupt prompt when ``turn == "agent"``, so a
        respawned container picks up where it left off instead of waiting for input.
        """

    @abstractmethod
    def resume_target(self, config_dir: Path, cwd: Path) -> Path | None:
        """The session file :meth:`launch_argv` would resume for ``cwd``, or ``None`` for a first run.

        Two callers need this, which is why it's a seam rather than an argv detail: :meth:`launch`
        asks "did we just resume?" to decide whether a fast failure is the *resume* failing, and it
        asks "resume what?" to know which file to :meth:`quarantine` when it was.
        """

    def prune_unresumable(self, config_dir: Path, cwd: Path) -> list[Path]:
        """Quarantine session files this CLI would offer to resume but is known to refuse.

        The *preventive* half of the resume fallback, and a **bootstrap** step — deterministic,
        unit-tested, run by the launcher before :meth:`launch`, so the argv builder stays pure. The
        default is a no-op: an adapter overrides it only when it can recognise an unresumable
        session (see :class:`~panopticon.container.cli.claude.ClaudeAgentCLI`). Returns the
        quarantined paths. :meth:`launch`'s fallback covers whatever this misses.
        """
        return []

    def quarantine(self, path: Path) -> Path | None:
        """Rename ``path`` out of the way of resume selection; return the new path, or ``None``.

        The file is **renamed, never deleted** — it stays on the per-task config volume as evidence
        of why a resume was refused, and a mistaken quarantine costs a chat history, not work. Best
        effort: any :class:`OSError` yields ``None`` (a launch must not die trying to tidy up).
        """
        for n in range(100):
            suffix = QUARANTINE_SUFFIX if n == 0 else f"{QUARANTINE_SUFFIX}.{n}"
            candidate = path.with_name(path.name + suffix)
            if candidate.exists():
                continue
            try:
                return path.rename(candidate)
            except OSError:
                return None
        return None

    def should_retry_after_resume(self, returncode: int, elapsed: float) -> bool:
        """Whether a just-finished **resumed** launch failed in the way that means "resume refused".

        True only for a *positive* exit code inside :data:`RESUME_FAILURE_WINDOW_SECONDS`:

        - a **negative** code is death by signal — the container is going down (the entrypoint's
          SIGTERM), not a refused resume, and relaunching would fight the teardown;
        - ``0`` is a session that ran and exited cleanly;
        - a slow failure is a session that genuinely started, so its history is worth keeping.
        """
        return returncode > 0 and elapsed < RESUME_FAILURE_WINDOW_SECONDS

    def launch(
        self,
        config_dir: Path,
        *,
        run: ProcessRunner = _run_process,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Run the CLI in the foreground (resuming if there's anything to resume); return on exit.

        Unlike an ``exec``, this returns control to the launcher when the CLI exits, so it can stop
        the container (the task → down → respawn). The CLI inherits this pane's TTY — the
        interactive surface ``tmux attach`` reaches — and is pointed at its config dir via
        :attr:`CONFIG_ENV_VAR`. The env is overlaid with :meth:`launch_env` so the CLI, and
        everything it shells out to, sees normalized credentials (see :func:`unquote_secret`); for a
        correctly written env-file the overlay is empty and nothing changes.

        **Resume is advisory.** A session file existing doesn't prove the interactive CLI will
        accept it: an SDK-written claude transcript is refused outright, and the CLI exits non-zero
        before drawing anything — which exits the tmux pane's command, destroys the session, and
        leaves the task unstartable and unattachable however many times it's respawned. So when a
        *resumed* launch fails that way (:meth:`should_retry_after_resume`), the file it tried to
        resume is quarantined (:meth:`quarantine`) and the CLI relaunched. Losing chat history
        beats a task that can't start — the plan, the memo, the branch and the tree survive — and
        because the resume target is always the newest *surviving* session, an older healthy one
        becomes the target, so history is usually recovered rather than dropped.

        The loop terminates structurally: each pass either returns or removes at least one
        candidate, and after :data:`MAX_RESUME_FALLBACKS` it clears every remaining candidate, so
        the final pass is necessarily a first run. Three execs, worst case.

        ``run`` and ``clock`` are injected so this policy is unit-tested with a fake; only the
        default :func:`_run_process` touches a real CLI (AGENTS.md "No LLMs in tests").
        """
        cwd = Path.cwd()
        env = {
            **os.environ,
            **self.launch_env(os.environ),
            self.CONFIG_ENV_VAR: str(config_dir),
        }
        attempts = 0
        while True:
            target = self.resume_target(config_dir, cwd)
            argv = self.launch_argv(
                config_dir,
                cwd,
                initial_prompt=os.environ.get("PANOPTICON_INITIAL_PROMPT") or None,
                turn=os.environ.get("PANOPTICON_TASK_TURN") or None,
                starting_model=os.environ.get("PANOPTICON_STARTING_MODEL") or None,
            )
            started = clock()
            returncode = run(argv, env)
            elapsed = clock() - started
            if target is None or not self.should_retry_after_resume(returncode, elapsed):
                return
            attempts += 1
            print(
                f"panopticon: {self.name} refused to resume {target.name} "
                f"(exit {returncode} after {elapsed:.1f}s) — quarantining it and relaunching",
                file=sys.stderr,
                flush=True,
            )
            if attempts > MAX_RESUME_FALLBACKS:
                return  # relaunched enough; stop rather than loop on a CLI failing for some other reason
            if attempts == MAX_RESUME_FALLBACKS:
                # Last retry: clear every remaining candidate so this pass is necessarily a first run.
                for path in self.resume_candidates(config_dir, cwd):
                    self.quarantine(path)
            else:
                self.quarantine(target)

    def resume_candidates(self, config_dir: Path, cwd: Path) -> list[Path]:
        """Every session file :meth:`resume_target` might pick for ``cwd`` (newest first).

        :meth:`launch` clears these wholesale once it has retried :data:`MAX_RESUME_FALLBACKS`
        times, to guarantee the next pass is a first run. The default returns the current target
        alone, which is correct for an adapter that only ever has one.
        """
        target = self.resume_target(config_dir, cwd)
        return [target] if target is not None else []


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

"""Core domain models — pure data, no I/O, no LLM.

These types are the vocabulary the whole system shares. Most are plain records; the exception
is :class:`Task`, which carries behavior over **its own record** — fulfilling the
responsibilities it promised on entry and reporting which remain outstanding. The *rules of
the state machine* (which transitions are legal, what each state means) live in
:class:`panopticon.core.workflow.Workflow`, and the state classes live in
:mod:`panopticon.core.state`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Actor(str, Enum):
    """A party that can act on a task: the user or the agent.

    The same two parties answer every "who?" in the model — who holds the turn
    (``Task.turn``, ``State.turn_on_enter``) and who transitions out of a state
    (``State.advanced_by``).
    """

    USER = "user"
    AGENT = "agent"


class Status(str, Enum):
    """Resolution status of a single responsibility."""

    PENDING = "pending"  # not yet resolved — blocks handing the turn back
    MET = "met"
    FAILED = "failed"  # could not be satisfied; requires a comment


class LifecyclePhase(str, Enum):
    """A step the **session service** reports as it brings a task's container up (ADR 0008).

    These are the *pre-live* phases the runner pushes over ``PUT /tasks/{id}/lifecycle`` as it
    claims, prepares, builds, and launches — the feedback that used to be invisible. ``LIVE`` /
    ``DOWN`` / ``QUEUED`` / ``DISCONNECTED`` are **not** here: they're derived by the task service
    from registration presence + runner liveness (see :func:`compose_container_status`), so the
    runner never invents them. Each value equals its :class:`ContainerStatus` counterpart so a
    reported phase maps straight through.
    """

    HEALING = "healing"  # an orphan flagged for self-heal; queued behind the serial respawn
    CLAIMING = "claiming"  # claimed the task; spawn about to start
    PREPARING = "preparing"  # readying the per-task clone / workspace
    BUILDING = "building"  # composing + docker-building the image (the slow first-run step)
    STARTING = "starting"  # docker run + the tmux session coming up
    AWAITING = "awaiting"  # container + tmux up; waiting for it to open its /live registration
    FAILED = "failed"  # a spawn step raised (carries a detail string)


class ContainerStatus(str, Enum):
    """A task's *composed* container-lifecycle status — the single string the dashboard displays.

    The task service computes it (see :func:`compose_container_status`) by folding the session
    service's reported :class:`LifecyclePhase` together with container-registration presence and
    runner liveness, so the dashboard renders it verbatim instead of guessing.
    """

    NONE = "–"  # terminal task — no container concept
    QUEUED = "queued"  # unclaimed, non-terminal — waiting for a runner to claim it
    HEALING = "healing"  # claimed, container gone, the runner is self-healing it (orphan respawn)
    CLAIMING = "claiming"
    PREPARING = "preparing"
    BUILDING = "building"
    STARTING = "starting"
    AWAITING = "awaiting"
    LIVE = "live"  # a container registration is open
    DOWN = "down"  # claimed, runner live, container gone and unregistered → respawn with `R`
    FAILED = "failed"  # a spawn step raised
    DISCONNECTED = "disconnected"  # claimed by a runner no longer connected to the task service


def compose_container_status(
    *,
    terminal: bool,
    claimed: bool,
    registered: bool,
    runner_live: bool,
    phase: LifecyclePhase | None,
) -> ContainerStatus:
    """Fold the lifecycle signals into one displayed status — pure, so it's unit-testable alone.

    Order matters (first match wins): a terminal task has no container; an unclaimed one is
    ``QUEUED``; an open container registration is ``LIVE`` regardless of anything else (the
    container holds its own ``/live`` connection independent of its runner); a claim held by a
    runner that's no longer connected is ``DISCONNECTED`` (even if it left a stale phase behind);
    otherwise a reported spawn ``phase`` shows through; and a claimed task with a live runner but
    no phase and no registration is ``DOWN`` (came up and vanished, or never reported).
    """
    if terminal:
        return ContainerStatus.NONE
    if not claimed:
        return ContainerStatus.QUEUED
    if registered:
        return ContainerStatus.LIVE
    if not runner_live:
        return ContainerStatus.DISCONNECTED
    if phase is not None:
        return ContainerStatus(phase.value)
    return ContainerStatus.DOWN


@dataclass(frozen=True)
class Responsibility:
    """An agent obligation for a state.

    The workflow supplies these as *definitions* (``status`` ``PENDING``, no comment). The
    agent resolves each to ``MET`` or ``FAILED`` before handing the turn back; a ``FAILED``
    responsibility must carry a ``comment`` explaining why. They are agent-only — user
    actions drive transitions directly rather than being modelled as responsibilities.
    """

    key: str
    description: str
    status: Status = Status.PENDING
    comment: str | None = None

    def resolve(self, status: Status, comment: str | None = None) -> Responsibility:
        """Return a resolved copy carrying the definition's ``key``/``description``."""
        return Responsibility(
            key=self.key, description=self.description, status=status, comment=comment
        )


@dataclass(frozen=True)
class Skill:
    """A workflow-specific, agent-driven procedure exposed in the task container (ADR 0004).

    Workflow-agnostic by design: a workflow declares skills as data (``name``, one-line
    ``description``, and ``instructions`` — the agent procedure); the agent layer renders them to
    whatever the active CLI needs (claude slash-commands for now, other CLIs in M3). On top of
    the core operations (advance/drop), and present only if the active workflow defines them.
    """

    name: str
    description: str
    instructions: str


@dataclass(frozen=True)
class Tool:
    """A command-line tool a workflow expects in its task container, named so the agent knows to use
    it. Declared as data (``name`` + one-line ``description`` of what it's for) and listed in the
    agent's system prompt; the *install* is the workflow's image layer (ADR 0005, e.g.
    github-peer-reviewed's ``gh``). Beyond the base image's shell/git, present only if the active
    workflow declares them.
    """

    name: str
    description: str


@dataclass
class Repo:
    """A repository tasks operate on.

    Holds *references* to its per-repo secrets (ADR 0007), never the values: ``env_file`` is a
    host path to an env-file of API-key-style secrets, and ``creds_volume`` names a persisted
    volume of OAuth credential files. Both are injected into the task container at launch (the
    runner), so secrets stay out of the DB, artifacts, and image layers.

    ``image_layer_file`` *references* the repo's Dockerfile fragment (ADR 0005's repo tier): a file
    name resolved relative to the task service's layers directory, not inline content. The task
    service reads it to serve over REST (``GET /repos/{id}/image-layer``) and the runner composes
    base → workflow → **repo** into the task image, so a repo can layer on its toolchain (e.g. `uv`,
    `make`). Empty/None = no repo layer.

    ``capabilities`` is a per-repo opt-in map for elevated container privileges the runner grants at
    spawn. ``docker_in_docker`` (a privileged nested Docker daemon) is the first — off by default,
    since it's a trust escalation (a privileged container ≈ host root).
    """

    id: str
    name: str
    git_url: str
    default_base: str = "main"
    env_file: str | None = None
    creds_volume: str | None = None
    image_layer_file: str | None = None
    capabilities: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HistoryEntry:
    """One entry in a task's log — recorded when the task *enters* ``to_state``.

    Timestamps are passed in by the caller (the task service stamps them); the core
    never reads the clock, which keeps the state machine deterministic and testable.

    On entry, ``responsibilities`` is seeded with the destination state's obligations, all
    ``PENDING`` — a promise to fulfil them before leaving. The agent then resolves them **one
    at a time**, which replaces entries in this list in place; that is the *only* mutable part
    of an otherwise append-only, frozen record (the transition facts never change).
    """

    at: str  # ISO-8601 timestamp, supplied by the caller
    from_state: str | None
    to_state: str
    trigger: str | None = None  # what triggered the transition (e.g. "start", "advance")
    note: str | None = None
    responsibilities: list[Responsibility] = field(default_factory=list)


@dataclass
class Task:
    """A unit of work. Identity is the internal ``id``; ``slug`` is a human label set later.

    A task carries behavior over **its own record** — fulfilling the responsibilities it
    promised on entering its current state, and reporting which remain outstanding. It knows
    nothing of the state machine's rules; those live in
    :class:`~panopticon.core.workflow.Workflow`, which drives the task across states.
    """

    id: str
    repo_id: str
    workflow: str
    state: str
    turn: Actor
    #: A deliberate "waiting on something" marker the agent sets; it is **orthogonal to the
    #: turn** and survives turn flips (cloude-cade's `:blocked:`), cleared only explicitly.
    blocked: bool = False
    #: A brief, one-line reminder of what the task is, collected when the task is created (shown
    #: in the dashboard's task summary) — a human label of *intent*, not a full description (that
    #: lives in the task's plan artifact). Distinct from the ``slug`` (a short identifier the
    #: agent sets later); ``None`` when the creator gave none.
    memo: str | None = None
    slug: str | None = None
    #: An optional external URL for the task — its pull request, an issue, a dashboard link
    #: (cloude-cade's ``pr_url``). Set via :meth:`TaskService.set_url`; the dashboard's ``p``
    #: hotkey opens it. ``None`` until something records one (e.g. the ``open-pr`` skill).
    url: str | None = None
    #: The git refs the session service provisions for this task once the slug is set (ADR
    #: 0010/0011): the slug-named branch and the path of the per-task ``clone`` it works in **on
    #: the host where the container runs**. The task service only records these — it does no git
    #: itself — so this stays correct when the runner is remote. Both ``None`` until provisioning.
    branch: str | None = None
    clone: str | None = None
    #: The runner that has **claimed** this task (its ``runner_id``), or ``None`` if unclaimed. A
    #: session service claims an unclaimed task before spawning its container, so exactly one host
    #: owns it; the claim is the spawn gate (ADR 0008). Released (back to ``None``) to hand it off
    #: or have it respawned. Distinct from liveness — a claimed task whose container died is
    #: "claimed but down".
    claimed_by: str | None = None
    #: Cumulative tokens the ``claude`` agent in this task's container has used (input + output +
    #: cache-creation + cache-read, summed across the session). The container's Stop hook reports
    #: it via :meth:`TaskService.set_tokens_used`, recomputing the session total each turn; the
    #: dashboard shows it in short human form. ``None`` until the first report.
    tokens_used: int | None = None
    #: The agent's *forecast* of the total tokens this task will consume, set once during planning
    #: via :meth:`TaskService.set_token_estimate` (distinct from ``tokens_used``, the running
    #: actual). The GithubForge workflows and the orchestrator record it when producing the plan.
    #: ``None`` until estimated.
    token_estimate: int | None = None
    history: list[HistoryEntry] = field(default_factory=list)

    @property
    def provisioned(self) -> bool:
        """True once the session service has provisioned this task — its branch (and per-task
        clone) are recorded (ADR 0011). Until then the task has at most a slug, no working branch.
        """
        return self.branch is not None

    @property
    def current_entry(self) -> HistoryEntry:
        """The latest history entry — the one recorded on entering the current state."""
        return self.history[-1]

    def resolve_responsibility(
        self, *, key: str, status: Status, comment: str | None = None
    ) -> None:
        """Resolve one responsibility promised on entering the current state, in place.

        Resolves the matching promise on :attr:`current_entry`. ``status`` must be ``MET`` or
        ``FAILED`` (the latter requires a ``comment``). Raises :class:`ValueError` for an
        unknown key, a ``PENDING`` status, or a ``FAILED`` without a comment.
        """
        if status is Status.PENDING:
            raise ValueError("resolve a responsibility as MET or FAILED, not PENDING")
        if status is Status.FAILED and not (comment and comment.strip()):
            raise ValueError(f"FAILED responsibility {key!r} requires a comment")
        promised = self.current_entry.responsibilities
        for i, definition in enumerate(promised):
            if definition.key == key:
                promised[i] = definition.resolve(status, comment)
                return
        raise ValueError(f"no responsibility {key!r} promised in state {self.state!r}")

    @property
    def outstanding_responsibilities(self) -> list[Responsibility]:
        """Promises on the current entry still unresolved (``PENDING``).

        An empty result means the turn may be handed back and the task may advance. A
        ``FAILED`` promise counts as resolved — :meth:`resolve_responsibility` already requires
        its comment, so it never lingers here.
        """
        return [r for r in self.current_entry.responsibilities if r.status is Status.PENDING]

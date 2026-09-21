"""The tarot review-artifact gate: the one ITERATING responsibility that is **verified, not
self-attested**.

Every other responsibility is a promise the agent resolves itself. For a repo that opts in
(``Repo.capabilities["tarot_review"]``, see :mod:`panopticon.workflows.github_forge`), this one is
checked for real: before an `advance` out of ITERATING is allowed, the task's `.tarot/` review
artifacts must pass `tarot strands check` / `tarot tour check`. The checks run **on the host**,
against the task's per-task clone — which is the very directory bind-mounted into the container at
``/workspace``, so they see exactly what the agent just wrote (:mod:`panopticon.core.tarot`).

Failure **refuses the transition** (:class:`TarotGateRefused`, surfaced to the agent as the failing
tool call) and records the checks' output as the responsibility's comment. The refusal is the
enforcement, not the comment: a ``FAILED``-with-comment responsibility counts as *resolved* by
:meth:`~panopticon.core.workflow.Workflow.apply_transition`, so the gate has to run on **every**
attempt rather than only while the promise is pending.

Deterministic and LLM-free: only subprocess + task state. Policy lives here; the argv lives in
:mod:`panopticon.core.tarot`.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path

from panopticon.core.models import Repo, Skill, Status, Task
from panopticon.core.tarot import CommandRunner, TarotCLI, base_args, subprocess_runner

#: The `Repo.capabilities` key that opts a repo into this gate (mirrors `docker_in_docker`).
CAPABILITY = "tarot_review"
#: Optional per-repo override (an int) of the trivial-diff line threshold, under `capabilities`.
THRESHOLD_CAPABILITY = "tarot_review_threshold"
#: The ITERATING responsibility this gate verifies (`GithubForgeWorkflow.TAROT_REVIEW_ARTIFACTS`).
RESPONSIBILITY_KEY = "tarot-review-artifacts"
#: Below this many total changed lines (added + removed), the diff is trivial and the checks are
#: skipped — there is no tour worth writing for a one-line fix.
DEFAULT_TRIVIAL_THRESHOLD = 20
#: The operation this gate guards.
GATED_TRIGGER = "advance"

_NO_BINARY = (
    "The tarot review gate is enabled for this repo (`capabilities.tarot_review`), but no `tarot` "
    "was found on the task service host's PATH. Install it on the host (`uv tool install "
    "tarot-review`), or set $PANOPTICON_TAROT_BIN, or clear `capabilities.tarot_review` on the "
    "repo to opt out. The task container does not need tarot — the checks run host-side."
)

_AUTHORING_HINT = (
    "Use the `tarot_strand_seed`, `tarot_tour_scaffold` and `tarot_check` tools to author and "
    "iterate on the `.tarot/` artifacts, then advance again."
)


class TarotGateRefused(Exception):
    """Raised instead of transitioning when the review artifacts don't pass (→ HTTP 409).

    Its message is the checks' own output plus a pointer at the authoring tools, so it lands in
    the agent's context the way a failing test's output would.
    """


@dataclass(frozen=True)
class GateDecision:
    """What the gate concluded: whether to allow the transition, and what to record.

    ``resolution`` is a ``(status, comment)`` to write onto the ``tarot-review-artifacts``
    responsibility — set on both the allow paths (``MET``) and the refusal (``FAILED``), so the
    outcome is visible in the task's history either way.
    """

    allowed: bool
    resolution: tuple[Status, str] | None = None
    refusal: str | None = None


def _clone_missing_message(task: Task, runner_host: str | None) -> str:
    if runner_host is not None:
        return (
            f"The tarot review gate runs on the task service host, but task {task.id}'s clone is on "
            f"runner host {runner_host!r}. Run the checks there, or clear "
            "`capabilities.tarot_review` on the repo."
        )
    where = f" ({task.clone})" if task.clone else ""
    return (
        f"The tarot review gate is enabled for this repo, but task {task.id} has no clone readable "
        f"on the task service host{where}, so the review artifacts can't be checked."
    )


class TarotGate:
    """Decides whether an `advance` out of ITERATING may proceed, for an opted-in repo.

    ``cli`` and ``run`` are injectable so the task service's tests never need a real `tarot` or a
    real repo on disk. ``clone_exists`` is the on-disk probe, injected for the same reason.
    """

    def __init__(
        self,
        *,
        cli: TarotCLI | None = None,
        run: CommandRunner = subprocess_runner,
        clone_exists: Callable[[str], bool] | None = None,
    ) -> None:
        self._cli = cli if cli is not None else TarotCLI(run=run)
        self._run = run
        self._exists = clone_exists or (lambda path: Path(path).is_dir())

    @property
    def cli(self) -> TarotCLI:
        """The underlying CLI adapter — the skill assembly reads tarot's packaged skill through it."""
        return self._cli

    @staticmethod
    def opted_in(repo: Repo | None) -> bool:
        return bool(repo is not None and repo.capabilities.get(CAPABILITY))

    @staticmethod
    def threshold(repo: Repo) -> int:
        """The repo's trivial-diff cutoff. ``bool`` is excluded deliberately — it's an ``int``
        subclass, and a stray ``true`` in the capability map must not mean a threshold of 1."""
        value = repo.capabilities.get(THRESHOLD_CAPABILITY)
        if isinstance(value, bool) or not isinstance(value, int):
            return DEFAULT_TRIVIAL_THRESHOLD
        return value

    def applies(
        self, task: Task, repo: Repo | None, *, trigger: str | None, declared: Collection[str]
    ) -> bool:
        """Whether this transition is one the gate guards.

        Three things must hold: it's an `advance` (drops and free moves are never gated), the repo
        opted in, and **the active workflow actually declares the responsibility for this state**
        (``declared`` is the workflow's responsibility keys for the state being left).

        That last condition is why this doesn't simply test ``task.state == "ITERATING"``: `Spike`
        has an ITERATING state too, but declares no review-artifact responsibility. Gating on the
        label alone would refuse every spike advance on an opted-in repo, with no responsibility
        to satisfy and no way out but a free move. Keying off the declaration means the gate
        guards exactly what the workflow promised, and a workflow that promises nothing is never
        gated.
        """
        return trigger == GATED_TRIGGER and self.opted_in(repo) and RESPONSIBILITY_KEY in declared

    def changed_line_count(self, clone: str, base_ref: str) -> int | None:
        """Total added+removed lines between the base and HEAD, or ``None`` when unknowable.

        ``None`` (the numstat command itself failed — e.g. the base ref isn't fetched) means
        **unknown, not trivial**: the caller runs the checks rather than waving the diff through.
        An earlier in-container version of this gate summed an empty output to ``0`` and
        auto-resolved the responsibility, which is precisely the silent pass this gate exists to
        prevent. Binary files report ``-`` counts, which don't parse and are skipped.
        """
        result = self._run(["git", "-C", clone, "diff", "--numstat", f"{base_ref}...HEAD"])
        if not result.ok:
            return None
        total = 0
        for line in result.output.splitlines():
            added, _, rest = line.partition("\t")
            removed, _, _path = rest.partition("\t")
            for count in (added, removed):
                if count.isdigit():
                    total += int(count)
        return total

    def unusable_reason(self, task: Task, *, runner_host: str | None = None) -> str | None:
        """Why tarot can't be run for this task here, or ``None`` when it can.

        Shared by the gate and the authoring tools so they refuse for the same reasons with the
        same words: no binary on this host, or no clone readable on this host (including the
        remote-runner case, where the clone exists but not *here*).
        """
        if not self._cli.available:
            return _NO_BINARY
        if not task.clone or runner_host is not None or not self._exists(task.clone):
            return _clone_missing_message(task, runner_host)
        return None

    def evaluate(self, task: Task, repo: Repo, *, runner_host: str | None = None) -> GateDecision:
        """Run the gate for a task the caller has already decided :meth:`applies`."""
        unusable = self.unusable_reason(task, runner_host=runner_host)
        if unusable is not None:
            return GateDecision(allowed=False, refusal=unusable)
        assert task.clone is not None  # guaranteed by unusable_reason

        # One read of the clone's `tarot.base`, feeding both the ref we diff against and the
        # arguments tarot gets — a clone that pins its own base must not be diffed against the
        # repo default (nor, as an earlier draft did, against HEAD, which reads as a 0-line diff
        # and waves every such task through as "trivial").
        configured = self._cli.configured_base(task.clone)
        base_ref = configured or f"origin/{repo.default_base}"
        tarot_base_args = base_args(configured, repo.default_base)
        changed = self.changed_line_count(task.clone, base_ref)
        if changed is not None and changed < self.threshold(repo):
            return GateDecision(
                allowed=True,
                resolution=(
                    Status.MET,
                    f"trivial diff ({changed} changed lines) — tarot review skipped",
                ),
            )

        outcome = self._cli.check(task.clone, base_args=tarot_base_args)
        if outcome.missing_binary:  # vanished between the probe and the run
            return GateDecision(allowed=False, refusal=_NO_BINARY)
        if outcome.ok:
            return GateDecision(
                allowed=True,
                resolution=(Status.MET, "verified by tarot strands check / tarot tour check"),
            )
        detail = outcome.output.strip() or "the tarot review checks failed with no output"
        return GateDecision(
            allowed=False,
            resolution=(Status.FAILED, detail),
            refusal=f"{detail}\n\n{_AUTHORING_HINT}",
        )


#: What differs between tarot's packaged skill and a task container. Deliberately a **name
#: mapping**, not a rewrite: every word about what a strand is, what a description is for, and what
#: makes a tour note worth reading stays tarot's, because tarot owns that contract and panopticon
#: paraphrasing it would be a second copy to rot.
_PREAMBLE = """\
The instructions below are tarot's own authoring skill, served verbatim. The `tarot` CLI is **not**
installed in this container — it runs on the host — so wherever the skill names a command, call the
matching MCP tool instead:

| tarot's skill says | call instead |
| --- | --- |
| `tarot strands suggest` (step 1) | `tarot_strand_seed` |
| `tarot strands check` (step 3) | `tarot_check` |
| `tarot tour scaffold --from-strands` (step 4) | `tarot_tour_scaffold` |
| `tarot tour check` (step 6) | `tarot_check` |

Every other step — editing the seed, writing the narrative, committing — is plain file editing in
`/workspace`, exactly as written. The checks also run automatically when you `advance`, and a
failure refuses the advance; `tarot_check` is for iterating before you get there, not a substitute
for it.

If `tarot_tour_scaffold` can't run, a tour step can be hand-written: it needs a resolvable
`node_id` plus `title`/`note`, and `trail: []`, `cursor: null`, `base_ref: null` all validate. That
is the degraded path — scaffolded steps carry a real trail/cursor and blast-radius steps a hand
enumeration can't produce, so prefer the tool.

---

"""


def authoring_skill(cli: TarotCLI) -> Skill | None:
    """Tarot's packaged authoring skill as a panopticon :class:`~panopticon.core.models.Skill`.

    ``None`` when tarot isn't on the host (or its skill layout moved) — in which case the gate's
    own missing-binary refusal is what tells the operator. We deliberately don't ship a
    panopticon-authored paraphrase as a consolation prize: a schema summary would teach the cheap
    half (mechanics, which the checks already teach by rejecting you) and omit the expensive half
    (judgment), which is the half that decides whether the artifacts are worth reading.
    """
    body = cli.authoring_skill()
    if not body:
        return None
    return Skill(
        name="tarot-authoring",
        description=(
            "Author the `.tarot/` review artifacts (strand seed + tour) this repo requires before "
            "advancing out of ITERATING."
        ),
        instructions=_PREAMBLE + body,
    )

"""The tarot adapter: invoking the operator's `tarot` CLI on the **host**, LLM-free.

`tarot` (the review tool) is installed by the operator on the host, not baked into task images —
so every tarot invocation panopticon makes happens here, host-side, against a task's per-task
clone (ADR 0011). That clone is bind-mounted into the task container at ``/workspace``, so a
command run here reads and writes exactly what the in-container agent sees: no copy, no sync, no
staleness window. That equivalence is what lets the review-artifact gate (and the authoring
passthroughs) live in the control plane while the agent's container stays tarot-free.

Like :mod:`panopticon.core.git`, this shells out behind an **injectable command-runner** so it is
unit-testable without a real `tarot`, and it is the second I/O-bearing module in `core`; the
domain models and the state machine stay pure. No LLM runs here (the determinism invariant) —
`tarot`'s checks are deterministic static analysis.

Nothing in this module decides *policy* (when to check, what to do about a failure). It builds
argv, runs it, and reports what happened; the task service owns the policy.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

#: Environment override for the tarot executable, for a host where it isn't on `PATH` under that
#: name. Honoured by :func:`binary`, which is also what the dashboard's `v` review hook resolves
#: through — so the gate and the reviewer always agree on which binary is "the" tarot.
BINARY_ENV_VAR = "PANOPTICON_TAROT_BIN"

#: The executable name looked up on `PATH` when no override is set.
DEFAULT_BINARY = "tarot"

#: The git config key a clone may set to pin its own diff base. When present we pass no ``--base``
#: at all and let tarot read its own config (mirroring the dashboard's review invocation).
BASE_CONFIG_KEY = "tarot.base"

#: Where `tarot skill install` writes, relative to the target directory it's given.
SKILL_RELPATH = Path(".claude") / "skills" / "tarot-authoring" / "SKILL.md"


@dataclass(frozen=True)
class CommandResult:
    """The outcome of one external command — never raises; the caller inspects it.

    ``found`` is ``False`` when the executable itself wasn't on `PATH`, which callers must
    distinguish from a command that ran and failed: a missing binary is an operator
    misconfiguration, a non-zero exit is a real finding about the code.
    """

    returncode: int
    output: str  # combined stdout+stderr
    found: bool = True

    @property
    def ok(self) -> bool:
        return self.found and self.returncode == 0


class CommandRunner(Protocol):
    def __call__(self, args: Sequence[str], *, cwd: str | None = None) -> CommandResult: ...


def subprocess_runner(args: Sequence[str], *, cwd: str | None = None) -> CommandResult:
    """The default :class:`CommandRunner`: run the command, never raise, report what happened."""
    try:
        proc = subprocess.run(list(args), cwd=cwd, capture_output=True, text=True, check=False)
    except (FileNotFoundError, NotADirectoryError):
        return CommandResult(returncode=127, output=f"{args[0]}: command not found", found=False)
    return CommandResult(returncode=proc.returncode, output=proc.stdout + proc.stderr)


def binary(override: str | None = None) -> str | None:
    """The tarot executable to run: explicit ``override`` → ``$PANOPTICON_TAROT_BIN`` → `PATH`.

    Returns ``None`` when tarot isn't installed — **never raises**. Every caller has to handle
    absence honestly (an operator-facing refusal), so absence is a value, not an exception.
    """
    return override or os.environ.get(BINARY_ENV_VAR) or shutil.which(DEFAULT_BINARY)


def base_args(configured_base: str | None, default_base: str) -> list[str]:
    """The ``--base`` arguments for a clone: ``[]`` when it pins its own base, else the repo's.

    **The one spelling of this ladder**, shared by the review-artifact gate, the authoring
    passthroughs, and the dashboard's `v` review hook. A clone that sets ``tarot.base`` in its git
    config knows its own diff base, so we pass nothing and let tarot read the config; otherwise the
    repo's recorded default base is the branch point. One ladder means the gate checks against
    exactly the base a reviewer will see — a task whose repo targets ``dimitri/pending-fixes`` must
    not be diffed against ``main``, and the two must never disagree about which it is.
    """
    return [] if configured_base else ["--base", f"origin/{default_base}"]


@dataclass(frozen=True)
class CheckOutcome:
    """The result of the review-artifact checks: ``strands check`` then ``tour check``.

    ``ok`` means both passed. ``missing_binary`` distinguishes "tarot isn't installed" from
    "the artifacts are wrong" — the two need completely different messages to the operator.
    ``output`` is the failing command's combined output (tarot prints one violation per line),
    empty when everything passed.
    """

    ok: bool
    output: str = ""
    missing_binary: bool = False


class TarotCLI:
    """Runs the operator's `tarot` against a task's clone. One spelling of every invocation.

    ``run`` is injectable so the task service's tests never need a real `tarot`; ``tarot_binary``
    is resolved once at construction (``None`` when absent — see :meth:`available`).
    """

    def __init__(
        self, *, run: CommandRunner = subprocess_runner, tarot_binary: str | None = None
    ) -> None:
        self._run = run
        self._override = tarot_binary
        self._resolved: str | None = None
        self._skill: str | None = None
        self._skill_loaded = False

    @property
    def _binary(self) -> str | None:
        """The executable, resolved lazily and re-probed while missing.

        The task service is long-lived, so resolving once at construction would mean an operator
        who installs tarot in response to the gate's own "install it on this host" refusal would
        still be refused until they restarted the service. A **hit** is cached (a resolved binary
        doesn't move); a **miss** re-probes, so the fix takes effect on the next advance.
        """
        if self._resolved is None:
            self._resolved = binary(self._override)
        return self._resolved

    @property
    def available(self) -> bool:
        """Whether a tarot executable resolves. Callers refuse honestly when this is False."""
        return self._binary is not None

    def configured_base(self, clone: str) -> str | None:
        """The clone's own ``tarot.base`` git config, or ``None``."""
        result = self._run(["git", "-C", clone, "config", "--get", BASE_CONFIG_KEY])
        return (result.output.strip() or None) if result.ok else None

    def resolve_base_args(self, clone: str, default_base: str) -> list[str]:
        """:func:`base_args` for ``clone``, reading its git config to decide."""
        return base_args(self.configured_base(clone), default_base)

    def _argv(self, *args: str, clone: str, base_args: Sequence[str]) -> list[str]:
        assert self._binary is not None  # guarded by `available` at every call site
        return [self._binary, *args, "--directory", clone, *base_args]

    def check(self, clone: str, *, base_args: Sequence[str]) -> CheckOutcome:
        """`tarot strands check` then `tarot tour check`, stopping at the first failure.

        Exit codes are tarot's: 0 valid, 1 content violations (one per line on stdout), 2
        structural — no seed file yet, malformed JSON, unresolvable repo/base. Both non-zero
        cases are failures for our purposes; the caller passes the output through so the agent
        sees the violations the way it would see a failing test's output.
        """
        if not self.available:
            return CheckOutcome(ok=False, missing_binary=True)
        for subcommand in (("strands", "check"), ("tour", "check")):
            result = self._run(self._argv(*subcommand, clone=clone, base_args=base_args))
            if not result.found:
                return CheckOutcome(ok=False, missing_binary=True)
            if result.returncode != 0:
                return CheckOutcome(ok=False, output=result.output)
        return CheckOutcome(ok=True)

    def suggest(self, clone: str, *, base_args: Sequence[str]) -> CommandResult:
        """`tarot strands suggest --json` — the detector-built strand seed, on stdout.

        ``--json`` is what makes this **read-only**: without it the subcommand writes
        ``.tarot/strands.json`` into the clone. The agent edits the returned seed itself, so a
        host process never mutates the working tree behind its back.
        """
        if not self.available:
            return CommandResult(returncode=127, output="", found=False)
        return self._run(
            self._argv("strands", "suggest", "--json", clone=clone, base_args=base_args)
        )

    def scaffold(
        self, clone: str, *, base_args: Sequence[str], title: str = "PR walkthrough"
    ) -> CommandResult:
        """`tarot tour scaffold --from-strands` — step stubs built from the *edited* seed.

        Unlike :meth:`suggest` this **writes** ``.tarot/tours/<id>.json`` into the clone (tarot
        has no ``--json`` for scaffold). That is the point: it supplies what no agent can derive
        from a diff — one chapter per strand carrying the author's own titles, a real
        trail/cursor per step, and blast-radius steps off tarot's call graph — with every note
        left a ``TODO`` for the agent to replace with real narrative.
        """
        if not self.available:
            return CommandResult(returncode=127, output="", found=False)
        return self._run(
            self._argv(
                "tour",
                "scaffold",
                "--from-strands",
                "--title",
                title,
                clone=clone,
                base_args=base_args,
            )
        )

    def authoring_skill(self) -> str | None:
        """Tarot's packaged authoring skill (its ``SKILL.md`` body), or ``None``.

        Tarot owns every word about what a strand is, what a description is for, and what makes
        a tour note worth reading — panopticon serves that text rather than paraphrasing a
        contract it doesn't own. There is no ``tarot skill show``, so the only public way to read
        the packaged skill is to install it into a scratch directory and read it back; a
        ``show``/``--print`` subcommand upstream would replace this entirely.

        Returns ``None`` — never raises — when tarot is absent or its install layout moves, so a
        tarot upgrade can never take the control plane down. Cached per instance.
        """
        if not self._skill_loaded:
            self._skill = self._read_authoring_skill()
            # Only a real answer is cached: a `None` from "tarot wasn't installed yet" must not
            # outlive the install, for the same reason `_binary` re-probes on a miss.
            self._skill_loaded = self._skill is not None
        return self._skill

    def _read_authoring_skill(self) -> str | None:
        if not self.available:
            return None
        assert self._binary is not None
        with tempfile.TemporaryDirectory() as scratch:
            result = self._run([self._binary, "skill", "install", "--target", scratch])
            if not result.ok:
                return None
            try:
                body = (Path(scratch) / SKILL_RELPATH).read_text()
            except OSError:
                return None
        return strip_frontmatter(body) or None


def strip_frontmatter(body: str) -> str:
    """Drop a leading ``---`` YAML frontmatter block, keeping the instructions themselves.

    Tarot's SKILL.md carries claude-skill frontmatter (name/description). Panopticon renders the
    text through its own CLI-agnostic :class:`~panopticon.core.models.Skill` spec, which supplies
    its own frontmatter, so carrying tarot's through would nest one inside another.
    """
    if not body.startswith("---"):
        return body.strip()
    end = body.find("\n---", 3)
    if end == -1:
        return body.strip()
    return body[body.find("\n", end + 1) + 1 :].strip()

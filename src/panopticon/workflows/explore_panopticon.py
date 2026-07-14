"""The ExplorePanopticon workflow — a **shell** workflow (no container) that opens a throwaway clone
of panopticon plus an interactive ``claude`` to help the operator understand and navigate the
codebase.

The second ``runner_type = "shell"`` workflow (after :class:`~panopticon.workflows.setup_repo.
SetupRepo`). Rather than spawn a task container + agent, the session service runs :meth:`shell_script`
directly in a host tmux session: the script clones panopticon into a self-cleaning temporary
directory, checks out the version the operator is running (the public remote at its ``v<version>``
tag), and launches a plain interactive ``claude`` in it — framed by an appended system prompt as a
*guide* to the codebase (a read-only tour, not an editing agent). When the operator quits ``claude``,
the temp dir is removed (a ``trap``) and the task is marked ``COMPLETE``.

``RUNNING → {COMPLETE, DROPPED}``. The single state carries no responsibilities (a shell task has no
agent to gate); the script drives the advance to COMPLETE when the operator finishes.

This stays LLM-free control-plane code: the only LLM call is the operator's own interactive
``claude``, spawned by the host shell — not the control plane (the determinism invariant).
"""

from __future__ import annotations

import importlib.resources
import shlex
from typing import ClassVar

from panopticon.core.state import Complete, InitialState
from panopticon.core.workflow import Workflow

#: The canonical panopticon remote. Injected into the script so a **packaged** (non-git) install —
#: which has no local checkout to clone from — still has a remote to clone the matching version from.
_REPO_URL = "https://github.com/Unsupervisedcom/panopticon.git"

#: The shell script the workflow runs, kept in a sibling ``explore_panopticon.sh`` so it's edited
#: (and shell-linted) as a real script rather than a Python string. Read once at import.
_SCRIPT = (importlib.resources.files("panopticon.workflows") / "explore_panopticon.sh").read_text()


class ExplorePanopticon(Workflow):
    """A no-container utility workflow: open a throwaway panopticon clone + ``claude`` to explore it.

    Clones panopticon into a self-cleaning temp dir at the version the operator is running and
    starts an interactive ``claude`` there, prompted to help the operator understand and navigate
    the codebase. Nothing is changed — it's a guided read-only tour.

    ``runner_type = "shell"`` routes it to the session service's shell runner instead of the Docker
    one. It's opt-out (``opt_in = False``) so it's available for every repo, and ``hidden`` keeps
    this operator utility out of both dashboard menus (the repo-form workflow list and the
    task-creation picker) — it's launched instead from the repos modal's explore hotkey, which
    creates an ``explore-panopticon`` task for the highlighted repo (that repo only supplies the
    host shell's ``claude`` credentials; the clone is always panopticon).
    """

    name: ClassVar[str] = "explore-panopticon"
    runner_type: ClassVar[str] = "shell"
    opt_in: ClassVar[bool] = False
    hidden: ClassVar[bool] = True
    when_to_use: ClassVar[str] = (
        "Open a throwaway clone of panopticon (at the version you're running) plus an interactive "
        "`claude` to help you understand and navigate the codebase — a read-only guided tour in a "
        "host shell (no container); the temp clone is cleaned up when you quit."
    )

    class Running(InitialState):
        label = "RUNNING"
        description = "Clone panopticon into a temp dir and open `claude` in it to explore; the script completes the task when the operator quits."
        transitions = (Complete,)  # advance → COMPLETE; + DROPPED inherited

    initial = Running

    def shell_script(self) -> str:
        """Clone panopticon at the running version, open ``claude`` to explore it, then complete.

        The script lives in the sibling ``explore_panopticon.sh``; the canonical remote URL is
        prepended as a ``REPO_URL`` assignment so a packaged install (no local checkout) still has a
        remote to clone from. The session service injects ``PANOPTICON_SERVICE_URL`` /
        ``PANOPTICON_TASK_ID`` (and sources the repo's secrets, so ``claude`` finds an auth token),
        and the script drives its own advance to COMPLETE over REST via the panopticon shell lib."""
        return f"REPO_URL={shlex.quote(_REPO_URL)}\n{_SCRIPT}"

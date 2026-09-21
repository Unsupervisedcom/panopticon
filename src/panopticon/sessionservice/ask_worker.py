"""Host-side ask delivery (ask-the-author): hand a reviewer's question to a task's claude session.

The sibling of :class:`~panopticon.sessionservice.provisioner.Provisioner` for the ask side. The
task service records the *ask* (question → answer) but, being LLM-free and docker-free, never touches
a container; the session service runs **where the container runs**, so it owns delivery:

* a **live** task (its tmux session exists) → inject the message into the running agent's pane
  (:meth:`LocalRunner.send_to_session`);
* a **parked** task (no session — including a COMPLETE one whose author we want to interrogate) →
  resume it via ``claude --continue`` with the question as the prompt
  (:meth:`Spawner.spawn_for_ask`), which allows terminal tasks unlike the normal spawn gate;
* a task whose **config volume was reaped** → mark the ask ``gone`` (the API returns 410 and the
  review tool falls back).

The agent's reply is recorded by the container's Stop hook (it has the transcript + the completion
signal), not here. Delivery is **observed, not pushed** (like provisioning): the host daemon spots a
task carrying an undelivered ask over its work-pull loop (the ``pending_ask_id`` the task service
overlays on the task) and calls :meth:`deliver`. Idempotent + self-gating, so the loop can call it on
every task each pass. LLM-free.
"""

from __future__ import annotations

import logging

from panopticon.client import JsonObj, TaskServiceClient
from panopticon.core.asking import compose_ask_message
from panopticon.core.state import TERMINAL_LABELS
from panopticon.sessionservice.local_runner import LocalRunner
from panopticon.sessionservice.spawner import Spawner

_log = logging.getLogger(__name__)


class AskWorker:
    """Delivers each task's pending ask to its agent — live-inject or ``--continue`` resume."""

    def __init__(
        self,
        client: TaskServiceClient,
        runner: LocalRunner,
        spawner: Spawner,
        *,
        runner_id: str,
    ) -> None:
        self._client = client
        self._runner = runner
        self._spawner = spawner
        self._runner_id = runner_id

    def deliver(self, task: JsonObj) -> str | None:
        """Deliver ``task``'s undelivered ask, if any, returning the ask id (else ``None``).

        No-ops unless the task carries a ``pending_ask_id`` (self-gating, so the daemon can call this
        on every task each pass — once delivered/gone the field clears). Only the host that **owns**
        the task (or an unclaimed one) delivers, since the tmux session and config volume are
        host-local; a task claimed by another runner is left for that host.
        """
        ask_id: str | None = task.get("pending_ask_id")
        if not ask_id:
            return None
        if task.get("claimed_by") not in (None, self._runner_id):
            return None  # another host owns it — its session/volume live there, so it delivers
        task_id = task["id"]
        if not self._runner.config_volume_exists(task_id):
            # The claude session was reaped — the agent can't be resumed. Mark it gone (→ 410).
            self._client.mark_ask_gone(task_id, ask_id)
            _log.info("task %s: ask %s undeliverable (config volume gone)", task_id, ask_id)
            return None
        ask = self._client.get_ask(task_id, ask_id)
        message = compose_ask_message(
            ask["question"],
            ask.get("context") or "",
            terminal=task["state"] in TERMINAL_LABELS,
            ask_id=ask_id,
        )
        if self._runner.has_session(task_id):
            self._runner.send_to_session(task_id, message)  # inject into the live agent's pane
            _log.info("task %s: ask %s injected into live session", task_id, ask_id)
        elif self._spawner.spawn_for_ask(task, message) is None:
            return None  # couldn't claim it (another host won the race) — retry next pass
        else:
            _log.info("task %s: ask %s delivered via --continue resume", task_id, ask_id)
        self._client.mark_ask_delivered(task_id, ask_id)
        return ask_id

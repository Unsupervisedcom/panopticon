"""The operator snooze predicate — pure arithmetic over a recorded deadline (no clock read).

``Task.snoozed_until`` is a recorded fact: the task service stores the operator's deadline verbatim
and never compares it to a clock (the determinism invariant). Deciding whether a deadline is *active*
is therefore the caller's job, and two callers need the same answer:

- the **dashboard**, to mute a snoozed row (its display clock), and
- the **session service**, to stop a snoozed task's container (its host clock, ADR 0008).

So the arithmetic lives here, in ``core``, with ``now`` passed **in** — LLM-free, I/O-free, and
clock-free like the rest of the package.
"""

from __future__ import annotations

from datetime import UTC, datetime

#: The reserved "sticky" deadline: a snooze that never expires, cleared only explicitly. Recorded by
#: the dashboard's `E` hotkey; defined here so the value has exactly one definition.
INDEFINITE_UNTIL = "9999-12-31T23:59:59+00:00"


def snooze_remaining(snoozed_until: str | None, now: datetime) -> float | None:
    """Active seconds remaining on ``snoozed_until`` at ``now``, else ``None``.

    ``float("inf")`` for :data:`INDEFINITE_UNTIL`; ``None`` when there's no deadline, when it has
    already passed, or when it isn't a parseable ISO-8601 string (an unreadable fact is inactive
    rather than an error — it must never take down a display or stall a spawn). Naive datetimes on
    either side are read as UTC.
    """
    if not isinstance(snoozed_until, str):
        return None
    if snoozed_until == INDEFINITE_UNTIL:
        return float("inf")
    try:
        deadline = datetime.fromisoformat(snoozed_until)
    except ValueError:
        return None
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    seconds = (deadline - now).total_seconds()
    return seconds if seconds > 0 else None


def is_snoozed(snoozed_until: str | None, now: datetime) -> bool:
    """Whether ``snoozed_until`` is an **active** snooze at ``now`` (see :func:`snooze_remaining`)."""
    return snooze_remaining(snoozed_until, now) is not None

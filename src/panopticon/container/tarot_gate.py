"""Compatibility shim: the tarot review-artifact gate no longer runs in the container.

Enforcement moved **host-side** (:mod:`panopticon.taskservice.tarot_gate`), where the operator's
`tarot` is installed and the task's clone already lives — the same directory the container sees at
``/workspace``. Nothing in a task image needs tarot any more, and the agent reaches tarot through
the `tarot_strand_seed` / `tarot_check` / `tarot_tour_scaffold` MCP tools instead.

This module survives only for **already-provisioned tasks**: `.claude/settings.json` lives in a
task's persisted config volume and :func:`panopticon.container.config.update_json_config` merges
rather than prunes, so a task respawned after this change still has a `PreToolUse` entry pointing
at ``python -m panopticon.container.tarot_gate``. Deleting the module would make every
`apply_operation` call fail with a hook error in those containers; allowing unconditionally here
makes the stale entry a harmless no-op. Newly rendered settings don't wire it at all
(:mod:`panopticon.container.hooks`), so this can be deleted once no such container remains.
"""

from __future__ import annotations

import contextlib
import sys
from typing import TextIO


def main(*, stdin: TextIO | None = None) -> int:
    """Drain the hook payload and allow the tool call. No output, exit 0."""
    # A closed/absent stdin is not a reason to fail a tool call.
    with contextlib.suppress(OSError, ValueError):
        (stdin or sys.stdin).read()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

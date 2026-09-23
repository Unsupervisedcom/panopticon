"""Host-side resource **priority** for task work: run it at the lowest priority we can.

A task container is the one place where unbounded work happens — an agent running a repo's full
test suite, a `docker build` inside a dind task, six tasks at once — and it shares a machine with
the operator's editor, their shell, and panopticon's own control plane. So every container is
spawned *deprioritized*: it loses every CPU and disk race against a normally-weighted process, and
under real memory pressure the kernel picks it before anything of the operator's. It still uses an
idle host fully — this is priority, **not** a cap: nothing here limits how much CPU or RAM a task
may use when nobody else wants it, so a quiet host runs tasks at full speed.

The knobs (each read from the **runner host's** env, so a big build box and a laptop can differ):

=========================================  ==================================================
``PANOPTICON_CONTAINER_CPU_SHARES``        CPU weight (default 2 = docker's floor, cgroup v2
                                           ``cpu.weight`` 1)
``PANOPTICON_CONTAINER_BLKIO_WEIGHT``      block-IO weight (default 10 = the floor; disk is what
                                           actually makes a desktop stutter)
``PANOPTICON_CONTAINER_OOM_SCORE_ADJ``     OOM-killer preference (default 500 — kill the task,
                                           not the operator's editor)
``PANOPTICON_CONTAINER_CGROUP_PARENT``     opt-in parent cgroup (unset; see below)
``PANOPTICON_HOST_NICE``                   ``nice`` for host-side task work with no cgroup
                                           around it — a shell task's session (default 19)
=========================================  ==================================================

Set any of them to ``off`` (or empty) to drop that flag entirely; the emitted argv is then exactly
what it was before this module existed. An unparseable value warns and falls back to the default
rather than failing a spawn, and values are clamped to what docker/the kernel accept — including
refusing a *negative* OOM adjustment, which would shield a task at the host's expense (the opposite
of this module's job).

**Why a cgroup-parent knob.** With docker's systemd cgroup driver, containers land in
``system.slice/docker-<id>.scope`` while the operator's processes sit in ``user.slice``, and cgroup
weights are compared **between siblings**. A weight of 1 on the container's own scope therefore
deprioritizes it *within* ``system.slice`` but does not by itself make it lose to ``user.slice``.
Operators who want that enforced at the hierarchy level can create a low-weight slice
(``CPUWeight=1``, ``IOWeight=1``) and point :data:`CGROUP_PARENT_VAR` at it; installing a systemd
unit is the operator's call, so the per-container weights remain the zero-setup default.

Pure and LLM-free: env in, argv out. :mod:`panopticon.sessionservice.local_runner` and
:mod:`panopticon.sessionservice.shell_runner` are the callers.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping, Sequence

_log = logging.getLogger(__name__)

#: CPU weight for a task container (``docker run --cpu-shares``).
CPU_SHARES_VAR = "PANOPTICON_CONTAINER_CPU_SHARES"
#: Block-IO weight for a task container (``docker run --blkio-weight``).
BLKIO_WEIGHT_VAR = "PANOPTICON_CONTAINER_BLKIO_WEIGHT"
#: OOM-killer preference for a task container (``docker run --oom-score-adj``).
OOM_SCORE_ADJ_VAR = "PANOPTICON_CONTAINER_OOM_SCORE_ADJ"
#: Opt-in parent cgroup for task containers (``docker run --cgroup-parent``); unset by default.
CGROUP_PARENT_VAR = "PANOPTICON_CONTAINER_CGROUP_PARENT"
#: ``nice`` increment for host-side task work outside any container (a shell task's session).
HOST_NICE_VAR = "PANOPTICON_HOST_NICE"

#: Docker's floor for ``--cpu-shares`` (cgroup v2 turns it into ``cpu.weight`` 1) — the least CPU
#: priority a container can be given while still running when the host is idle.
DEFAULT_CPU_SHARES = 2
#: The floor for ``--blkio-weight`` (default 500), so a task's IO yields to the operator's.
DEFAULT_BLKIO_WEIGHT = 10
#: Positive OOM adjustment: under memory pressure the kernel scores task containers well above the
#: host's own processes, so it kills one of ours first. The runner already explains such a death —
#: ``LocalRunner.exit_reason`` reads ``OOMKilled`` off ``docker inspect`` before the exit code.
DEFAULT_OOM_SCORE_ADJ = 500
#: The maximum ``nice`` increment (lowest scheduling priority) for host-side task work.
DEFAULT_HOST_NICE = 19

#: Values that switch a knob **off** (the flag is omitted). Note ``0`` is *not* one of them: it's a
#: legitimate setting for these flags (docker's default shares, normal niceness), not an opt-out.
_OFF_VALUES = frozenset({"", "off", "none"})

#: The flags that need a working cgroup controller to apply. A daemon whose cgroup is in an odd
#: state (nested docker, some CI images) fails ``docker run`` outright when these are present —
#: :func:`strip_cgroup_flags` is how the runner degrades instead of losing the spawn.
_CGROUP_FLAGS = ("--cpu-shares", "--blkio-weight", "--cgroup-parent")


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return env if env is not None else os.environ


def _int_setting(
    env: Mapping[str, str] | None, var: str, default: int, *, minimum: int, maximum: int
) -> int | None:
    """The integer value of ``var``, clamped to ``minimum..maximum``; ``None`` when switched off.

    Unset → ``default``. Unparseable → ``default`` with a warning: a typo in one env var must not
    take out every spawn on the host."""
    raw = _env(env).get(var)
    if raw is None:
        return default
    value = raw.strip()
    if value.lower() in _OFF_VALUES:
        return None
    try:
        parsed = int(value)
    except ValueError:
        _log.warning("%s=%r is not an integer — using the default %d", var, raw, default)
        return default
    clamped = max(minimum, min(maximum, parsed))
    if clamped != parsed:
        _log.warning("%s=%d is outside %d..%d — using %d", var, parsed, minimum, maximum, clamped)
    return clamped


def container_cgroup_parent(env: Mapping[str, str] | None = None) -> str | None:
    """The opt-in parent cgroup for task containers, or ``None`` when unset (the default)."""
    value = (_env(env).get(CGROUP_PARENT_VAR) or "").strip()
    return value if value.lower() not in _OFF_VALUES else None


def container_priority_flags(env: Mapping[str, str] | None = None) -> list[str]:
    """The ``docker run`` flags that deprioritize a task container, in long form.

    Defaults to ``--cpu-shares 2 --blkio-weight 10 --oom-score-adj 500`` (plus
    ``--cgroup-parent`` when configured). Each flag is omitted when its knob is off, so an
    operator can hand a dedicated build host the exact argv panopticon emitted before this
    existed."""
    flags: list[str] = []
    if (
        shares := _int_setting(env, CPU_SHARES_VAR, DEFAULT_CPU_SHARES, minimum=2, maximum=262144)
    ) is not None:
        flags += ["--cpu-shares", str(shares)]
    if (
        weight := _int_setting(
            env, BLKIO_WEIGHT_VAR, DEFAULT_BLKIO_WEIGHT, minimum=10, maximum=1000
        )
    ) is not None:
        flags += ["--blkio-weight", str(weight)]
    if (adj := container_oom_score_adj(env)) is not None:
        flags += ["--oom-score-adj", str(adj)]
    if (parent := container_cgroup_parent(env)) is not None:
        flags += ["--cgroup-parent", parent]
    return flags


def container_oom_score_adj(env: Mapping[str, str] | None = None) -> int | None:
    """The OOM-killer adjustment a task's processes should carry, or ``None`` when switched off.

    Both the container's own ``docker run --oom-score-adj`` *and* the agent pane use this: an
    exec'd process does **not** inherit the container's score (``oom_score_adj`` is per-process,
    inherited across fork, and ``docker exec`` forks from the daemon, not from the container's
    PID 1), so the pane raises it itself — see ``LocalRunner.spawn``. Clamped to ``0..1000``:
    negative values would make the kernel prefer host processes over a task, which is backwards,
    and would need ``CAP_SYS_RESOURCE`` for the pane to set anyway."""
    return _int_setting(env, OOM_SCORE_ADJ_VAR, DEFAULT_OOM_SCORE_ADJ, minimum=0, maximum=1000)


def host_nice_prefix(env: Mapping[str, str] | None = None) -> list[str]:
    """``["nice", "-n", "<n>"]`` to prefix host-side task work with, or ``[]`` when switched off.

    For work that runs on the host with no container cgroup around it — a shell workflow's session
    (``runner_type="shell"``). ``-n`` is spelled short because ``nice`` has no long option in the
    BSD userland on macOS hosts (AGENTS.md's long-options convention)."""
    nice = _int_setting(env, HOST_NICE_VAR, DEFAULT_HOST_NICE, minimum=0, maximum=19)
    return [] if nice is None else ["nice", "-n", str(nice)]


def strip_cgroup_flags(argv: Sequence[str]) -> list[str]:
    """``argv`` without the flags that need a cgroup controller (:data:`_CGROUP_FLAGS`) + values.

    The degradation path for a Docker daemon that refuses them outright — e.g. a nested daemon
    whose cgroup is in threaded mode answers any of them with ``unable to apply cgroup
    configuration``. ``--oom-score-adj`` is deliberately **kept**: it needs no controller, works on
    such hosts, and is the flag that protects the host's memory."""
    kept: list[str] = []
    skip_value = False
    for arg in argv:
        if skip_value:
            skip_value = False
            continue
        if arg in _CGROUP_FLAGS:
            skip_value = True
            continue
        kept.append(arg)
    return kept

"""Read a task's claude session transcripts out of its per-task config volume — host-side, so the
profiler (:mod:`panopticon.profiler`) can gap-analyze a task's history **after** its container is
long gone, which is exactly the retroactive case the profiler exists for.

The config volume (``panopticon-config-<task_id>``, :data:`~panopticon.sessionservice.local_runner.CONFIG_MOUNT`)
persists across respawn/recreate and is never explicitly removed (see ``LocalRunner``), so a
completed task's transcripts are still sitting in it on whichever host ran the task. There's no
bind mount to them, so we shell out to ``docker`` (reusing the already-built ``panopticon-base``
image — no new image dependency) to list and read the files, behind an injectable command-runner
(the same convention as :mod:`~panopticon.sessionservice.local_runner` / ``GitClones``) so this is
unit-testable without a daemon; ``tests/sessionservice/test_transcripts.py`` also has a
``skipif``-gated integration test against a real volume.

**Scope: local runner only.** This assumes the caller runs on the same docker host that ran the
task. A task run on a remote runner host (``TaskOut.runner_host``) isn't reachable from here — the
ssh-wrap pattern ``terminal/attach.py::attach_command`` already uses is the natural follow-up if
that's ever needed.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from panopticon.sessionservice.local_runner import (
    CONFIG_MOUNT,
    DEFAULT_IMAGE,
    CommandRunner,
    _subprocess_run,
)

#: Where claude's transcripts live inside the config volume. The project directory is always this
#: fixed path: `container/agent.py`'s `_claude_argv` keys it off `Path.cwd()`, which is always
#: `/workspace` (ADR 0011's `WORKSPACE_MOUNT`), encoded as `-workspace`.
_TRANSCRIPT_DIR = f"{CONFIG_MOUNT}/projects/-workspace"


def _volume_name(task_id: str) -> str:
    return f"panopticon-config-{task_id}"


def task_session_paths(
    task_id: str,
    *,
    dest: Path | str | None = None,
    run: CommandRunner = _subprocess_run,
    image: str = DEFAULT_IMAGE,
) -> list[Path]:
    """Extract task ``task_id``'s session transcript files into a local directory; return their
    paths (unsorted by design — :func:`panopticon.profiler.parse.profile_transcripts` sorts them
    itself by each session's own first-record timestamp).

    ``dest`` is the directory to extract into (a fresh ``tempfile.mkdtemp()`` by default — the
    caller owns cleaning it up). Returns ``[]``, **never raises**, when the task has no config
    volume (never spawned, or a ``runner_type="shell"`` workflow that never ran a container) or no
    transcripts yet: docker/tar trouble is swallowed rather than propagated, so a caller profiling
    many tasks (``--all-tasks``) can just skip the empty ones.

    Checks the volume exists (``docker volume inspect``) **before** doing anything else — a bare
    ``docker run --volume <missing-name>:...`` silently *creates* an empty named volume as a side
    effect, which would otherwise litter the host with junk volumes every time a never-spawned or
    typo'd task id is profiled.

    ``image`` overrides the reader image (default: the already-built ``panopticon-base`` — no new
    pull needed on any host that's spawned a task); tests point it at a minimal throwaway image.
    """
    volume = _volume_name(task_id)
    try:
        run(["docker", "volume", "inspect", volume], check=True)
    except (OSError, subprocess.CalledProcessError):
        return []  # no such volume (or no docker at all) — nothing to profile

    dest_dir = (
        Path(dest) if dest is not None else Path(tempfile.mkdtemp(prefix="panopticon-profile-"))
    )
    dest_dir.mkdir(parents=True, exist_ok=True)

    def _reader(*args: str) -> str:
        return run(
            [
                "docker",
                "run",
                "--rm",
                "--volume",
                f"{volume}:{CONFIG_MOUNT}:ro",
                image,
                *args,
            ],
            check=False,  # tolerate "no such directory" (an existing-but-empty volume) — empty stdout
        )

    # `find`'s predicates have no long-option form (`-maxdepth`/`-name` are the only spelling; see
    # AGENTS.md's shelling-out rule), unlike the double-dash flags used elsewhere in this module.
    listing = _reader("find", _TRANSCRIPT_DIR, "-maxdepth", "1", "-name", "*.jsonl")
    names = sorted({Path(line).name for line in listing.splitlines() if line.strip()})

    paths = []
    for name in names:
        content = _reader("cat", f"{_TRANSCRIPT_DIR}/{name}")
        out_path = dest_dir / name
        out_path.write_text(content)
        paths.append(out_path)
    return paths

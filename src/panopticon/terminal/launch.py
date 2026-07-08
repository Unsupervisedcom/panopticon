"""Command construction for ``panopticon start-runner``.

Pure functions — no I/O except via the injectable ``run`` callable.  All logic
is unit-testable without SSH, Docker, or a live task service.
"""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Callable
from urllib.parse import urlparse


DEFAULT_IMAGE = "panopticon-base"
DEFAULT_TASKS_ROOT = "~/.panopticon/tasks"
DEFAULT_CACHE_ROOT = "~/.panopticon/cache"
DEFAULT_PYTHON = "python3"


def _port_from_url(url: str) -> int:
    parsed = urlparse(url)
    if parsed.port is not None:
        return parsed.port
    return 443 if parsed.scheme == "https" else 80


def build_ssh_command(
    host: str,
    remote_cmd: list[str],
    *,
    reverse_port: int | None = None,
    local_port: int | None = None,
) -> list[str]:
    """Build the ``ssh`` argv.

    Tunnel: ``['ssh', '-R', 'localhost:<rport>:localhost:<lport>',
    '-o', 'ExitOnForwardFailure=yes', host, *remote_cmd]``

    No tunnel (both ports ``None``): ``['ssh', host, *remote_cmd]``
    """
    if reverse_port is not None and local_port is not None:
        return [
            "ssh",
            "-R", f"localhost:{reverse_port}:localhost:{local_port}",
            "-o", "ExitOnForwardFailure=yes",
            host,
            *remote_cmd,
        ]
    return ["ssh", host, *remote_cmd]


def build_remote_host_command(
    *,
    service_url: str,
    container_service_url: str,
    runner_id: str,
    host: str,
    image: str = DEFAULT_IMAGE,
    tasks_root: str = DEFAULT_TASKS_ROOT,
    cache_root: str = DEFAULT_CACHE_ROOT,
    python: str = DEFAULT_PYTHON,
) -> list[str]:
    """Build the ``<python> -m panopticon.sessionservice.host …`` argv for the remote host.

    ``python`` may be a multi-word string (e.g. ``"uv run python"``); it is split
    with :func:`shlex.split` before being prepended to the argv.
    """
    return [
        *shlex.split(python), "-m", "panopticon.sessionservice.host",
        "--service-url", service_url,
        "--container-service-url", container_service_url,
        "--runner-id", runner_id,
        "--host", host,
        "--image", image,
        "--tasks-root", tasks_root,
        "--cache-root", cache_root,
    ]


def start_runner(
    host: str,
    *,
    local_service_url: str = "http://localhost:8000",
    remote_port: int | None = None,
    runner_id: str | None = None,
    container_service_url: str | None = None,
    tunnel: bool = True,
    image: str = DEFAULT_IMAGE,
    tasks_root: str = DEFAULT_TASKS_ROOT,
    cache_root: str = DEFAULT_CACHE_ROOT,
    python: str = DEFAULT_PYTHON,
    run: Callable[[list[str]], None] = subprocess.run,  # type: ignore[assignment]
) -> None:
    """Wire and invoke (or run) the SSH command for a remote session-service runner.

    Tunnel mode (``tunnel=True``): opens a reverse port forward so the remote
    runner's containers can reach the local task service via
    ``host.docker.internal:<port>``.  Requires ``GatewayPorts clientspecified``
    (or ``yes``) in the remote ``sshd_config``.

    Direct mode (``tunnel=False``): no port forward; ``local_service_url`` and
    ``container_service_url`` must be routable from the remote host.
    """
    effective_runner_id = runner_id or host
    local_port = _port_from_url(local_service_url)
    effective_remote_port = remote_port if remote_port is not None else local_port

    if tunnel:
        effective_service_url = f"http://localhost:{effective_remote_port}"
        effective_container_url = (
            container_service_url
            or f"http://host.docker.internal:{effective_remote_port}"
        )
        remote_cmd = build_remote_host_command(
            service_url=effective_service_url,
            container_service_url=effective_container_url,
            runner_id=effective_runner_id,
            host=host,
            image=image,
            tasks_root=tasks_root,
            cache_root=cache_root,
            python=python,
        )
        cmd = build_ssh_command(
            host,
            remote_cmd,
            reverse_port=effective_remote_port,
            local_port=local_port,
        )
    else:
        effective_container_url = container_service_url or local_service_url
        remote_cmd = build_remote_host_command(
            service_url=local_service_url,
            container_service_url=effective_container_url,
            runner_id=effective_runner_id,
            host=host,
            image=image,
            tasks_root=tasks_root,
            cache_root=cache_root,
            python=python,
        )
        cmd = build_ssh_command(host, remote_cmd)

    run(cmd)

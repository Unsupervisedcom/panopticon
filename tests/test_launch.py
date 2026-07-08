"""Unit tests for panopticon.terminal.launch — command construction for start-runner."""

from __future__ import annotations

from panopticon.terminal.launch import (
    _port_from_url,
    build_remote_host_command,
    build_ssh_command,
    start_runner,
)


# ---------------------------------------------------------------------------
# _port_from_url
# ---------------------------------------------------------------------------


def test_port_from_url_explicit() -> None:
    assert _port_from_url("http://localhost:8000") == 8000


def test_port_from_url_explicit_nonstandard() -> None:
    assert _port_from_url("http://10.0.1.5:9001") == 9001


def test_port_from_url_http_default() -> None:
    assert _port_from_url("http://example.com") == 80


def test_port_from_url_https_default() -> None:
    assert _port_from_url("https://example.com") == 443


# ---------------------------------------------------------------------------
# build_ssh_command
# ---------------------------------------------------------------------------


def test_build_ssh_command_tunnel() -> None:
    cmd = build_ssh_command(
        "myhost",
        ["echo", "hi"],
        reverse_port=8001,
        local_port=8000,
    )
    assert cmd == [
        "ssh",
        "-R", "localhost:8001:localhost:8000",
        "-o", "ExitOnForwardFailure=yes",
        "myhost",
        "echo", "hi",
    ]


def test_build_ssh_command_no_tunnel() -> None:
    cmd = build_ssh_command("myhost", ["echo", "hi"])
    assert cmd == ["ssh", "myhost", "echo", "hi"]


def test_build_ssh_command_same_port() -> None:
    cmd = build_ssh_command("myhost", ["ls"], reverse_port=8000, local_port=8000)
    assert "-R" in cmd
    assert "localhost:8000:localhost:8000" in cmd


# ---------------------------------------------------------------------------
# build_remote_host_command
# ---------------------------------------------------------------------------


def test_build_remote_host_command() -> None:
    cmd = build_remote_host_command(
        service_url="http://localhost:8000",
        container_service_url="http://host.docker.internal:8000",
        runner_id="myhost",
        host="myhost",
        image="panopticon-base",
        tasks_root="~/.panopticon/tasks",
        cache_root="~/.panopticon/cache",
    )
    assert cmd[:3] == ["python", "-m", "panopticon.sessionservice.host"]
    assert "--service-url" in cmd
    assert "http://localhost:8000" in cmd
    assert "--container-service-url" in cmd
    assert "http://host.docker.internal:8000" in cmd
    assert "--runner-id" in cmd
    assert "myhost" in cmd
    assert "--host" in cmd
    assert "--tasks-root" in cmd
    assert "~/.panopticon/tasks" in cmd
    assert "--cache-root" in cmd
    assert "~/.panopticon/cache" in cmd


def test_build_remote_host_command_uses_long_flags() -> None:
    cmd = build_remote_host_command(
        service_url="http://localhost:8000",
        container_service_url="http://host.docker.internal:8000",
        runner_id="myhost",
        host="myhost",
    )
    # python -m is exempt (no long form); all other flags must use double-dash
    flags = [tok for tok in cmd if tok.startswith("-") and tok != "-m"]
    for flag in flags:
        assert flag.startswith("--"), f"short flag found: {flag}"


# ---------------------------------------------------------------------------
# start_runner — tunnel mode
# ---------------------------------------------------------------------------


def test_start_runner_tunnel_mode() -> None:
    received: list[list[str]] = []
    start_runner("myhost", local_service_url="http://localhost:8000", run=received.append)
    assert len(received) == 1
    cmd = received[0]
    assert "ssh" in cmd
    assert "-R" in cmd
    assert "localhost:8000:localhost:8000" in cmd
    assert "-o" in cmd
    assert "ExitOnForwardFailure=yes" in cmd
    assert "myhost" in cmd
    assert "--service-url" in cmd
    assert "http://localhost:8000" in cmd
    assert "--container-service-url" in cmd
    assert "http://host.docker.internal:8000" in cmd
    assert "--runner-id" in cmd
    # runner_id defaults to host
    ri_idx = cmd.index("--runner-id")
    assert cmd[ri_idx + 1] == "myhost"


def test_start_runner_tunnel_mode_runner_id_defaults_to_host() -> None:
    received: list[list[str]] = []
    start_runner("remotebox", local_service_url="http://localhost:8000", run=received.append)
    cmd = received[0]
    ri_idx = cmd.index("--runner-id")
    assert cmd[ri_idx + 1] == "remotebox"


def test_start_runner_tunnel_container_url_defaults_to_docker_internal() -> None:
    received: list[list[str]] = []
    start_runner("myhost", local_service_url="http://localhost:9000", run=received.append)
    cmd = received[0]
    cu_idx = cmd.index("--container-service-url")
    assert cmd[cu_idx + 1] == "http://host.docker.internal:9000"


def test_start_runner_custom_remote_port() -> None:
    received: list[list[str]] = []
    start_runner(
        "myhost",
        local_service_url="http://localhost:8000",
        remote_port=9000,
        run=received.append,
    )
    cmd = received[0]
    assert "localhost:9000:localhost:8000" in cmd
    # The remote runner should connect to localhost:9000
    su_idx = cmd.index("--service-url")
    assert cmd[su_idx + 1] == "http://localhost:9000"
    cu_idx = cmd.index("--container-service-url")
    assert cmd[cu_idx + 1] == "http://host.docker.internal:9000"


def test_start_runner_explicit_container_url() -> None:
    received: list[list[str]] = []
    start_runner(
        "myhost",
        local_service_url="http://localhost:8000",
        container_service_url="http://10.0.1.5:8000",
        run=received.append,
    )
    cmd = received[0]
    cu_idx = cmd.index("--container-service-url")
    assert cmd[cu_idx + 1] == "http://10.0.1.5:8000"


# ---------------------------------------------------------------------------
# start_runner — direct mode
# ---------------------------------------------------------------------------


def test_start_runner_direct_mode() -> None:
    received: list[list[str]] = []
    start_runner(
        "myhost",
        local_service_url="http://10.0.1.5:8000",
        tunnel=False,
        run=received.append,
    )
    cmd = received[0]
    assert "-R" not in cmd
    assert "ExitOnForwardFailure=yes" not in cmd
    assert "ssh" in cmd
    assert "myhost" in cmd
    su_idx = cmd.index("--service-url")
    assert cmd[su_idx + 1] == "http://10.0.1.5:8000"


def test_start_runner_direct_mode_container_url_defaults_to_service_url() -> None:
    received: list[list[str]] = []
    start_runner(
        "myhost",
        local_service_url="http://10.0.1.5:8000",
        tunnel=False,
        run=received.append,
    )
    cmd = received[0]
    cu_idx = cmd.index("--container-service-url")
    assert cmd[cu_idx + 1] == "http://10.0.1.5:8000"


def test_start_runner_direct_mode_explicit_container_url() -> None:
    received: list[list[str]] = []
    start_runner(
        "myhost",
        local_service_url="http://10.0.1.5:8000",
        container_service_url="http://10.0.1.5:9000",
        tunnel=False,
        run=received.append,
    )
    cmd = received[0]
    cu_idx = cmd.index("--container-service-url")
    assert cmd[cu_idx + 1] == "http://10.0.1.5:9000"

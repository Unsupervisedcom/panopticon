"""Resource priority (:mod:`panopticon.sessionservice.priority`): the env → argv layer.

Pure unit tests — the defaults every task container is spawned with, each per-host override, the
off switch that returns the argv to exactly what it was before priority existed, the clamps, and
the cgroup-flag strip the runner degrades through on a daemon that refuses them.
"""

from __future__ import annotations

import logging

import pytest

from panopticon.sessionservice.priority import (
    BLKIO_WEIGHT_VAR,
    CGROUP_PARENT_VAR,
    CPU_SHARES_VAR,
    HOST_NICE_VAR,
    OOM_SCORE_ADJ_VAR,
    container_cgroup_parent,
    container_oom_score_adj,
    container_priority_flags,
    host_nice_prefix,
    strip_cgroup_flags,
)


def test_defaults_are_the_lowest_priority_docker_can_express() -> None:
    # cpu-shares 2 and blkio-weight 10 are docker's floors (cgroup v2 cpu.weight 1); a positive
    # oom-score-adj makes the kernel pick a task container over the operator's own processes.
    assert container_priority_flags({}) == [
        "--cpu-shares",
        "2",
        "--blkio-weight",
        "10",
        "--oom-score-adj",
        "500",
    ]


def test_each_knob_is_overridable_per_host() -> None:
    env = {CPU_SHARES_VAR: "512", BLKIO_WEIGHT_VAR: "250", OOM_SCORE_ADJ_VAR: "100"}
    assert container_priority_flags(env) == [
        "--cpu-shares",
        "512",
        "--blkio-weight",
        "250",
        "--oom-score-adj",
        "100",
    ]


@pytest.mark.parametrize("off", ["off", "OFF", "none", "", "  "])
def test_a_knob_switched_off_drops_its_flag_entirely(off: str) -> None:
    # An operator on a dedicated build host opts out and gets the argv panopticon emitted before
    # this module existed — not a flag with a permissive value.
    flags = container_priority_flags(
        {CPU_SHARES_VAR: off, BLKIO_WEIGHT_VAR: off, OOM_SCORE_ADJ_VAR: off}
    )
    assert flags == []


def test_zero_is_a_value_not_an_off_switch() -> None:
    # 0 is legitimate for these flags (docker's default shares / normal niceness), so it must not
    # be read as "unset" — that would silently ignore a deliberate setting.
    assert "--oom-score-adj" in container_priority_flags({OOM_SCORE_ADJ_VAR: "0"})
    assert host_nice_prefix({HOST_NICE_VAR: "0"}) == ["nice", "-n", "0"]


def test_cgroup_parent_is_opt_in() -> None:
    assert container_cgroup_parent({}) is None
    assert "--cgroup-parent" not in container_priority_flags({})
    flags = container_priority_flags({CGROUP_PARENT_VAR: "panopticon.slice"})
    assert flags[flags.index("--cgroup-parent") + 1] == "panopticon.slice"


def test_values_are_clamped_to_what_docker_and_the_kernel_accept(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        assert container_priority_flags({CPU_SHARES_VAR: "1"})[:2] == ["--cpu-shares", "2"]
        assert container_priority_flags({BLKIO_WEIGHT_VAR: "1"})[2:4] == ["--blkio-weight", "10"]
        assert container_oom_score_adj({OOM_SCORE_ADJ_VAR: "5000"}) == 1000
        assert host_nice_prefix({HOST_NICE_VAR: "40"}) == ["nice", "-n", "19"]
    assert caplog.records  # a clamped value is surfaced, not silently rewritten


def test_a_negative_oom_adjustment_is_refused() -> None:
    # Negative would make the kernel prefer host processes over a task — the opposite of the point
    # (and the pane couldn't set it without CAP_SYS_RESOURCE anyway).
    assert container_oom_score_adj({OOM_SCORE_ADJ_VAR: "-500"}) == 0


def test_an_unparseable_value_falls_back_to_the_default(caplog: pytest.LogCaptureFixture) -> None:
    # A typo in one env var must not take out every spawn on the host.
    with caplog.at_level(logging.WARNING):
        assert container_priority_flags({CPU_SHARES_VAR: "lowest"})[:2] == ["--cpu-shares", "2"]
    assert "lowest" in caplog.text


def test_host_nice_prefix_default_and_off() -> None:
    assert host_nice_prefix({}) == ["nice", "-n", "19"]  # `-n` is short: BSD nice has no long form
    assert host_nice_prefix({HOST_NICE_VAR: "off"}) == []


def test_strip_cgroup_flags_drops_the_controller_backed_flags_and_keeps_the_rest() -> None:
    argv = [
        "docker",
        "run",
        "--detach",
        "--cpu-shares",
        "2",
        "--blkio-weight",
        "10",
        "--oom-score-adj",
        "500",
        "--cgroup-parent",
        "panopticon.slice",
        "image",
    ]
    # --oom-score-adj survives: it needs no cgroup controller, works on the hosts that refuse the
    # others, and is the flag that protects the host's memory.
    assert strip_cgroup_flags(argv) == [
        "docker",
        "run",
        "--detach",
        "--oom-score-adj",
        "500",
        "image",
    ]


def test_strip_cgroup_flags_is_a_no_op_when_there_is_nothing_to_strip() -> None:
    argv = ["docker", "run", "--detach", "--oom-score-adj", "500", "image"]
    assert strip_cgroup_flags(argv) == argv

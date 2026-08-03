"""KubernetesRunner: the emitted kubectl argv and the Job manifest. No cluster — the command runner
is a fake that records calls and replays canned stdout. LLM-free (the agent runs in the pod)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import pytest

from panopticon.core.models import LifecyclePhase
from panopticon.sessionservice.agent_operator import UnknownAgent
from panopticon.sessionservice.kubernetes_runner import KubernetesRunner
from panopticon.sessionservice.runner import Runner

AGENT_CR = json.dumps(
    {
        "metadata": {"name": "researcher"},
        "spec": {
            "memberships": [{"organization": "unsupervised"}],
            "profile": {"agent": "wiki-maintainer", "harness": "pi"},
            "credentials": [
                {"secret": "claude-oauth", "as": "env"},
                {"secret": "gh-token", "as": "volume"},
            ],
        },
        "status": {"namespace": "agent-researcher"},
    }
)


class _Cluster:
    """A fake kubectl: records every call, and answers per command verb."""

    def __init__(self, *, agent: str = AGENT_CR, pod_phase: str = "") -> None:
        self.calls: list[list[str]] = []
        self.manifests: list[dict[str, Any]] = []
        self._agent = agent
        self._pod_phase = pod_phase

    def __call__(self, args: Sequence[str], *, check: bool = True, stdin: str | None = None) -> str:
        self.calls.append(list(args))
        if stdin is not None:
            self.manifests.append(json.loads(stdin))
        if "agents.link.aioutfitter.com" in args:
            return self._agent
        if "pod" in args:
            return self._pod_phase
        return ""

    def argv(self, *verbs: str) -> list[list[str]]:
        """Every recorded call whose argv contains all of ``verbs``."""
        return [call for call in self.calls if all(verb in call for verb in verbs)]


def _spawn(cluster: _Cluster, **kwargs: Any) -> dict[str, Any]:
    """Spawn one task and return the Job manifest that was applied."""
    runner = KubernetesRunner("http://control-plane:8000", image="panopticon-base", run=cluster)
    runner.spawn("t1", operator_agent="researcher", **kwargs)
    return cluster.manifests[-1]


def test_kubernetes_runner_is_a_runner() -> None:
    assert issubclass(KubernetesRunner, Runner)


def test_spawn_applies_a_job_named_for_the_task_in_the_agents_namespace() -> None:
    cluster = _Cluster()
    runner = KubernetesRunner("http://control-plane:8000", run=cluster)

    job = runner.spawn("t1", operator_agent="researcher")

    assert job == "panopticon-t1"  # the shared session-name convention, so a respawn replaces
    manifest = cluster.manifests[-1]
    assert manifest["kind"] == "Job"
    assert manifest["metadata"] == {
        "name": "panopticon-t1",
        "namespace": "agent-researcher",
        "labels": {
            "panopticon.task": "t1",
            "link.aioutfitter.com/agent": "researcher",
            "app.kubernetes.io/managed-by": "panopticon",
        },
    }
    apply = cluster.argv("apply")[-1]
    assert apply == [
        "kubectl",
        "--namespace",
        "agent-researcher",
        "apply",
        "--filename",
        "-",
    ]


def test_spawn_deletes_a_stale_job_first_so_a_respawn_replaces_it() -> None:
    """A Job's pod template is immutable, so applying over a changed one is rejected outright."""
    cluster = _Cluster()
    KubernetesRunner("http://control-plane:8000", run=cluster).spawn(
        "t1", operator_agent="researcher"
    )

    delete, apply = cluster.argv("delete")[-1], cluster.argv("apply")[-1]
    assert "--ignore-not-found" in delete and "panopticon-t1" in delete
    assert cluster.calls.index(delete) < cluster.calls.index(apply)


def test_the_pod_runs_as_the_agent_and_reaches_the_control_plane_outside_the_cluster() -> None:
    manifest = _spawn(_Cluster(), git_url="https://forge/repo.git")
    container = manifest["spec"]["template"]["spec"]["containers"][0]
    env = {item["name"]: item["value"] for item in container["env"]}

    assert manifest["spec"]["template"]["spec"]["serviceAccountName"] == "agent-runtime"
    assert env["PANOPTICON_SERVICE_URL"] == "http://control-plane:8000"
    assert env["PANOPTICON_GIT_URL"] == "https://forge/repo.git"  # the pod clones it itself
    # the identity variables the operator sets on the agent's own Deployment
    assert env["LINK_AGENT"] == "researcher"
    assert env["LINK_AGENT_SLUG"] == "wiki-maintainer"
    assert env["LINK_ORGANIZATION"] == "unsupervised"


def test_the_pod_gets_the_agents_credentials_the_way_the_operator_projects_them() -> None:
    manifest = _spawn(_Cluster())
    pod = manifest["spec"]["template"]["spec"]
    container = pod["containers"][0]

    assert container["envFrom"] == [{"secretRef": {"name": "claude-oauth"}}]
    assert {"name": "secret-gh-token", "secret": {"secretName": "gh-token"}} in pod["volumes"]
    assert {
        "name": "secret-gh-token",
        "mountPath": "/var/run/link/credentials/secrets/gh-token",
        "readOnly": True,
    } in container["volumeMounts"]


def test_the_workspace_is_an_emptydir_because_the_agents_pvc_is_already_mounted() -> None:
    """``agent-workspace`` is ReadWriteOnce and held by the agent's always-on Deployment, so a task
    pod that tried to mount it would not schedule."""
    manifest = _spawn(_Cluster())
    pod = manifest["spec"]["template"]["spec"]

    assert {"name": "workspace", "emptyDir": {}} in pod["volumes"]
    assert {"name": "workspace", "mountPath": "/workspace"} in pod["containers"][0]["volumeMounts"]


def test_the_pod_runs_unprivileged_as_the_images_baked_user() -> None:
    """The bootstrap command replaces the image's entrypoint, which is what would otherwise drop
    from root — so the pod has to say who it runs as."""
    pod = _spawn(_Cluster())["spec"]["template"]["spec"]

    assert pod["securityContext"] == {
        "runAsNonRoot": True,
        "runAsUser": 1000,
        "runAsGroup": 1000,
        "fsGroup": 1000,
    }
    assert pod["containers"][0]["command"] == ["python", "-m", "panopticon.container.pod"]


def test_the_job_never_retries_because_respawn_is_the_daemons_job() -> None:
    spec = _spawn(_Cluster())["spec"]

    assert spec["backoffLimit"] == 0
    assert spec["ttlSecondsAfterFinished"] == 3600  # a finished Job must not sit in the quota
    assert spec["template"]["spec"]["restartPolicy"] == "Never"


def test_the_agent_prompt_and_model_reach_the_pod_as_on_the_docker_path() -> None:
    manifest = _spawn(
        _Cluster(), initial_prompt="fix the flake", turn="agent", starting_model="opus"
    )
    env = {
        i["name"]: i["value"] for i in manifest["spec"]["template"]["spec"]["containers"][0]["env"]
    }

    assert env["PANOPTICON_INITIAL_PROMPT"] == "fix the flake"
    assert env["PANOPTICON_TASK_TURN"] == "agent"
    assert env["PANOPTICON_STARTING_MODEL"] == "opus"


def test_a_composed_image_overrides_the_configured_default() -> None:
    manifest = _spawn(_Cluster(), image="panopticon-spike-repo1")
    assert (
        manifest["spec"]["template"]["spec"]["containers"][0]["image"] == "panopticon-spike-repo1"
    )


def test_spawn_reports_starting_then_awaiting() -> None:
    phases: list[LifecyclePhase] = []
    KubernetesRunner("http://control-plane:8000", run=_Cluster()).spawn(
        "t1", operator_agent="researcher", progress=phases.append
    )
    assert phases == [LifecyclePhase.STARTING, LifecyclePhase.AWAITING]


def test_spawn_without_an_agent_fails_rather_than_choosing_one() -> None:
    with pytest.raises(ValueError, match="needs an operator_agent"):
        KubernetesRunner("http://control-plane:8000", run=_Cluster()).spawn("t1")


def test_an_unknown_agent_surfaces_as_a_failed_spawn() -> None:
    with pytest.raises(UnknownAgent):
        KubernetesRunner("http://control-plane:8000", run=_Cluster(agent="")).spawn(
            "t1", operator_agent="ghost"
        )


def test_the_agent_is_read_once_and_cached_across_spawns() -> None:
    cluster = _Cluster()
    runner = KubernetesRunner("http://control-plane:8000", run=cluster)

    runner.spawn("t1", operator_agent="researcher")
    runner.spawn("t2", operator_agent="researcher")

    assert len(cluster.argv("agents.link.aioutfitter.com")) == 1


def test_is_running_reads_the_pod_not_the_job() -> None:
    """A Job outlives its pod: it stays present when the pod fails or completes, so a Job-level
    check would report a dead task as up and defeat the daemon's down-detection."""
    cluster = _Cluster(pod_phase="Running")
    runner = KubernetesRunner("http://control-plane:8000", run=cluster)
    runner.spawn("t1", operator_agent="researcher")

    assert runner.is_running("t1") is True
    probe = cluster.argv("get", "pod")[-1]
    assert "--selector" in probe and "panopticon.task=t1" in probe


def test_a_succeeded_pod_is_not_running() -> None:
    cluster = _Cluster(pod_phase="Succeeded")
    runner = KubernetesRunner("http://control-plane:8000", run=cluster)
    runner.spawn("t1", operator_agent="researcher")

    assert runner.is_running("t1") is False


def test_has_session_tracks_the_pod_because_the_session_lives_in_it() -> None:
    """The tmux session is inside the pod, so there is no host-side session to lose while the task
    keeps running — the orphan case the local runner heals cannot happen here."""
    cluster = _Cluster(pod_phase="Running")
    runner = KubernetesRunner("http://control-plane:8000", run=cluster)
    runner.spawn("t1", operator_agent="researcher")

    assert runner.has_session("t1") == runner.is_running("t1")


def test_stop_deletes_the_job_and_only_the_job() -> None:
    cluster = _Cluster()
    runner = KubernetesRunner("http://control-plane:8000", run=cluster)
    runner.spawn("t1", operator_agent="researcher")
    cluster.calls.clear()

    runner.stop("panopticon-t1")

    deletes = cluster.argv("delete")
    assert deletes and all("job" in call for call in deletes)  # never the namespace itself
    assert deletes[-1][-3:] == ["panopticon-t1", "--ignore-not-found", "--wait=false"]


def test_attach_command_execs_into_the_pods_tmux_session() -> None:
    cluster = _Cluster()
    runner = KubernetesRunner("http://control-plane:8000", run=cluster)
    runner.spawn("t1", operator_agent="researcher")

    assert runner.attach_command("t1")[-5:] == [
        "--",
        "tmux",
        "attach",
        "-t",
        "panopticon-t1",
    ]

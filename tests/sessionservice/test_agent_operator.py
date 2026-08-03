"""Resolving an agent-operator ``Agent``: the ``kubectl get`` argv, and the reduction of the CR to
what a task Job needs. No cluster — the command runner is a fake that replays a canned CR."""

from __future__ import annotations

import json
from collections.abc import Sequence

import pytest

from panopticon.sessionservice.agent_operator import (
    AGENT_RESOURCE,
    Credential,
    UnknownAgent,
    resolve_agent,
)


class _Recorder:
    """An injectable kubectl runner that records argv and replays one canned stdout."""

    def __init__(self, stdout: str = "") -> None:
        self.calls: list[list[str]] = []
        self._stdout = stdout

    def __call__(self, args: Sequence[str], *, check: bool = True, stdin: str | None = None) -> str:
        self.calls.append(list(args))
        return self._stdout


def _agent(**overrides: object) -> str:
    """A reconciled Agent CR, shaped as agent-operator writes it."""
    agent = {
        "apiVersion": "link.aioutfitter.com/v1alpha1",
        "kind": "Agent",
        "metadata": {"name": "researcher"},
        "spec": {
            "memberships": [{"organization": "unsupervised"}],
            "profile": {"agent": "wiki-maintainer", "harness": "pi"},
            "credentials": [
                {"secret": "claude-oauth", "as": "env"},
                {"configMap": "gitconfig", "as": "volume"},
            ],
        },
        "status": {"namespace": "agent-researcher"},
    }
    agent.update(overrides)  # type: ignore[arg-type]
    return json.dumps(agent)


def test_resolve_reads_the_cluster_scoped_agent_with_the_configured_context() -> None:
    rec = _Recorder(_agent())

    resolve_agent("researcher", run=rec, context="microvm")

    # cluster-scoped: no --namespace, and the context is never left to whatever kubectl defaults to
    assert rec.calls == [
        [
            "kubectl",
            "--context",
            "microvm",
            "get",
            AGENT_RESOURCE,
            "researcher",
            "--output",
            "json",
        ]
    ]


def test_resolve_reduces_the_cr_to_the_job_relevant_fields() -> None:
    workspace = resolve_agent("researcher", run=_Recorder(_agent()))

    assert workspace.namespace == "agent-researcher"  # the operator's, not a guessed agent-<name>
    assert workspace.organization == "unsupervised"
    assert (workspace.agent_slug, workspace.harness) == ("wiki-maintainer", "pi")
    assert workspace.credentials == (
        Credential(kind="Secret", name="claude-oauth", exposure="env"),
        Credential(kind="ConfigMap", name="gitconfig", exposure="volume"),
    )


def test_resolve_labels_mark_a_created_resource_as_the_agents_and_panopticons() -> None:
    workspace = resolve_agent("researcher", run=_Recorder(_agent()))

    assert workspace.labels == {
        "link.aioutfitter.com/agent": "researcher",
        "app.kubernetes.io/managed-by": "panopticon",
    }


def test_a_missing_agent_is_an_unknown_agent_not_an_empty_workspace() -> None:
    with pytest.raises(UnknownAgent, match="no agent-operator Agent named 'ghost'"):
        resolve_agent("ghost", run=_Recorder(""))


def test_an_unreconciled_agent_is_refused_rather_than_guessed() -> None:
    """``agent-<name>`` is the operator's convention, but until it writes ``status.namespace`` the
    namespace may not exist (or may be another Agent's), so spawning into a guess would race it."""
    with pytest.raises(UnknownAgent, match=r"has no status\.namespace"):
        resolve_agent("researcher", run=_Recorder(_agent(status={})))


def test_credential_mount_paths_and_volume_names_match_the_operators() -> None:
    secret = Credential(kind="Secret", name="claude-oauth", exposure="volume")
    config = Credential(kind="ConfigMap", name="gitconfig", exposure="volume")

    assert secret.volume_name == "secret-claude-oauth"
    assert secret.mount_path == "/var/run/link/credentials/secrets/claude-oauth"
    assert config.volume_name == "config-gitconfig"
    assert config.mount_path == "/var/run/link/credentials/configmaps/gitconfig"


def test_a_long_credential_name_is_truncated_to_a_legal_volume_name() -> None:
    """Kubernetes caps a volume name at 63 characters, and a trailing dash is illegal — the same
    truncation the operator applies, so both projections name the same volume."""
    name = "x" * 70
    assert len(Credential(kind="Secret", name=name, exposure="volume").volume_name) == 63

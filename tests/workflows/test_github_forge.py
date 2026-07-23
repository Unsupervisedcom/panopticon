"""The tarot review-artifact responsibility shared by the GitHub-forge workflows.

`GithubForgeWorkflow.responsibilities()` conditionally adds `TAROT_REVIEW_ARTIFACTS` to
ITERATING for a repo that opts in (`Repo.capabilities["tarot_review"]`) — verified here on both
concrete subclasses (`GithubPeerReviewed`, `GithubSelfReviewed`) since the override lives on the
shared base. The real verification (running `tarot strands check` / `tarot tour check` and
blocking `advance` on failure) is the in-container `tarot_gate` hook
(`tests/container/test_tarot_gate.py`), not this responsibility-declaration layer.
"""

from __future__ import annotations

from panopticon.core.models import Repo, Status
from panopticon.workflows import GithubPeerReviewed, GithubSelfReviewed
from panopticon.workflows.github_forge import TAROT_REVIEW_CAPABILITY, GithubForgeWorkflow


def _repo(**capabilities: object) -> Repo:
    return Repo(id="r1", name="r1", git_url="git@example.com:r1.git", capabilities=capabilities)


def test_tarot_responsibility_absent_with_no_repo() -> None:
    wf = GithubPeerReviewed()
    assert "tarot-review-artifacts" not in {r.key for r in wf.responsibilities("ITERATING")}


def test_tarot_responsibility_absent_for_a_non_opted_in_repo() -> None:
    wf = GithubPeerReviewed()
    keys = {r.key for r in wf.responsibilities("ITERATING", repo=_repo())}
    assert "tarot-review-artifacts" not in keys


def test_tarot_responsibility_present_for_an_opted_in_repo() -> None:
    wf = GithubPeerReviewed()
    repo = _repo(**{TAROT_REVIEW_CAPABILITY: True})
    responsibilities = list(wf.responsibilities("ITERATING", repo=repo))
    assert "tarot-review-artifacts" in {r.key for r in responsibilities}
    tarot = next(r for r in responsibilities if r.key == "tarot-review-artifacts")
    assert "tarot strands check" in tarot.description
    assert "tarot tour check" in tarot.description
    # every other ITERATING responsibility is untouched
    assert {"plan-implemented", "tests-pass", "url-recorded"} <= {r.key for r in responsibilities}


def test_tarot_responsibility_only_applies_to_iterating() -> None:
    wf = GithubPeerReviewed()
    repo = _repo(**{TAROT_REVIEW_CAPABILITY: True})
    assert "tarot-review-artifacts" not in {
        r.key for r in wf.responsibilities("PLANNING", repo=repo)
    }
    assert "tarot-review-artifacts" not in {r.key for r in wf.responsibilities("REVIEW", repo=repo)}
    assert "tarot-review-artifacts" not in {
        r.key for r in wf.responsibilities("MERGING", repo=repo)
    }


def test_shared_across_both_forge_workflows() -> None:
    repo = _repo(**{TAROT_REVIEW_CAPABILITY: True})
    for wf in (GithubPeerReviewed(), GithubSelfReviewed()):
        assert "tarot-review-artifacts" in {
            r.key for r in wf.responsibilities("ITERATING", repo=repo)
        }


def test_seeded_through_a_real_transition_only_when_opted_in() -> None:
    wf = GithubPeerReviewed()
    task = wf.start_task("t1", "r1", at="t0")
    for r in list(task.outstanding_responsibilities):
        task.resolve_responsibility(key=r.key, status=Status.MET)
    wf.apply_transition(task, "ITERATING", at="t1", repo=_repo(**{TAROT_REVIEW_CAPABILITY: True}))
    assert "tarot-review-artifacts" in {r.key for r in task.current_entry.responsibilities}

    other = wf.start_task("t2", "r1", at="t0")
    for r in list(other.outstanding_responsibilities):
        other.resolve_responsibility(key=r.key, status=Status.MET)
    wf.apply_transition(other, "ITERATING", at="t1", repo=_repo())
    assert "tarot-review-artifacts" not in {r.key for r in other.current_entry.responsibilities}


def test_tarot_review_capability_constant_matches_docker_in_docker_style() -> None:
    # Same "per-repo opt-in map" mechanism as `docker_in_docker` — a plain capabilities key.
    assert TAROT_REVIEW_CAPABILITY == "tarot_review"
    assert GithubForgeWorkflow.TAROT_REVIEW_ARTIFACTS.key == "tarot-review-artifacts"

"""The tarot adapter: argv, exit-code handling, binary resolution, and the base-ref ladder.

Pins the *spelling* of every tarot invocation panopticon makes, with a fake command-runner — no
real `tarot` needed (the same style as `tests/core/test_git.py`). The policy that uses these lives
in `tests/taskservice/test_tarot_gate.py`.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from panopticon.core import tarot
from panopticon.core.tarot import CommandResult, TarotCLI

CLONE = "/tasks/t1"


class FakeRun:
    """Records every argv it's asked to run; returns a canned result per matched prefix."""

    def __init__(self, responses: dict[tuple[str, ...], CommandResult] | None = None) -> None:
        self._responses = responses or {}
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, args: Sequence[str], *, cwd: str | None = None) -> CommandResult:
        argv = tuple(args)
        self.calls.append(argv)
        for prefix, result in self._responses.items():
            if argv[: len(prefix)] == prefix:
                return result
        return CommandResult(returncode=0, output="")


def cli(run: FakeRun) -> TarotCLI:
    return TarotCLI(run=run, tarot_binary="tarot")


# -- binary resolution ------------------------------------------------------------


def test_binary_prefers_the_explicit_override(monkeypatch) -> None:
    monkeypatch.setenv(tarot.BINARY_ENV_VAR, "/env/tarot")
    assert tarot.binary("/explicit/tarot") == "/explicit/tarot"


def test_binary_falls_back_to_the_env_var_then_path(monkeypatch) -> None:
    monkeypatch.setenv(tarot.BINARY_ENV_VAR, "/env/tarot")
    assert tarot.binary() == "/env/tarot"
    monkeypatch.delenv(tarot.BINARY_ENV_VAR)
    monkeypatch.setattr(tarot.shutil, "which", lambda name: f"/path/{name}")
    assert tarot.binary() == "/path/tarot"


def test_binary_is_none_when_tarot_is_absent(monkeypatch) -> None:
    """Absence is a value, not an exception — every caller has to refuse honestly."""
    monkeypatch.delenv(tarot.BINARY_ENV_VAR, raising=False)
    monkeypatch.setattr(tarot.shutil, "which", lambda name: None)
    assert tarot.binary() is None
    assert TarotCLI(run=FakeRun()).available is False


# -- the base-ref ladder ----------------------------------------------------------


def test_base_args_defers_to_a_clone_that_pins_its_own_base() -> None:
    assert tarot.base_args("some-ref", "main") == []


def test_base_args_uses_the_repo_default_otherwise() -> None:
    assert tarot.base_args(None, "dimitri/pending-fixes") == [
        "--base",
        "origin/dimitri/pending-fixes",
    ]


def test_resolve_base_args_reads_the_clones_git_config() -> None:
    run = FakeRun({("git", "-C", CLONE, "config"): CommandResult(0, "some-ref\n")})
    assert cli(run).resolve_base_args(CLONE, "main") == []
    assert run.calls[0] == ("git", "-C", CLONE, "config", "--get", "tarot.base")


def test_resolve_base_args_when_the_config_is_unset() -> None:
    # `git config --get` exits 1 with no output when the key is absent.
    run = FakeRun({("git",): CommandResult(1, "")})
    assert cli(run).resolve_base_args(CLONE, "main") == ["--base", "origin/main"]


# -- checks -----------------------------------------------------------------------


def test_check_runs_both_subcommands_with_directory_and_base() -> None:
    run = FakeRun()
    assert cli(run).check(CLONE, base_args=["--base", "origin/main"]).ok
    assert run.calls == [
        ("tarot", "strands", "check", "--directory", CLONE, "--base", "origin/main"),
        ("tarot", "tour", "check", "--directory", CLONE, "--base", "origin/main"),
    ]


def test_check_stops_at_the_first_failure_and_returns_its_output() -> None:
    run = FakeRun({("tarot", "strands"): CommandResult(1, "a.py:f: not claimed by any strand")})
    outcome = cli(run).check(CLONE, base_args=[])
    assert not outcome.ok
    assert outcome.output == "a.py:f: not claimed by any strand"
    assert len(run.calls) == 1  # nothing to gain running `tour check` too


def test_check_reports_a_structural_failure_the_same_way() -> None:
    """Exit 2 (no seed file, malformed JSON) is a failure like exit 1 — both refuse."""
    run = FakeRun({("tarot", "strands"): CommandResult(2, "tarot: no seed at .tarot/strands.json")})
    outcome = cli(run).check(CLONE, base_args=[])
    assert not outcome.ok and not outcome.missing_binary
    assert "no seed" in outcome.output


def test_check_short_circuits_when_no_binary_resolved(monkeypatch) -> None:
    """With nothing resolved we don't even try to run — no argv, just the honest verdict."""
    monkeypatch.delenv(tarot.BINARY_ENV_VAR, raising=False)
    monkeypatch.setattr(tarot.shutil, "which", lambda name: None)
    run = FakeRun()
    outcome = TarotCLI(run=run).check(CLONE, base_args=[])
    assert outcome.missing_binary and not outcome.ok
    assert run.calls == []


def test_check_flags_a_missing_binary_distinctly() -> None:
    """ "tarot isn't installed" and "the artifacts are wrong" need different messages."""
    run = FakeRun({("tarot",): CommandResult(127, "not found", found=False)})
    outcome = cli(run).check(CLONE, base_args=[])
    assert not outcome.ok and outcome.missing_binary


# -- suggest / scaffold -----------------------------------------------------------


def test_suggest_passes_json_so_it_writes_nothing() -> None:
    run = FakeRun({("tarot",): CommandResult(0, '{"strands": []}')})
    result = cli(run).suggest(CLONE, base_args=["--base", "origin/main"])
    assert result.output == '{"strands": []}'
    assert run.calls == [
        ("tarot", "strands", "suggest", "--json", "--directory", CLONE, "--base", "origin/main")
    ]


def test_scaffold_passes_from_strands_and_the_title() -> None:
    run = FakeRun()
    cli(run).scaffold(CLONE, base_args=[], title="Host-side gate")
    assert run.calls == [
        (
            "tarot",
            "tour",
            "scaffold",
            "--from-strands",
            "--title",
            "Host-side gate",
            "--directory",
            CLONE,
        )
    ]


def test_suggest_and_scaffold_report_a_missing_binary_without_raising(monkeypatch) -> None:
    monkeypatch.delenv(tarot.BINARY_ENV_VAR, raising=False)
    monkeypatch.setattr(tarot.shutil, "which", lambda name: None)
    absent = TarotCLI(run=FakeRun())
    assert absent.suggest(CLONE, base_args=[]).found is False
    assert absent.scaffold(CLONE, base_args=[]).found is False


# -- the packaged authoring skill -------------------------------------------------


def test_authoring_skill_installs_to_a_scratch_dir_and_reads_it_back(tmp_path) -> None:
    written: dict[str, Path] = {}

    def run(args: Sequence[str], *, cwd: str | None = None) -> CommandResult:
        assert list(args[:3]) == ["tarot", "skill", "install"]
        target = Path(args[-1])
        written["target"] = target
        skill = target / tarot.SKILL_RELPATH
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: tarot-authoring\n---\n\n## 1. Suggest a strand seed\nbody\n")
        return CommandResult(0, "added SKILL.md")

    body = TarotCLI(run=run, tarot_binary="tarot").authoring_skill()
    assert body is not None
    assert body.startswith("## 1. Suggest a strand seed")
    assert "name: tarot-authoring" not in body  # tarot's frontmatter is stripped, not nested
    assert not written["target"].exists()  # the scratch dir is cleaned up


def test_authoring_skill_is_none_when_the_install_fails() -> None:
    run = FakeRun({("tarot", "skill"): CommandResult(2, "tarot: boom")})
    assert cli(run).authoring_skill() is None


def test_authoring_skill_is_none_when_the_layout_moved() -> None:
    """A tarot upgrade that relocates the packaged skill must not take the control plane down."""
    run = FakeRun({("tarot", "skill"): CommandResult(0, "added SKILL.md")})  # writes nothing
    assert cli(run).authoring_skill() is None


def test_authoring_skill_caches_a_hit_but_retries_a_miss(tmp_path) -> None:
    """A `None` learned while tarot was absent must not outlive the install."""
    attempts: list[int] = []

    def run(args: Sequence[str], *, cwd: str | None = None) -> CommandResult:
        attempts.append(1)
        if len(attempts) == 1:
            return CommandResult(2, "tarot: boom")
        skill = Path(args[-1]) / tarot.SKILL_RELPATH
        skill.parent.mkdir(parents=True)
        skill.write_text("body")
        return CommandResult(0, "added SKILL.md")

    adapter = TarotCLI(run=run, tarot_binary="tarot")
    assert adapter.authoring_skill() is None
    assert adapter.authoring_skill() == "body"  # retried
    assert adapter.authoring_skill() == "body"
    assert len(attempts) == 2  # …and the hit is cached


def test_binary_re_probes_while_missing_then_caches_the_hit(monkeypatch) -> None:
    """An operator installing tarot in response to the gate's refusal shouldn't need a restart."""
    monkeypatch.delenv(tarot.BINARY_ENV_VAR, raising=False)
    found: list[str | None] = [None, None, "/usr/local/bin/tarot"]
    monkeypatch.setattr(tarot.shutil, "which", lambda name: found.pop(0) if found else None)

    adapter = TarotCLI(run=FakeRun())
    assert adapter.available is False
    assert adapter.available is False
    assert adapter.available is True
    assert found == []  # three probes, then…
    assert adapter.available is True  # …no fourth: the hit is cached


# -- frontmatter ------------------------------------------------------------------


def test_strip_frontmatter_handles_absent_and_unterminated_blocks() -> None:
    assert tarot.strip_frontmatter("no frontmatter here") == "no frontmatter here"
    assert tarot.strip_frontmatter("---\nname: x\n---\nbody") == "body"
    assert tarot.strip_frontmatter("---\nunterminated\n") == "---\nunterminated"

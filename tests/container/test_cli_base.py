"""The agent-CLI adapter seam (ADR 0014): the ABC + the name-keyed registry. A CLI drops in by
implementing :class:`AgentCLI` and registering under its name, with no launcher edit. Shared
base-class behavior (read_hook_payload, resolve_model passthrough) lives here; per-adapter
MODEL_TIERS mapping assertions live in their own modules."""

from __future__ import annotations

import io
import os
from collections.abc import Mapping
from pathlib import Path

import pytest

from panopticon.container.cli import (
    DEFAULT_AGENT_CLI,
    AgentCLI,
    get_agent_cli,
    register_agent_cli,
)
from panopticon.container.cli.base import (
    MAX_RESUME_FALLBACKS,
    RESUME_FAILURE_WINDOW_SECONDS,
    secret_from_env,
    unquote_secret,
)
from panopticon.container.cli.claude import INTERRUPT_PROMPT, ClaudeAgentCLI
from panopticon.container.cli.codex import CodexAgentCLI
from panopticon.core.features import CODEX_FLAG


def test_default_resolves_to_claude() -> None:
    assert DEFAULT_AGENT_CLI == "claude"
    assert isinstance(get_agent_cli(), ClaudeAgentCLI)  # no name → the default
    assert isinstance(get_agent_cli("claude"), ClaudeAgentCLI)


def test_codex_is_a_registered_built_in_adapter(enable_codex: None) -> None:
    # The second built-in CLI: registering it makes it resolvable with no launcher edit (ADR 0014 §2).
    # Behind its feature flag (ADR 0014 §7), so the fixture turns it on.
    assert isinstance(get_agent_cli("codex"), CodexAgentCLI)


def test_codex_is_unavailable_while_its_feature_flag_is_off() -> None:
    # The shipped default: the adapter isn't registered and resolving it says why, naming the flag
    # rather than reading as a typo (ADR 0014 §7).
    with pytest.raises(KeyError, match="PANOPTICON_ENABLE_CODEX"):
        get_agent_cli("codex")


def test_codex_stops_resolving_when_the_flag_goes_off_after_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The registry is process-global, so registration alone can't be the gate: an adapter registered
    # while the flag was on must stop resolving once it's off.
    monkeypatch.setenv(CODEX_FLAG, "1")
    assert isinstance(get_agent_cli("codex"), CodexAgentCLI)
    monkeypatch.delenv(CODEX_FLAG)
    with pytest.raises(KeyError, match="PANOPTICON_ENABLE_CODEX"):
        get_agent_cli("codex")


def test_unknown_cli_name_is_a_clear_error() -> None:
    with pytest.raises(KeyError, match="unknown agent CLI 'nope'"):
        get_agent_cli("nope")


def test_registering_an_adapter_makes_it_resolvable_without_a_launcher_edit() -> None:
    class _Fake(AgentCLI):
        name = "fake-cli"
        config_dirname = ".fake"
        MODEL_TIERS = {}

        def render_skills(self, client: object, task_id: str, home: Path) -> list[Path]:
            return []

        def render_operations(self, client: object, task_id: str, home: Path) -> list[Path]:
            return []

        def write_settings(self, home: Path) -> Path:
            return home

        def write_mcp_config(self, config_dir: Path, service_url: str) -> Path:
            return config_dir

        def write_workflow_overview(self, config_dir: Path, overview: str) -> Path | None:
            return None

        def trust_workspace(self, config_dir: Path, cwd: Path, env: object) -> Path:
            return config_dir

        def auth_missing_detail(self, env: object, config_dir: object) -> str | None:
            return None

        def write_credentials(self, config_dir: Path, env: object) -> Path | None:
            return None

        def has_live_background_task(self, payload: dict[str, object]) -> bool:
            return False

        def launch_argv(
            self,
            config_dir: Path,
            cwd: Path,
            *,
            initial_prompt: str | None = None,
            turn: str | None = None,
            starting_model: str | None = None,
        ) -> list[str]:
            return ["fake-cli"]

        def resume_target(self, config_dir: Path, cwd: Path) -> Path | None:
            return None

    register_agent_cli(_Fake)
    resolved = get_agent_cli("fake-cli")
    assert isinstance(resolved, _Fake) and resolved.config_dirname == ".fake"


# -- shared base-class behaviour ----------------------------------------------------------------


def test_read_hook_payload_tolerates_empty_and_invalid() -> None:
    # Shared implementation on the base — tested once; adapter tests cover only their own seams.
    cli = ClaudeAgentCLI()
    assert cli.read_hook_payload(io.StringIO("")) == {}
    assert cli.read_hook_payload(io.StringIO("not json")) == {}
    assert cli.read_hook_payload(io.StringIO("[]")) == {}  # JSON, but not an object
    assert cli.read_hook_payload(io.StringIO('{"a": 1}')) == {"a": 1}


def test_resolve_model_passes_unknown_tiers_through() -> None:
    # The passthrough fallback lives on the base; adapters supply only their own mapping.
    assert ClaudeAgentCLI().resolve_model("some-raw-model-id") == "some-raw-model-id"


def test_resolve_model_rejects_an_unmapped_reserved_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    # A reserved tier absent from the adapter's MODEL_TIERS must fail loud — the stale-image
    # backstop (resolve_tier raises instead of leaking the raw tier to --model).
    monkeypatch.setattr(ClaudeAgentCLI, "MODEL_TIERS", {})
    with pytest.raises(ValueError, match="primary"):
        ClaudeAgentCLI().resolve_model("primary")


# -- env-file secret normalization --------------------------------------------------------------
#
# `docker run --env-file` does no dotenv parsing: everything after the first `=` is the value,
# quotes and all. The shell runner, which *sources* the same file, strips them. These pin the
# reconciliation (unquote_secret) both runners' consumers now go through.


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("sk-ant-bare", "sk-ant-bare"),  # the correct spelling — untouched
        ('"sk-ant-dq"', "sk-ant-dq"),  # KEY="v"
        ("'sk-ant-sq'", "sk-ant-sq"),  # KEY='v'
        ("  sk-ant-pad  ", "sk-ant-pad"),  # stray whitespace
        ("sk-ant-crlf\r", "sk-ant-crlf"),  # CRLF env-file leaves the \r on the value
        ('"sk-ant-crlf"\r\n', "sk-ant-crlf"),  # ...quoted *and* CRLF
        ('"sk-ant-unbalanced', '"sk-ant-unbalanced'),  # unbalanced → not a legible mistake
        ('sk-ant-unbalanced"', 'sk-ant-unbalanced"'),
        ("'sk-ant-mismatched\"", "'sk-ant-mismatched\""),  # ends must *match*
        ('sk-ant-"inner"-quotes', 'sk-ant-"inner"-quotes'),  # inner quotes preserved
        ("'\"sk-ant-nested\"'", '"sk-ant-nested"'),  # one pair only — no guessing
        ('""', ""),  # a deliberately blank value
        ("   ", ""),
        ("", ""),
        ('"', '"'),  # a lone quote isn't a pair
    ],
)
def test_unquote_secret_normalizes_env_file_values(raw: str, expected: str) -> None:
    assert unquote_secret(raw) == expected


def test_secret_from_env_reads_absent_and_blank_alike() -> None:
    # `KEY=""` arrives as the two *literal* characters `""` — truthy, so a raw presence check reads
    # a deliberately blank credential as present-but-broken. Normalizing reads it as absent.
    assert secret_from_env({}, "KEY") is None
    assert secret_from_env({"KEY": ""}, "KEY") is None
    assert secret_from_env({"KEY": '""'}, "KEY") is None
    assert secret_from_env({"KEY": "  "}, "KEY") is None
    assert secret_from_env({"KEY": '"v"'}, "KEY") == "v"


def test_launch_env_overlays_only_the_vars_that_need_normalizing() -> None:
    cli = ClaudeAgentCLI()
    overlay = cli.launch_env(
        {
            "ANTHROPIC_API_KEY": '"sk-ant-quoted"',
            "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-bare",  # already correct
            "GH_TOKEN": "'ghp_quoted'",  # gh inherits the CLI's env, so it's fixed too
            "PATH": '"/usr/bin"',  # not a secret var — never touched
        }
    )
    assert overlay == {"ANTHROPIC_API_KEY": "sk-ant-quoted", "GH_TOKEN": "ghp_quoted"}


def test_launch_env_is_empty_for_a_correctly_written_env_file() -> None:
    # The no-op case: merging this over the process env changes nothing.
    assert ClaudeAgentCLI().launch_env({"ANTHROPIC_API_KEY": "sk-ant-bare"}) == {}
    assert ClaudeAgentCLI().launch_env({}) == {}


def test_launch_env_defaults_to_normalizing_nothing() -> None:
    # SECRET_ENV_VARS is opt-in: an adapter that declares none inherits a no-op overlay.
    assert AgentCLI.SECRET_ENV_VARS == ()


# -- launch: resume, and the fallback when the CLI refuses it ------------------------------------
#
# The launch *policy* — not the exec. ``run``/``clock`` are injected, so no agent CLI is ever
# started here (AGENTS.md "No LLMs in tests"); the real :func:`_run_process` is the only uncovered
# line. Driven through the claude adapter because its resume semantics are the real ones.


class _FakeLaunch:
    """A stand-in for the CLI exec: records each argv, replays a scripted (exit code, duration)."""

    def __init__(self, *results: tuple[int, float]) -> None:
        self._results = list(results)
        self.argvs: list[list[str]] = []
        self.envs: list[dict[str, str]] = []
        self.now = 0.0

    def run(self, argv: list[str], env: Mapping[str, str]) -> int:
        self.argvs.append(list(argv))
        self.envs.append(dict(env))
        returncode, elapsed = self._results[min(len(self.argvs) - 1, len(self._results) - 1)]
        self.now += elapsed
        return returncode

    def clock(self) -> float:
        return self.now


def _transcript(project: Path, name: str, *, mtime_ns: int) -> Path:
    """Write a transcript into claude's per-project dir and pin its mtime (resume order is mtime)."""
    project.mkdir(parents=True, exist_ok=True)
    path = project / name
    path.write_text('{"type":"mode"}\n')
    os.utime(path, ns=(mtime_ns, mtime_ns))
    return path


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A cwd the adapter's project-dir encoding resolves against, with the launch env cleared."""
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    for var in ("PANOPTICON_INITIAL_PROMPT", "PANOPTICON_TASK_TURN", "PANOPTICON_STARTING_MODEL"):
        monkeypatch.delenv(var, raising=False)
    return cwd


def test_launch_runs_once_when_there_is_nothing_to_resume(tmp_path: Path, workspace: Path) -> None:
    # A first run that fails fast is just a failing CLI — there's no resume to blame or retry.
    fake = _FakeLaunch((1, 0.1))
    ClaudeAgentCLI().launch(tmp_path, run=fake.run, clock=fake.clock)
    assert fake.argvs == [["claude", "--dangerously-skip-permissions"]]


def test_launch_runs_once_when_a_resumed_session_exits_cleanly(
    tmp_path: Path, workspace: Path
) -> None:
    cli = ClaudeAgentCLI()
    transcript = _transcript(cli.project_dir(tmp_path, workspace), "a.jsonl", mtime_ns=1_000)
    fake = _FakeLaunch((0, 0.1))
    cli.launch(tmp_path, run=fake.run, clock=fake.clock)
    assert fake.argvs == [["claude", "--dangerously-skip-permissions", "--continue"]]
    assert transcript.exists()  # a clean exit is never grounds for quarantine


def test_launch_quarantines_and_relaunches_when_a_resume_is_refused(
    tmp_path: Path, workspace: Path
) -> None:
    # The bug: claude refuses the transcript and exits at once, which exits the pane's command and
    # destroys the tmux session. The second launch is what keeps the task startable.
    cli = ClaudeAgentCLI()
    project = cli.project_dir(tmp_path, workspace)
    transcript = _transcript(project, "sdk.jsonl", mtime_ns=1_000)
    fake = _FakeLaunch((1, 0.2), (0, 5.0))
    cli.launch(tmp_path, run=fake.run, clock=fake.clock)
    assert fake.argvs == [
        ["claude", "--dangerously-skip-permissions", "--continue"],
        ["claude", "--dangerously-skip-permissions"],  # fresh: nothing left to resume
    ]
    assert not transcript.exists()
    assert (project / "sdk.jsonl.broken").exists()  # renamed, never deleted


def test_launch_falls_back_to_an_older_healthy_transcript(tmp_path: Path, workspace: Path) -> None:
    # Quarantining the refused transcript uncovers the next one down, so history is recovered
    # rather than dropped — better than the manual "move them all aside" workaround.
    cli = ClaudeAgentCLI()
    project = cli.project_dir(tmp_path, workspace)
    older = _transcript(project, "older.jsonl", mtime_ns=1_000)
    _transcript(project, "newest.jsonl", mtime_ns=2_000)
    fake = _FakeLaunch((1, 0.2), (0, 5.0))
    cli.launch(tmp_path, run=fake.run, clock=fake.clock)
    assert len(fake.argvs) == 2
    assert fake.argvs[1] == ["claude", "--dangerously-skip-permissions", "--continue"]
    assert cli.resume_target(tmp_path, workspace) == older


def test_launch_does_not_retry_when_the_cli_is_killed_by_a_signal(
    tmp_path: Path, workspace: Path
) -> None:
    # A negative returncode is the container going down (the entrypoint's SIGTERM), not a refused
    # resume — relaunching would fight the teardown and quarantine a healthy transcript.
    cli = ClaudeAgentCLI()
    transcript = _transcript(cli.project_dir(tmp_path, workspace), "a.jsonl", mtime_ns=1_000)
    fake = _FakeLaunch((-15, 0.1))
    cli.launch(tmp_path, run=fake.run, clock=fake.clock)
    assert len(fake.argvs) == 1
    assert transcript.exists()


def test_launch_does_not_retry_when_a_resumed_session_fails_slowly(
    tmp_path: Path, workspace: Path
) -> None:
    # It ran long enough to have been a real session; its history is worth keeping.
    cli = ClaudeAgentCLI()
    transcript = _transcript(cli.project_dir(tmp_path, workspace), "a.jsonl", mtime_ns=1_000)
    fake = _FakeLaunch((1, RESUME_FAILURE_WINDOW_SECONDS + 1))
    cli.launch(tmp_path, run=fake.run, clock=fake.clock)
    assert len(fake.argvs) == 1
    assert transcript.exists()


def test_launch_stops_retrying_and_starts_fresh_after_the_cap(
    tmp_path: Path, workspace: Path
) -> None:
    # Every launch refused: the loop must terminate, and its last pass must be a first run.
    cli = ClaudeAgentCLI()
    project = cli.project_dir(tmp_path, workspace)
    for n in range(5):
        _transcript(project, f"t{n}.jsonl", mtime_ns=1_000 + n)
    fake = _FakeLaunch((1, 0.2))
    cli.launch(tmp_path, run=fake.run, clock=fake.clock)
    assert len(fake.argvs) == MAX_RESUME_FALLBACKS + 1
    assert fake.argvs[-1] == ["claude", "--dangerously-skip-permissions"]  # fresh
    assert not list(project.glob("*.jsonl"))  # all cleared, so a respawn also starts fresh


def test_launch_points_the_cli_at_its_config_dir_and_normalizes_credentials(
    tmp_path: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Hoisted out of the two adapters into the shared template: the config-dir env var is what
    # makes session history (a per-task volume) resumable at all (ADR 0014 §4a).
    monkeypatch.setenv("ANTHROPIC_API_KEY", '"sk-ant-quoted"')
    fake = _FakeLaunch((0, 1.0))
    ClaudeAgentCLI().launch(tmp_path, run=fake.run, clock=fake.clock)
    assert fake.envs[0][ClaudeAgentCLI.CONFIG_ENV_VAR] == str(tmp_path)
    assert fake.envs[0]["ANTHROPIC_API_KEY"] == "sk-ant-quoted"


def test_launch_passes_the_turn_derived_prompt_through_to_the_argv(
    tmp_path: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The template reads the launch env; the adapter decides what it means.
    monkeypatch.setenv("PANOPTICON_TASK_TURN", "agent")
    cli = ClaudeAgentCLI()
    _transcript(cli.project_dir(tmp_path, workspace), "a.jsonl", mtime_ns=1_000)
    fake = _FakeLaunch((0, 1.0))
    cli.launch(tmp_path, run=fake.run, clock=fake.clock)
    assert fake.argvs[0][-1] == INTERRUPT_PROMPT


def test_quarantine_never_clobbers_an_existing_quarantined_file(tmp_path: Path) -> None:
    first = tmp_path / "a.jsonl"
    first.write_text("one")
    assert ClaudeAgentCLI().quarantine(first) == tmp_path / "a.jsonl.broken"
    first.write_text("two")
    assert ClaudeAgentCLI().quarantine(first) == tmp_path / "a.jsonl.broken.1"
    assert (tmp_path / "a.jsonl.broken").read_text() == "one"  # the earlier evidence survives


def test_quarantine_is_best_effort(tmp_path: Path) -> None:
    # A launch must not die trying to tidy up.
    assert ClaudeAgentCLI().quarantine(tmp_path / "gone.jsonl") is None

"""The codex harness: config.toml rendering (validated as real TOML), skills, auth, argv.

Facts pinned against codex-cli 0.144.4: the config keys come from its published config schema;
the api-key ``auth.json`` shape is what ``codex login --with-api-key`` writes; ``codex resume``
takes ``--last``, the bypass flags, and a positional prompt.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from panopticon.core.models import Skill
from panopticon.harnesses import INTERRUPT_PROMPT, BootstrapContext, LaunchContext
from panopticon.harnesses.codex import CODEX_VERSION, CodexHarness, render_config

HARNESS = CodexHarness()


def _ctx(home: Path, **kwargs: str | None) -> LaunchContext:
    return LaunchContext(home=home, cwd=Path("/workspace"), **kwargs)  # type: ignore[arg-type]


def _bootstrap_ctx(home: Path, **kwargs: object) -> BootstrapContext:
    defaults: dict[str, object] = {
        "home": home,
        "cwd": Path("/workspace"),
        "service_url": "http://host.docker.internal:8000",
        "task_id": "t1",
        "skills": [Skill(name="open-pr", description="Open the PR.", instructions="gh pr create")],
        "operations": {"advance": "COMPLETE"},
        "overview": "# the workflow map",
        "environ": {},
    }
    defaults.update(kwargs)
    return BootstrapContext(**defaults)  # type: ignore[arg-type]


# -- config.toml ---------------------------------------------------------------------


def test_config_is_valid_toml_with_the_panopticon_mcp_server() -> None:
    cfg = tomllib.loads(render_config("http://svc:8000", "# map", Path("/workspace")))
    assert cfg["mcp_servers"]["panopticon"] == {"url": "http://svc:8000/mcp"}


def test_config_carries_the_overview_as_developer_instructions() -> None:
    cfg = tomllib.loads(
        render_config("http://svc:8000", "# map\nwith 'quotes' & \"lines\"", Path("/w"))
    )
    assert cfg["developer_instructions"] == "# map\nwith 'quotes' & \"lines\""


def test_config_omits_developer_instructions_when_no_overview() -> None:
    cfg = tomllib.loads(render_config("http://svc:8000", "  ", Path("/w")))
    assert "developer_instructions" not in cfg


def test_config_trusts_the_workspace() -> None:
    # codex's analog of claude's trust dialog — an unattended container can't answer it.
    cfg = tomllib.loads(render_config("http://svc:8000", "", Path("/workspace")))
    assert cfg["projects"]["/workspace"] == {"trust_level": "trusted"}


def test_config_wires_the_turn_flip_hooks_to_the_shared_callback() -> None:
    # codex's hooks system is Claude-Code-compatible (same events, same JSON-on-stdin), so both
    # events invoke the exact command claude's settings.json uses — one callback, two harnesses.
    cfg = tomllib.loads(render_config("http://svc:8000", "", Path("/w")))
    stop = cfg["hooks"]["Stop"][0]["hooks"][0]
    prompt = cfg["hooks"]["UserPromptSubmit"][0]["hooks"][0]
    assert stop == {"type": "command", "command": "python -m panopticon.container.hook user stop"}
    assert prompt == {
        "type": "command",
        "command": "python -m panopticon.container.hook agent prompt",
    }


def test_config_disables_the_builtin_codex_apps_connector() -> None:
    # It can't start in the container and would stall every spawn on its 30s startup timeout.
    cfg = tomllib.loads(render_config("http://svc:8000", "", Path("/w")))
    assert cfg["mcp_servers"]["codex_apps"] == {"enabled": False}


def test_config_forces_file_backed_credentials() -> None:
    # Containers have no OS keyring, and the subscription flow shares auth.json via a mount.
    cfg = tomllib.loads(render_config("http://svc:8000", "", Path("/w")))
    assert cfg["cli_auth_credentials_store"] == "file"


# -- bootstrap: skills + operations + auth ------------------------------------------


def test_bootstrap_writes_config_and_skills(tmp_path: Path) -> None:
    HARNESS.bootstrap(_bootstrap_ctx(tmp_path))
    assert (tmp_path / ".codex" / "config.toml").exists()
    skill = (tmp_path / ".agents" / "skills" / "open-pr" / "SKILL.md").read_text()
    assert skill.startswith("---\nname: open-pr\ndescription: Open the PR.\n---\ngh pr create")
    assert 'task_id="t1"' in skill  # the concrete task id, injected for MCP tool calls
    operation = (tmp_path / ".agents" / "skills" / "advance" / "SKILL.md").read_text()
    assert "apply_operation" in operation and "COMPLETE" in operation


def test_bootstrap_renders_skills_user_scope_not_into_the_workspace(tmp_path: Path) -> None:
    # The workspace is the task's git clone — rendered files there could end up in a commit, so
    # skills land under the *home* (codex's user scope), never under ctx.cwd.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    HARNESS.bootstrap(_bootstrap_ctx(tmp_path, cwd=workspace))
    assert not (workspace / ".agents").exists()
    assert (tmp_path / ".agents" / "skills").is_dir()


def test_bootstrap_writes_an_api_key_auth_file_from_the_env(tmp_path: Path) -> None:
    HARNESS.bootstrap(_bootstrap_ctx(tmp_path, environ={"CODEX_API_KEY": "sk-x"}))
    auth = tmp_path / ".codex" / "auth.json"
    # The exact shape `codex login --with-api-key` writes (observed, codex-cli 0.144.4).
    assert json.loads(auth.read_text()) == {"auth_mode": "apikey", "OPENAI_API_KEY": "sk-x"}
    assert (auth.stat().st_mode & 0o777) == 0o600


def test_bootstrap_symlinks_auth_from_the_credential_mount(tmp_path: Path) -> None:
    # The repo's shared credential dir (ChatGPT subscription): every container of the repo links
    # the same auth.json, so a token refresh by any session is visible to all (codex re-reads the
    # file before refreshing and writes through the symlink — verified against 0.144.4).
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    (credentials / "auth.json").write_text('{"auth_mode": "chatgpt"}')
    HARNESS.bootstrap(
        _bootstrap_ctx(tmp_path, environ={"PANOPTICON_CREDENTIALS": str(credentials)})
    )
    auth = tmp_path / ".codex" / "auth.json"
    assert auth.is_symlink() and auth.resolve() == (credentials / "auth.json").resolve()


def test_bootstrap_prefers_the_credential_mount_over_an_env_key(tmp_path: Path) -> None:
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    (credentials / "auth.json").write_text('{"auth_mode": "chatgpt"}')
    HARNESS.bootstrap(
        _bootstrap_ctx(
            tmp_path,
            environ={"PANOPTICON_CREDENTIALS": str(credentials), "CODEX_API_KEY": "sk-x"},
        )
    )
    assert (tmp_path / ".codex" / "auth.json").is_symlink()  # subscription wins


def test_bootstrap_never_clobbers_an_existing_auth_file(tmp_path: Path) -> None:
    config_dir = tmp_path / ".codex"
    config_dir.mkdir(parents=True)
    (config_dir / "auth.json").write_text('{"auth_mode": "chatgpt", "tokens": {}}')
    HARNESS.bootstrap(_bootstrap_ctx(tmp_path, environ={"CODEX_API_KEY": "sk-x"}))
    assert json.loads((config_dir / "auth.json").read_text())["auth_mode"] == "chatgpt"


def test_bootstrap_is_idempotent_across_respawns(tmp_path: Path) -> None:
    ctx = _bootstrap_ctx(tmp_path, environ={"CODEX_API_KEY": "sk-x"})
    HARNESS.bootstrap(ctx)
    HARNESS.bootstrap(ctx)  # a respawn re-runs the bootstrap on the same config volume
    assert (tmp_path / ".codex" / "config.toml").exists()


# -- auth check ----------------------------------------------------------------------


def test_missing_auth_accepts_each_credential_source(tmp_path: Path) -> None:
    assert HARNESS.missing_auth({"CODEX_API_KEY": "k"}, home=tmp_path) is None
    assert HARNESS.missing_auth({"OPENAI_API_KEY": "k"}, home=tmp_path) is None
    assert HARNESS.missing_auth({"CODEX_ACCESS_TOKEN": "t"}, home=tmp_path) is None


def test_missing_auth_accepts_a_mounted_credential_dir(tmp_path: Path) -> None:
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    (credentials / "auth.json").write_text("{}")
    env = {"PANOPTICON_CREDENTIALS": str(credentials)}
    assert HARNESS.missing_auth(env, home=tmp_path) is None


def test_missing_auth_accepts_an_auth_file_on_the_config_volume(tmp_path: Path) -> None:
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "auth.json").write_text("{}")
    assert HARNESS.missing_auth({}, home=tmp_path) is None


def test_missing_auth_names_the_fix_when_nothing_is_configured(tmp_path: Path) -> None:
    detail = HARNESS.missing_auth({}, home=tmp_path)
    assert detail is not None
    assert "CODEX_API_KEY" in detail and "credential_dir" in detail


# -- argv ----------------------------------------------------------------------------

_BYPASS = ["--dangerously-bypass-approvals-and-sandbox", "--dangerously-bypass-hook-trust"]


def _seed_session(home: Path) -> None:
    rollouts = home / ".codex" / "sessions" / "2026" / "07" / "15"
    rollouts.mkdir(parents=True)
    (rollouts / "rollout-1.jsonl").write_text("{}")


def test_argv_first_run_bypasses_approvals_and_hook_trust(tmp_path: Path) -> None:
    # The container is the sandbox — same posture as claude --dangerously-skip-permissions; the
    # hook-trust bypass is required or codex stops on an interactive per-hash trust prompt.
    assert HARNESS.argv(_ctx(tmp_path)) == ["codex", *_BYPASS]


def test_argv_first_run_passes_model_then_prompt(tmp_path: Path) -> None:
    argv = HARNESS.argv(_ctx(tmp_path, initial_prompt="start now", starting_model="gpt-5.6-sol"))
    assert argv == ["codex", *_BYPASS, "--model", "gpt-5.6-sol", "start now"]


def test_argv_resumes_the_last_session_when_one_is_recorded(tmp_path: Path) -> None:
    _seed_session(tmp_path)
    assert HARNESS.argv(_ctx(tmp_path)) == ["codex", "resume", "--last", *_BYPASS]


def test_argv_resume_appends_interrupt_prompt_on_agent_turn(tmp_path: Path) -> None:
    _seed_session(tmp_path)
    argv = HARNESS.argv(_ctx(tmp_path, turn="agent"))
    assert argv == ["codex", "resume", "--last", *_BYPASS, INTERRUPT_PROMPT]


def test_argv_resume_omits_model_and_initial_prompt(tmp_path: Path) -> None:
    _seed_session(tmp_path)
    argv = HARNESS.argv(_ctx(tmp_path, initial_prompt="start now", starting_model="gpt-5.6-sol"))
    assert "--model" not in argv and "start now" not in argv


# -- image layer + env ----------------------------------------------------------------


def test_image_layer_installs_the_pinned_release_for_both_architectures() -> None:
    layer = HARNESS.image_layer()
    assert f"rust-v{CODEX_VERSION}" in layer  # pinned, not `latest` — the verified version
    assert "x86_64-unknown-linux-musl" in layer and "aarch64-unknown-linux-musl" in layer
    assert "--extract --gzip --directory" in layer  # long options (repo convention)


def test_env_points_codex_at_the_per_task_config_dir(tmp_path: Path) -> None:
    assert HARNESS.env(_ctx(tmp_path)) == {"CODEX_HOME": str(tmp_path / ".codex")}

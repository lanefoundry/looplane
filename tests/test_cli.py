from __future__ import annotations

import runpy
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from coding_agent import cli
from coding_agent.approvals import HeadlessApprovalPolicy
from coding_agent.codex_oauth import CodexCredentials, CodexCredentialStore
from coding_agent.contracts import ModelTurn, RunResult, RunStatus, ToolCall
from coding_agent.models import ScriptedModel

FIX_PATCH = """\
diff --git a/src/tiny_python_bug/calculator.py b/src/tiny_python_bug/calculator.py
--- a/src/tiny_python_bug/calculator.py
+++ b/src/tiny_python_bug/calculator.py
@@ -1,3 +1,3 @@
 def add(left: int, right: int) -> int:
     \"\"\"Return the sum of two integers.\"\"\"
-    return left - right
+    return left + right
"""


def test_bare_pca_runs_our_agent_loop_with_trace_and_session(
    tiny_bug_repo: Path, tmp_path: Path, monkeypatch
) -> None:
    model = ScriptedModel(
        [
            ModelTurn(
                tool_calls=(ToolCall(name="apply_patch", arguments={"patch": FIX_PATCH}),)
            ),
            ModelTurn(content="Fixed through the interactive CLI."),
        ]
    )
    monkeypatch.setattr(cli, "_model_from_env", lambda **_: model)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        cli,
        "TTYApprovalPolicy",
        lambda *_: HeadlessApprovalPolicy(allow_modify=True, allow_execute=True),
    )
    run_root = tmp_path / "runs"

    result = CliRunner().invoke(
        cli.app,
        [
            "--repo",
            str(tiny_bug_repo),
            "--task",
            "Fix the calculator.",
            "--model",
            "scripted",
            "--check",
            "pytest -q",
            "--run-root",
            str(run_root),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "completed: Fixed through the interactive CLI." in result.output
    assert "session:" in result.output
    sessions = list(run_root.glob("*/session.json"))
    assert len(sessions) == 1
    assert (sessions[0].parent / "changes.patch").is_file()


def test_cli_help_exposes_interactive_options_headless_run_and_resume() -> None:
    result = CliRunner().invoke(cli.app, ["--help"])

    assert result.exit_code == 0
    assert "--api-url" in result.output
    assert "resume" in result.output
    assert "exec" in result.output
    assert "config" in result.output
    assert "run" in result.output


def test_positional_prompt_cd_alias_and_print_mode_use_own_loop(
    tiny_bug_repo: Path, tmp_path: Path, monkeypatch
) -> None:
    model = ScriptedModel([ModelTurn(content="No source change was needed.")])
    captured: dict[str, object] = {}
    original_runner = cli.AgentRunner

    class CapturingRunner(original_runner):
        def __init__(self, task, selected_model, run_root, **kwargs) -> None:
            captured["task"] = task
            captured["kwargs"] = kwargs
            super().__init__(task, selected_model, run_root, **kwargs)

    monkeypatch.setattr(cli, "AgentRunner", CapturingRunner)
    monkeypatch.setattr(cli, "_model_from_env", lambda **_: model)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("PCA_CONFIG", str(tmp_path / "missing-config.json"))

    result = CliRunner().invoke(
        cli.app,
        [
            "-p",
            "-C",
            str(tiny_bug_repo),
            "--model",
            "scripted",
            "--unsafe-local-exec",
            "No source change is needed.",
        ],
    )

    assert result.exit_code == 0, result.output
    assert '"status": "completed"' in result.output
    assert captured["task"].repository == tiny_bug_repo
    assert captured["task"].instruction == "No source change is needed."
    assert captured["kwargs"]["approval_policy"] is None


def test_known_subcommand_wins_over_default_prompt_routing(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        cli.app,
        ["resume", "last", "--run-root", str(tmp_path / "missing")],
    )

    assert result.exit_code == 2
    assert "no such command" not in result.output.lower()
    assert "PROMPT" not in result.output


def test_config_command_and_cli_env_config_precedence(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "config.json"
    monkeypatch.setenv("PCA_CONFIG", str(path))
    saved = CliRunner().invoke(
        cli.app,
        [
            "config",
            "--provider",
            "ollama",
            "--model",
            "qwen3:4b",
            "--api-url",
            "http://127.0.0.1:11434/v1",
        ],
    )
    shown = CliRunner().invoke(cli.app, ["config"])

    assert saved.exit_code == 0, saved.output
    assert shown.exit_code == 0
    assert "provider: ollama" in shown.output
    assert "model: qwen3:4b" in shown.output
    assert "api_key" not in path.read_text()
    assert cli._resolve_cli_settings(provider=None, model=None, api_url=None) == (
        "ollama",
        "qwen3:4b",
        "http://127.0.0.1:11434/v1",
    )
    assert cli._resolve_cli_settings(
        provider="anthropic",
        model="claude-test",
        api_url="https://proxy.example/v1",
    ) == ("anthropic", "claude-test", "https://proxy.example/v1")
    assert cli._resolve_cli_settings(
        provider=None,
        model="gemini/gemini-test",
        api_url=None,
    ) == ("gemini", "gemini-test", None)

    switched = CliRunner().invoke(
        cli.app,
        ["config", "--provider", "openai-compatible"],
    )
    assert switched.exit_code == 0, switched.output
    switched_config = cli.load_cli_config(path)
    assert switched_config.provider == "openai-compatible"
    assert switched_config.model is None
    assert switched_config.api_url is None


def test_root_completion_option_is_not_routed_as_a_prompt() -> None:
    result = CliRunner().invoke(cli.app, ["--show-completion"])

    assert result.exit_code == 1
    assert "Shell" in result.output
    assert "PROMPT is required" not in result.output


def test_root_shell_completion_lists_subcommands_instead_of_routing_to_chat() -> None:
    result = CliRunner().invoke(
        cli.app,
        [],
        env={
            "_ROOT_COMPLETE": "complete_bash",
            "COMP_WORDS": "root ",
            "COMP_CWORD": "1",
        },
    )

    assert result.exit_code == 0, result.output
    assert "exec" in result.output
    assert "resume" in result.output
    assert "config" in result.output


def test_root_shell_completion_includes_default_prompt_options() -> None:
    runner = CliRunner()
    for words, word_index in (("root --", "1"), ("root -p --", "2"), ("root Fix --", "2")):
        result = runner.invoke(
            cli.app,
            [],
            env={
                "_ROOT_COMPLETE": "complete_bash",
                "COMP_WORDS": words,
                "COMP_CWORD": word_index,
            },
        )

        assert result.exit_code == 0, result.output
        assert "--cd" in result.output
        assert "--model" in result.output
        assert "--provider" in result.output
        assert "--check" in result.output


def test_routed_help_never_exposes_hidden_chat_command() -> None:
    result = CliRunner().invoke(cli.app, ["-p", "--help"])

    assert result.exit_code == 0
    assert "Usage: root [OPTIONS] [PROMPT]" in result.output
    assert "root chat" not in result.output


def test_legacy_short_provider_option_has_actionable_migration_error(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("PCA_CONFIG", str(tmp_path / "missing.json"))
    result = CliRunner().invoke(
        cli.app,
        ["-p", "ollama", "--model", "qwen3:4b", "--task", "Fix it"],
    )

    assert result.exit_code == 2
    assert "-p now means --print" in result.output
    assert "--provider" in result.output
    assert "pca chat" not in result.output


def test_exec_positional_and_legacy_run_flags_share_headless_contract(
    tiny_bug_repo: Path, tmp_path: Path, monkeypatch
) -> None:
    tasks = []

    class FakeRunner:
        def __init__(self, task, _model, _run_root, **_kwargs) -> None:
            tasks.append(task)

        async def run(self):
            return RunResult(
                run_id="headless-run",
                task_id="headless-task",
                status=RunStatus.COMPLETED,
                summary="verified",
                terminal_reason="verified",
                artifacts={"patch": str(tmp_path / "changes.patch")},
            )

    monkeypatch.setattr(cli, "AgentRunner", FakeRunner)
    model_options = []

    def fake_model(**kwargs):
        model_options.append(kwargs)
        return ScriptedModel([ModelTurn(content="unused")])

    monkeypatch.setattr(cli, "_model_from_env", fake_model)
    monkeypatch.setenv("PCA_CONFIG", str(tmp_path / "missing.json"))

    modern = CliRunner().invoke(
        cli.app,
        ["exec", "Fix it", "-C", str(tiny_bug_repo), "--model", "scripted"],
    )
    legacy = CliRunner().invoke(
        cli.app,
        [
            "run",
            "--task",
            "Fix it again",
            "--repo",
            str(tiny_bug_repo),
            "--model",
            "scripted",
        ],
    )

    assert modern.exit_code == 0, modern.output
    assert legacy.exit_code == 0, legacy.output
    assert [task.instruction for task in tasks] == ["Fix it", "Fix it again"]
    assert all(task.repository == tiny_bug_repo for task in tasks)
    assert [options["tool_calling"] for options in model_options] == [False, False]


def test_ollama_preset_retains_a_bounded_tool_turn_budget(monkeypatch) -> None:
    captured: dict[str, object] = {}
    sentinel = object()

    def fake_model(**kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(cli, "OpenAICompatibleModel", fake_model)

    model = cli._model_from_env(
        provider="ollama",
        model="qwen3:4b",
        base_url=None,
        tool_calling=True,
        allow_custom_provider_endpoint=False,
    )

    assert model is sentinel
    assert captured["max_tokens"] == 4_096
    assert captured["extra_body"] == {"think": False}
    assert captured["user_message_prefix"] == "/no_think\n"
    assert captured["api_key"] is None


def test_remote_ollama_uses_explicit_key_but_loopback_never_receives_it(monkeypatch) -> None:
    captured: list[dict[str, object]] = []

    def fake_model(**kwargs):
        captured.append(kwargs)
        return object()

    monkeypatch.setattr(cli, "OpenAICompatibleModel", fake_model)
    monkeypatch.setenv("OLLAMA_API_KEY", "remote-only-secret")

    cli._model_from_env(
        provider="ollama",
        model="remote-model",
        base_url="https://ollama.com/v1",
        tool_calling=True,
        allow_custom_provider_endpoint=False,
    )
    cli._model_from_env(
        provider="ollama",
        model="local-model",
        base_url="http://127.0.0.1:11434/v1",
        tool_calling=True,
        allow_custom_provider_endpoint=False,
    )

    assert captured[0]["api_key"] == "remote-only-secret"
    assert captured[1]["api_key"] is None


def test_claude_backend_requires_explicit_coding_boundaries(
    monkeypatch, tiny_bug_repo: Path, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    class FakeBackend:
        def __init__(self, *, timeout_seconds: float) -> None:
            captured["timeout"] = timeout_seconds

    class FakeRunner:
        def __init__(self, task, backend, run_root, **kwargs) -> None:
            captured["task"] = task
            captured["backend"] = backend
            captured["run_root"] = run_root
            captured.update(kwargs)

        async def run(self):
            return RunResult(
                run_id="delegated-run",
                task_id="delegated-1",
                status=RunStatus.COMPLETED,
                summary="delegated",
                terminal_reason="verified",
                artifacts={"patch": str(tmp_path / "changes.patch")},
            )

    monkeypatch.setattr(cli, "ClaudeCodeBackend", FakeBackend)
    monkeypatch.setattr(cli, "ExternalCodingRunner", FakeRunner)

    rejected = CliRunner().invoke(
        cli.app,
        ["backend", "claude-code", "--task", "inspect this"],
    )
    accepted = CliRunner().invoke(
        cli.app,
        [
            "backend",
            "claude-code",
            "--task",
            "inspect this",
            "--repo",
            str(tiny_bug_repo),
            "--task-id",
            "delegated-1",
            "--timeout",
            "45",
            "--experimental-subscription",
            "--check",
            "pytest -q",
            "--allow-external-modify",
            "--unsafe-local-exec",
        ],
    )

    assert rejected.exit_code == 2
    assert accepted.exit_code == 0, accepted.output
    assert "completed: delegated" in accepted.output
    assert captured["timeout"] == 45
    assert captured["task"].task_id == "delegated-1"
    assert captured["allow_external_modify"] is True
    assert captured["allow_unsafe_local_exec"] is True

    missing_check = CliRunner().invoke(
        cli.app,
        [
            "backend",
            "claude-code",
            "--task",
            "inspect this",
            "--repo",
            str(tiny_bug_repo),
            "--experimental-subscription",
            "--allow-external-modify",
            "--unsafe-local-exec",
        ],
    )
    assert missing_check.exit_code == 2
    assert "requires at least one explicit --check" in missing_check.output


def test_codex_login_closes_oauth_client_when_callback_fails(monkeypatch) -> None:
    closed = False

    class FakeOAuth:
        def begin_login(self, *, originator: str):
            assert originator == "python-coding-agent"
            return SimpleNamespace(url="https://example.test/login", state="state")

        async def aclose(self) -> None:
            nonlocal closed
            closed = True

    monkeypatch.setattr(cli, "CodexOAuthClient", FakeOAuth)
    monkeypatch.setattr(cli.webbrowser, "open", lambda _: True)

    def fail_after_listening(_, *, on_listening, **__) -> None:
        on_listening(("127.0.0.1", 1455))
        raise TimeoutError("callback timed out")

    monkeypatch.setattr(
        cli,
        "wait_for_codex_callback",
        fail_after_listening,
    )

    result = CliRunner().invoke(cli.app, ["auth", "login-codex"])

    assert result.exit_code == 2
    assert closed is True


def test_codex_login_opens_browser_only_after_listener_is_ready(monkeypatch) -> None:
    ready = False
    closed = False

    class FakeOAuth:
        def begin_login(self, *, originator: str):
            return SimpleNamespace(url="https://example.test/login", state="state")

        async def aclose(self) -> None:
            nonlocal closed
            closed = True

    def open_browser(_: str) -> bool:
        assert ready is True
        return True

    def fail_after_listening(_, *, on_listening, **__) -> None:
        nonlocal ready
        ready = True
        on_listening(("127.0.0.1", 1455))
        raise TimeoutError("callback timed out")

    monkeypatch.setattr(cli, "CodexOAuthClient", FakeOAuth)
    monkeypatch.setattr(cli.webbrowser, "open", open_browser)
    monkeypatch.setattr(cli, "wait_for_codex_callback", fail_after_listening)

    result = CliRunner().invoke(cli.app, ["auth", "login-codex", "--timeout", "1"])

    assert result.exit_code == 2
    assert ready is True
    assert closed is True


def test_codex_manual_login_hides_callback_and_never_opens_browser(
    tmp_path: Path, monkeypatch
) -> None:
    credential_path = tmp_path / "auth" / "openai-codex.json"
    prompt_options: dict[str, object] = {}

    class FakeOAuth:
        def begin_login(self, *, originator: str):
            return SimpleNamespace(
                url="https://example.test/login",
                state="expected",
                verifier="verifier",
            )

        async def exchange_code(self, *, code: str, verifier: str) -> CodexCredentials:
            assert code == "short-lived"
            assert verifier == "verifier"
            return CodexCredentials(
                access_token="access-secret",
                refresh_token="refresh-secret",
                expires_at=4_000_000_000,
                account_id="account-secret",
            )

        async def aclose(self) -> None:
            return

    def prompt(_: str, **kwargs) -> str:
        prompt_options.update(kwargs)
        return (
            "http://localhost:1455/auth/callback?"
            "code=short-lived&state=expected"
        )

    monkeypatch.setattr(cli, "CodexOAuthClient", FakeOAuth)
    monkeypatch.setattr(cli, "_codex_credential_path", lambda: credential_path)
    monkeypatch.setattr(cli.typer, "prompt", prompt)
    monkeypatch.setattr(
        cli.webbrowser,
        "open",
        lambda _: (_ for _ in ()).throw(AssertionError("browser must not open")),
    )

    result = CliRunner().invoke(cli.app, ["auth", "login-codex", "--manual"])

    assert result.exit_code == 0, result.output
    assert prompt_options["hide_input"] is True
    assert "short-lived" not in result.output
    assert credential_path.is_file()


def test_codex_status_is_redacted_and_logout_removes_only_app_grant(
    tmp_path: Path, monkeypatch
) -> None:
    credential_path = tmp_path / "auth" / "openai-codex.json"
    CodexCredentialStore(credential_path).save(
        CodexCredentials(
            access_token="access-secret",
            refresh_token="refresh-secret",
            expires_at=4_000_000_000,
            account_id="account-secret",
        )
    )
    monkeypatch.setattr(cli, "_codex_credential_path", lambda: credential_path)

    status = CliRunner().invoke(cli.app, ["auth", "status-codex"])
    logout = CliRunner().invoke(cli.app, ["auth", "logout-codex"])

    assert status.exit_code == 0
    assert "configured (valid)" in status.output
    assert "secret" not in status.output
    assert logout.exit_code == 0
    assert not credential_path.exists()


def test_live_eval_requires_explicit_subscription_opt_in() -> None:
    script = Path(__file__).parents[1] / "scripts" / "eval_live_provider.py"
    build_agent_command = runpy.run_path(str(script))["build_agent_command"]
    config = {
        "task": "Fix it",
        "check": "pytest -q",
        "max_steps": 3,
        "wall_time_seconds": 30,
        "allowed_paths": ["src/**"],
    }

    disabled = build_agent_command(
        config=config,
        source=Path("/tmp/source"),
        run_root=Path("/tmp/runs"),
        provider="openai-codex",
        model="codex-test",
        base_url=None,
        experimental_subscription=False,
    )
    enabled = build_agent_command(
        config=config,
        source=Path("/tmp/source"),
        run_root=Path("/tmp/runs"),
        provider="openai-codex",
        model="codex-test",
        base_url=None,
        experimental_subscription=True,
    )

    assert "--experimental-subscription" not in disabled
    assert enabled[-1] == "--experimental-subscription"


def test_real_tty_routes_bare_prompt_to_full_screen_tui(
    tiny_bug_repo: Path, tmp_path: Path, monkeypatch
) -> None:
    from coding_agent import tui

    captured: dict[str, object] = {}

    class FakeApp:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)
            self.last_error = None

        def run(self):
            captured["ran"] = True
            return None

    monkeypatch.setenv("PCA_CONFIG", str(tmp_path / "missing-config.json"))
    monkeypatch.setattr(cli, "_terminal_supports_tui", lambda: True)
    monkeypatch.setattr(cli, "_discover_local_ollama_models", lambda: ("qwen3:4b",))
    monkeypatch.setattr(tui, "PcaApp", FakeApp)

    result = CliRunner().invoke(
        cli.app,
        ["-C", str(tiny_bug_repo), "Fix the failing test."],
    )

    assert result.exit_code == 0, result.output
    assert captured["ran"] is True
    assert captured["initial_prompt"] == "Fix the failing test."
    assert captured["repository"] == tiny_bug_repo
    assert captured["ollama_models"] == ("qwen3:4b",)


def test_plain_flag_never_launches_full_screen_tui(
    tiny_bug_repo: Path, tmp_path: Path, monkeypatch
) -> None:
    from coding_agent import tui

    class FakeRunner:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def run(self):
            return RunResult(
                run_id="plain-run",
                task_id="plain-task",
                status=RunStatus.COMPLETED,
                summary="plain completed",
                terminal_reason="verified",
                artifacts={"patch": str(tmp_path / "changes.patch")},
            )

    monkeypatch.setattr(cli, "_terminal_supports_tui", lambda: True)
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(
        tui,
        "PcaApp",
        lambda **_: (_ for _ in ()).throw(AssertionError("TUI must not launch")),
    )
    monkeypatch.setattr(
        cli,
        "_model_from_env",
        lambda **_: ScriptedModel([ModelTurn(content="unused")]),
    )
    monkeypatch.setattr(cli, "AgentRunner", FakeRunner)

    result = CliRunner().invoke(
        cli.app,
        [
            "--plain",
            "-m",
            "ollama/qwen3:4b",
            "-C",
            str(tiny_bug_repo),
            "Explain this repository.",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "plain completed" in result.output

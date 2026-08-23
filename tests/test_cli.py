from __future__ import annotations

import builtins
import runpy
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer
from typer.testing import CliRunner

from rivumi import cli
from rivumi.approvals import HeadlessApprovalPolicy
from rivumi.claude_conversation import IsolatedClaudeConversation
from rivumi.codex_conversation import IsolatedCodexConversation
from rivumi.codex_oauth import CodexCredentials, CodexCredentialStore
from rivumi.contracts import ModelTurn, RunResult, RunStatus, ToolCall
from rivumi.conversation_controller import ConversationController
from rivumi.loop import AgentRunner
from rivumi.models import ScriptedModel

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


def test_bare_rivumi_runs_our_agent_loop_with_trace_and_session(
    tiny_bug_repo: Path, tmp_path: Path, monkeypatch
) -> None:
    model = ScriptedModel(
        [
            ModelTurn(tool_calls=(ToolCall(name="apply_patch", arguments={"patch": FIX_PATCH}),)),
            ModelTurn(content="Fixed through the interactive CLI."),
        ]
    )
    monkeypatch.setattr(cli, "_model_from_env", lambda **_: model)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        "rivumi.approvals.TTYApprovalPolicy",
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


def test_missing_textual_reports_editable_install_refresh(tmp_path: Path, monkeypatch) -> None:
    original_import = builtins.__import__

    def import_without_textual(name, *args, **kwargs):
        if name == "rivumi.tui":
            raise ModuleNotFoundError("No module named 'textual'", name="textual")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_textual)
    monkeypatch.setattr(cli, "_terminal_supports_tui", lambda: True)
    monkeypatch.setenv("RIVUMI_CONFIG", str(tmp_path / "config.json"))

    result = CliRunner().invoke(cli.app, ["--model", "qwen3:4b"])

    assert result.exit_code == 2
    expected_script = Path(cli.__file__).resolve().parents[2] / "scripts" / "install-dev-cli"
    assert str(expected_script) in result.output
    assert "Traceback" not in result.output


def test_positional_prompt_cd_alias_and_print_mode_use_own_loop(
    tiny_bug_repo: Path, tmp_path: Path, monkeypatch
) -> None:
    model = ScriptedModel([ModelTurn(content="No source change was needed.")])
    captured: dict[str, object] = {}
    original_runner = AgentRunner

    class CapturingRunner(original_runner):
        def __init__(self, task, selected_model, run_root, **kwargs) -> None:
            captured["task"] = task
            captured["kwargs"] = kwargs
            super().__init__(task, selected_model, run_root, **kwargs)

    monkeypatch.setattr("rivumi.loop.AgentRunner", CapturingRunner)
    monkeypatch.setattr(cli, "_model_from_env", lambda **_: model)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("RIVUMI_CONFIG", str(tmp_path / "missing-config.json"))

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
    monkeypatch.setenv("RIVUMI_CONFIG", str(path))
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
    monkeypatch.setenv("RIVUMI_CONFIG", str(tmp_path / "missing.json"))
    result = CliRunner().invoke(
        cli.app,
        ["-p", "ollama", "--model", "qwen3:4b", "--task", "Fix it"],
    )

    assert result.exit_code == 2
    assert "-p now means --print" in result.output
    assert "--provider" in result.output
    assert "rivumi chat" not in result.output


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

    monkeypatch.setattr("rivumi.loop.AgentRunner", FakeRunner)
    model_options = []

    def fake_model(**kwargs):
        model_options.append(kwargs)
        return ScriptedModel([ModelTurn(content="unused")])

    monkeypatch.setattr(cli, "_model_from_env", fake_model)
    monkeypatch.setenv("RIVUMI_CONFIG", str(tmp_path / "missing.json"))

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

    monkeypatch.setattr("rivumi.models.OpenAICompatibleModel", fake_model)

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

    monkeypatch.setattr("rivumi.models.OpenAICompatibleModel", fake_model)
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


@pytest.mark.parametrize(
    ("provider", "env_var", "base_url"),
    [
        ("openrouter", "OPENROUTER_API_KEY", "https://openrouter.ai/api/v1"),
        ("deepseek", "DEEPSEEK_API_KEY", "https://api.deepseek.com"),
        ("groq", "GROQ_API_KEY", "https://api.groq.com/openai/v1"),
        ("moonshotai", "MOONSHOT_API_KEY", "https://api.moonshot.ai/v1"),
        ("zai", "ZAI_API_KEY", "https://api.z.ai/api/coding/paas/v4"),
        ("xai", "XAI_API_KEY", "https://api.x.ai/v1"),
        ("nvidia-nim", "NVIDIA_API_KEY", "https://integrate.api.nvidia.com/v1"),
        ("opencode-zen", "OPENCODE_ZEN_API_KEY", "https://opencode.ai/zen/v1"),
        ("ollama-cloud", "OLLAMA_CLOUD_API_KEY", "https://ollama.com/v1"),
    ],
)
def test_new_openai_compatible_providers_build_and_hint(
    provider: str, env_var: str, base_url: str, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv(env_var, raising=False)

    assert env_var in (cli._credential_hint(provider) or "")
    with pytest.raises(typer.BadParameter, match=env_var):
        cli._model_from_env(
            provider=provider,
            model="test-model",
            base_url=None,
            tool_calling=True,
            allow_custom_provider_endpoint=False,
        )

    monkeypatch.setenv(env_var, "test-key")
    assert cli._credential_hint(provider) is None
    model = cli._model_from_env(
        provider=provider,
        model="test-model",
        base_url=None,
        tool_calling=True,
        allow_custom_provider_endpoint=False,
    )
    assert model.provider_name == provider
    assert model.model_id == "test-model"
    assert str(model._client.base_url).rstrip("/") == base_url


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

    monkeypatch.setattr("rivumi.claude_backend.ClaudeCodeBackend", FakeBackend)
    monkeypatch.setattr("rivumi.external_runner.ExternalCodingRunner", FakeRunner)

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
            assert originator == "rivumi"
            return SimpleNamespace(url="https://example.test/login", state="state")

        async def aclose(self) -> None:
            nonlocal closed
            closed = True

    monkeypatch.setattr("rivumi.codex_oauth.CodexOAuthClient", FakeOAuth)
    monkeypatch.setattr(cli.webbrowser, "open", lambda _: True)

    def fail_after_listening(_, *, on_listening, **__) -> None:
        on_listening(("127.0.0.1", 1455))
        raise TimeoutError("callback timed out")

    monkeypatch.setattr(
        "rivumi.oauth_login.wait_for_codex_callback",
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

    monkeypatch.setattr("rivumi.codex_oauth.CodexOAuthClient", FakeOAuth)
    monkeypatch.setattr(cli.webbrowser, "open", open_browser)
    monkeypatch.setattr("rivumi.oauth_login.wait_for_codex_callback", fail_after_listening)

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
        return "http://localhost:1455/auth/callback?code=short-lived&state=expected"

    monkeypatch.setattr("rivumi.codex_oauth.CodexOAuthClient", FakeOAuth)
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


def _stub_verification(monkeypatch, *, ok: bool, message: str = "") -> None:
    from rivumi.provider_verification import VerificationResult

    async def fake_verify(provider, fields, **_kwargs):
        return VerificationResult(ok=ok, message=message or f"{provider} check")

    monkeypatch.setattr("rivumi.provider_verification.verify_native_credential", fake_verify)


def test_auth_set_key_prompts_hidden_and_persists_0600(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    _stub_verification(monkeypatch, ok=True)

    result = CliRunner().invoke(cli.app, ["auth", "set-key", "anthropic"], input="sk-secret\n")

    assert result.exit_code == 0, result.output
    assert "sk-secret" not in result.output
    from rivumi.native_credentials import native_credential_path, resolve_native_field

    path = native_credential_path("anthropic")
    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert resolve_native_field("anthropic", "api_key") == "sk-secret"


def test_auth_set_key_prints_verification_success(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    _stub_verification(monkeypatch, ok=True)

    result = CliRunner().invoke(cli.app, ["auth", "set-key", "anthropic"], input="sk-secret\n")

    assert result.exit_code == 0, result.output
    assert "✓" in result.output
    assert "verified" in result.output


def test_auth_set_key_prints_verification_failure_but_still_saves(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    _stub_verification(monkeypatch, ok=False, message="anthropic rejected the credential (401).")

    result = CliRunner().invoke(cli.app, ["auth", "set-key", "anthropic"], input="sk-bad\n")

    assert result.exit_code == 0, result.output
    assert "⚠" in result.output
    assert "rejected the credential (401)" in result.output
    from rivumi.native_credentials import resolve_native_field

    assert resolve_native_field("anthropic", "api_key") == "sk-bad"


def test_auth_set_key_rejects_unknown_provider() -> None:
    result = CliRunner().invoke(cli.app, ["auth", "set-key", "made-up"])

    assert result.exit_code != 0
    assert "must be one of" in result.output


def test_auth_clear_key_removes_stored_credential(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    from rivumi.native_credentials import save_native_credential

    save_native_credential("gemini", {"api_key": "sk-secret"})

    cleared = CliRunner().invoke(cli.app, ["auth", "clear-key", "gemini"])
    cleared_again = CliRunner().invoke(cli.app, ["auth", "clear-key", "gemini"])

    assert cleared.exit_code == 0
    assert "Cleared" in cleared.output
    assert cleared_again.exit_code == 0
    assert "No stored" in cleared_again.output


def test_auth_list_default_does_not_call_network(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    from rivumi.native_credentials import save_native_credential

    save_native_credential("gemini", {"api_key": "sk-secret"})

    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("auth list without --verify must not call the network")

    monkeypatch.setattr("rivumi.provider_verification.verify_native_credential", fail_if_called)

    result = CliRunner().invoke(cli.app, ["auth", "list"])

    assert result.exit_code == 0, result.output
    assert "gemini" in result.output
    assert "not verified this run" in result.output


def test_auth_list_shows_not_set_saved_and_verified_states(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    from rivumi.native_credentials import save_native_credential
    from rivumi.provider_verification import VerificationResult

    save_native_credential("gemini", {"api_key": "good-key"})
    save_native_credential("groq", {"api_key": "bad-key"})

    async def fake_verify(provider, fields, **_kwargs):
        if provider == "gemini":
            return VerificationResult(ok=True, message="Connected to gemini.")
        return VerificationResult(ok=False, message=f"{provider} rejected the credential (401).")

    monkeypatch.setattr("rivumi.provider_verification.verify_native_credential", fake_verify)

    result = CliRunner().invoke(cli.app, ["auth", "list", "--verify"])

    assert result.exit_code == 0, result.output
    assert "✓ gemini" in result.output
    assert "✗ groq" in result.output
    assert "· anthropic" in result.output
    assert "not set" in result.output


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
    from rivumi import tui

    captured: dict[str, object] = {}

    class FakeApp:
        final_transcript_text = ""

        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)
            self.last_error = None

        def run(self):
            captured["ran"] = True
            return None

    monkeypatch.setenv("RIVUMI_CONFIG", str(tmp_path / "missing-config.json"))
    monkeypatch.setattr(cli, "_terminal_supports_tui", lambda: True)
    monkeypatch.setattr(cli, "_discover_local_ollama_models", lambda: ("qwen3:4b",))
    monkeypatch.setattr(tui, "RivumiApp", FakeApp)

    result = CliRunner().invoke(
        cli.app,
        ["-C", str(tiny_bug_repo), "Fix the failing test."],
    )

    assert result.exit_code == 0, result.output
    assert captured["ran"] is True
    assert captured["initial_prompt"] == "Fix the failing test."
    assert captured["repository"] == tiny_bug_repo
    assert captured["ollama_models"] == ("qwen3:4b",)


@pytest.mark.parametrize(
    ("runtime", "model", "session_type"),
    [
        ("claude-code", "sonnet", IsolatedClaudeConversation),
        ("codex-cli", None, IsolatedCodexConversation),
    ],
)
def test_tui_subscription_runtime_routes_to_long_lived_isolated_session(
    tiny_bug_repo: Path,
    tmp_path: Path,
    monkeypatch,
    runtime: str,
    model: str,
    session_type: type,
) -> None:
    from rivumi import tui

    captured: dict[str, object] = {}

    class FakeApp:
        final_transcript_text = ""

        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)
            self.last_error = None

        def run(self):
            return None

    monkeypatch.setenv("RIVUMI_CONFIG", str(tmp_path / "missing-config.json"))
    monkeypatch.setattr(cli, "_terminal_supports_tui", lambda: True)
    monkeypatch.setattr(tui, "RivumiApp", FakeApp)
    monkeypatch.setattr(
        cli,
        "_model_from_env",
        lambda **_: (_ for _ in ()).throw(AssertionError("API adapter must not run")),
    )

    result = CliRunner().invoke(cli.app, ["-C", str(tiny_bug_repo)])
    assert result.exit_code == 0, result.output

    request = tui.TuiRunRequest(
        repository=tiny_bug_repo,
        instruction="Fix the failing test",
        runtime=runtime,
        provider=None,
        model=model,
        api_url=None,
    )
    runner, resource = captured["runner_factory"](request, None, None)

    assert isinstance(resource, ConversationController)
    assert runner.controller is resource
    assert isinstance(resource.session, session_type)
    assert resource.session.model == model


def test_tui_codex_uses_one_isolated_long_lived_conversation(
    tiny_bug_repo: Path, tmp_path: Path, monkeypatch
) -> None:
    from rivumi import tui

    captured: dict[str, object] = {}

    class FakeApp:
        final_transcript_text = ""

        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)
            self.last_error = None

        def run(self):
            return None

    monkeypatch.setenv("RIVUMI_CONFIG", str(tmp_path / "missing-config.json"))
    monkeypatch.setattr(cli, "_terminal_supports_tui", lambda: True)
    monkeypatch.setattr(tui, "RivumiApp", FakeApp)
    monkeypatch.setattr("rivumi.external_runner.ExternalCodingRunner", None)

    result = CliRunner().invoke(cli.app, ["-C", str(tiny_bug_repo)])
    assert result.exit_code == 0, result.output

    runner, resource = captured["runner_factory"](
        tui.TuiRunRequest(
            repository=tiny_bug_repo,
            instruction="User: hi",
            runtime="codex-cli",
            provider=None,
            model=None,
            api_url=None,
            mode="ask",
        ),
        None,
        None,
    )

    assert resource is not None
    assert isinstance(resource, ConversationController)
    assert runner.controller is resource
    assert isinstance(resource.session, IsolatedCodexConversation)


def test_acquire_native_controller_reuses_open_and_recreates_closed(
    tmp_path: Path,
) -> None:
    cache: dict = {}
    identity = ("codex-cli", tmp_path, None, None)
    adapter = cli.runtime_registry.RUNTIME_REGISTRY["codex-cli"]
    first = cli._acquire_native_controller(
        cache, identity, adapter=adapter, repository=tmp_path, model=None
    )
    assert isinstance(first, ConversationController)
    assert not first.is_closed
    assert cache[identity] is first

    # An open controller is reused instead of rebuilt.
    again = cli._acquire_native_controller(
        cache, identity, adapter=adapter, repository=tmp_path, model=None
    )
    assert again is first

    # A controller that closed itself after a failed turn is discarded so the
    # next run rebuilds a fresh conversation instead of failing forever.
    first._closed = True
    rebuilt = cli._acquire_native_controller(
        cache, identity, adapter=adapter, repository=tmp_path, model=None
    )
    assert rebuilt is not first
    assert not rebuilt.is_closed
    assert rebuilt.is_closed is False
    assert cache[identity] is rebuilt
    assert first.is_closed is True


def test_rebuild_controller_after_failure(tmp_path: Path, monkeypatch) -> None:
    from rivumi import tui

    captured: dict[str, object] = {}

    class FakeApp:
        final_transcript_text = ""

        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)
            self.last_error = None

        def run(self) -> None:
            return None

    monkeypatch.setenv("RIVUMI_CONFIG", str(tmp_path / "missing-config.json"))
    monkeypatch.setattr(cli, "_terminal_supports_tui", lambda: True)
    monkeypatch.setattr(tui, "RivumiApp", FakeApp)
    monkeypatch.setattr("rivumi.external_runner.ExternalCodingRunner", None)

    result = CliRunner().invoke(cli.app, ["-C", str(tmp_path)])
    assert result.exit_code == 0, result.output

    factory = captured["runner_factory"]
    request = tui.TuiRunRequest(
        repository=tmp_path,
        instruction="hi",
        runtime="codex-cli",
        provider=None,
        model=None,
        api_url=None,
    )
    _runner, resource = factory(request, None, None)
    first_controller = resource
    first_controller._closed = True
    _runner2, resource2 = factory(request, None, None)
    assert resource2 is not first_controller
    assert isinstance(resource2, ConversationController)
    assert not resource2.is_closed


def test_plain_flag_never_launches_full_screen_tui(
    tiny_bug_repo: Path, tmp_path: Path, monkeypatch
) -> None:
    from rivumi import tui

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
        "RivumiApp",
        lambda **_: (_ for _ in ()).throw(AssertionError("TUI must not launch")),
    )
    monkeypatch.setattr(
        cli,
        "_model_from_env",
        lambda **_: ScriptedModel([ModelTurn(content="unused")]),
    )
    monkeypatch.setattr("rivumi.loop.AgentRunner", FakeRunner)

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


def test_startup_tracer_is_noop_when_disabled(tmp_path: Path, monkeypatch) -> None:
    from rivumi.startup_trace import _StartupTracer

    log = tmp_path / "startup.jsonl"
    tracer = _StartupTracer(None)
    assert tracer.enabled is False
    with tracer.span("config.load"):
        pass
    assert not log.exists()


def test_startup_tracer_emits_config_load_span(
    tmp_path: Path, monkeypatch
) -> None:
    from rivumi.startup_trace import _StartupTracer

    log = tmp_path / "startup.jsonl"
    monkeypatch.setattr(cli, "_STARTUP", _StartupTracer(str(log)))

    result = CliRunner().invoke(cli.app, ["config"])

    assert result.exit_code == 0, result.output
    spans = [line for line in log.read_text().splitlines() if line.strip()]
    assert spans, "expected at least one startup span"
    assert any("config.load" in line for line in spans)
    assert all(line.startswith("{") and line.endswith("}") for line in spans)


def test_discover_ollama_models_is_cached(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    fetches = []

    def fake_fetch() -> tuple[str, ...]:
        fetches.append(1)
        return ("llama3:8b",)

    monkeypatch.setattr(cli, "_fetch_ollama_models", fake_fetch)
    first = cli._discover_local_ollama_models()
    second = cli._discover_local_ollama_models()
    assert first == second == ("llama3:8b",)
    assert fetches == [1]

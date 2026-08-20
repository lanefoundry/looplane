from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from coding_agent import cli
from coding_agent.approvals import HeadlessApprovalPolicy
from coding_agent.contracts import ModelTurn, ToolCall
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
    assert "run" in result.output


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
    monkeypatch.setattr(
        cli,
        "wait_for_codex_callback",
        lambda _: (_ for _ in ()).throw(TimeoutError("callback timed out")),
    )

    result = CliRunner().invoke(cli.app, ["auth", "login-codex"])

    assert result.exit_code == 2
    assert closed is True

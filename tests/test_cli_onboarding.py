from __future__ import annotations

import json
import stat
from pathlib import Path

import httpx
import pytest
from conftest import plain_cli_output
from typer.testing import CliRunner

from looplane import cli
from looplane.cli_config import CliConfig, load_cli_config
from looplane.contracts import ModelTurn, RunResult, RunStatus
from looplane.models import ScriptedModel


def test_discovers_bounded_unique_models_from_loopback_ollama(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    entries = [{"name": "qwen3:4b"}, {"model": "qwen3:4b"}, {"name": "qwen3:0.6b"}]
    original_client = httpx.Client
    client = original_client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"models": entries}, request=request)
        )
    )
    monkeypatch.setattr(httpx, "Client",lambda **_: client)

    assert cli._discover_local_ollama_models() == ("qwen3:4b", "qwen3:0.6b")


def test_ollama_discovery_ignores_proxy_env_and_control_characters(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    entries = [
        {"name": " qwen3:4b "},
        {"name": "bad\x1b[2Jname"},
        {"name": "bad\nname"},
    ]
    original_client = httpx.Client
    captured: dict[str, object] = {}
    client = original_client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"models": entries}, request=request)
        )
    )

    def fake_client(**kwargs):
        captured.update(kwargs)
        return client

    monkeypatch.setattr(httpx, "Client",fake_client)

    assert cli._discover_local_ollama_models() == ("qwen3:4b",)
    assert captured["trust_env"] is False
    assert captured["headers"] == {"Accept-Encoding": "identity"}


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503),
        httpx.Response(200, content=b"not-json"),
        httpx.Response(200, content=b"x" * (cli.MAX_OLLAMA_TAGS_BYTES + 1)),
    ],
)
def test_ollama_discovery_fails_closed_on_bad_or_oversized_response(
    response: httpx.Response, monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    original_client = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            response.status_code,
            content=response.content,
            request=request,
        )

    client = original_client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(httpx, "Client",lambda **_: client)

    assert cli._discover_local_ollama_models() == ()


def test_first_time_setup_selects_ollama_model_and_saves_private_config(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    path = tmp_path / "config.json"
    answers = iter(("1", "1"))
    monkeypatch.setenv("LOOPLANE_CONFIG", str(path))
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(
        cli,
        "_discover_local_ollama_models",
        lambda: ("qwen3:4b", "qwen3:0.6b"),
    )
    monkeypatch.setattr(cli.typer, "prompt", lambda *_args, **_kwargs: next(answers))

    configured = cli._interactive_setup()

    assert configured == CliConfig(runtime="looplane-agent", provider="ollama", model="qwen3:4b")
    assert load_cli_config(path) == configured
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "qwen3:4b" in capsys.readouterr().out
    assert "api_key" not in path.read_text()


def test_cancelled_setup_never_writes_partial_config(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "config.json"
    monkeypatch.setenv("LOOPLANE_CONFIG", str(path))
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_discover_local_ollama_models", lambda: ("qwen3:4b",))
    monkeypatch.setattr(
        cli.typer,
        "prompt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt),
    )

    with pytest.raises(KeyboardInterrupt):
        cli._interactive_setup()

    assert not path.exists()


def test_non_tty_missing_model_has_actionable_setup_error(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "missing.json"
    monkeypatch.setenv("LOOPLANE_CONFIG", str(path))
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)

    result = CliRunner().invoke(cli.app, ["-p", "Explain this repository"])

    assert result.exit_code == 2
    output = plain_cli_output(result)
    assert "looplane config --interactive" in output
    assert "--provider PROVIDER --model MODEL" in output
    assert not path.exists()


def test_config_interactive_wires_current_config_into_setup(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"provider": "ollama", "model": "qwen3:4b"}))
    captured: dict[str, CliConfig] = {}
    monkeypatch.setenv("LOOPLANE_CONFIG", str(path))

    def fake_setup(*, current: CliConfig, locked_provider: str | None = None) -> CliConfig:
        captured["current"] = current
        assert locked_provider is None
        return current

    monkeypatch.setattr(cli, "_interactive_setup", fake_setup)

    result = CliRunner().invoke(cli.app, ["config", "--interactive"])

    assert result.exit_code == 0, result.output
    assert captured["current"] == CliConfig(provider="ollama", model="qwen3:4b")


def test_configured_bare_cli_shows_context_and_natural_task_prompt(
    tiny_bug_repo: Path, tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"provider": "ollama", "model": "qwen3:4b"}))
    captured: dict[str, object] = {}

    class FakeRunner:
        def __init__(self, task, _model, _run_root, **_kwargs) -> None:
            captured["task"] = task

        async def run(self):
            return RunResult(
                run_id="onboarding-run",
                task_id="onboarding-task",
                status=RunStatus.COMPLETED,
                summary="answered",
                terminal_reason="verified",
                artifacts={"patch": str(tmp_path / "changes.patch")},
            )

    monkeypatch.setenv("LOOPLANE_CONFIG", str(path))
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(
        cli,
        "_interactive_setup",
        lambda **_: (_ for _ in ()).throw(AssertionError("setup must not run")),
    )
    monkeypatch.setattr(
        cli,
        "_model_from_env",
        lambda **_: ScriptedModel([ModelTurn(content="unused")]),
    )
    monkeypatch.setattr("looplane.loop.AgentRunner", FakeRunner)

    result = CliRunner().invoke(
        cli.app,
        ["-C", str(tiny_bug_repo), "--run-root", str(tmp_path / "runs")],
        input="Explain what you can do.\n",
    )

    assert result.exit_code == 0, result.output
    assert "looplane  ·  ollama/qwen3:4b  ·" in result.output
    assert "What would you like me to do in this repository?" in result.output
    assert "Model:" not in result.output
    assert captured["task"].instruction == "Explain what you can do."


def test_positional_prompt_is_retained_after_first_time_setup(
    tiny_bug_repo: Path, tmp_path: Path, monkeypatch
) -> None:
    captured: dict[str, object] = {}

    class FakeRunner:
        def __init__(self, task, _model, _run_root, **_kwargs) -> None:
            captured["task"] = task

        async def run(self):
            return RunResult(
                run_id="first-run",
                task_id="first-task",
                status=RunStatus.COMPLETED,
                summary="done",
                terminal_reason="verified",
                artifacts={"patch": str(tmp_path / "changes.patch")},
            )

    monkeypatch.setenv("LOOPLANE_CONFIG", str(tmp_path / "missing.json"))
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(
        cli,
        "_interactive_setup",
        lambda **_: CliConfig(provider="ollama", model="qwen3:4b"),
    )
    monkeypatch.setattr(
        cli,
        "_model_from_env",
        lambda **_: ScriptedModel([ModelTurn(content="unused")]),
    )
    monkeypatch.setattr("looplane.loop.AgentRunner", FakeRunner)

    result = CliRunner().invoke(
        cli.app,
        ["-C", str(tiny_bug_repo), "Explain this repository"],
    )

    assert result.exit_code == 0, result.output
    assert captured["task"].instruction == "Explain this repository"


def test_print_mode_never_runs_setup_when_tty_is_attached(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "missing.json"
    monkeypatch.setenv("LOOPLANE_CONFIG", str(path))
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(
        cli,
        "_interactive_setup",
        lambda **_: (_ for _ in ()).throw(AssertionError("setup must not run")),
    )

    result = CliRunner().invoke(cli.app, ["-p", "Explain this repository"])

    assert result.exit_code == 2
    assert "looplane config --interactive" in plain_cli_output(result)


def test_print_mode_never_prompts_for_missing_task_on_tty(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"provider": "ollama", "model": "qwen3:4b"}))
    monkeypatch.setenv("LOOPLANE_CONFIG", str(path))
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(
        cli.typer,
        "prompt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not prompt")),
    )

    result = CliRunner().invoke(cli.app, ["-p"])

    assert result.exit_code == 2
    assert "PROMPT is required in non-interactive mode" in result.output


def test_explicit_provider_is_locked_during_setup(
    tiny_bug_repo: Path, tmp_path: Path, monkeypatch
) -> None:
    captured: dict[str, object] = {}

    def fake_setup(*, current: CliConfig, locked_provider: str | None = None) -> CliConfig:
        captured["current"] = current
        captured["locked_provider"] = locked_provider
        return CliConfig(provider="anthropic", model="claude-sonnet-4-5")

    monkeypatch.setenv("LOOPLANE_CONFIG", str(tmp_path / "missing.json"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_interactive_setup", fake_setup)
    monkeypatch.setattr(
        cli,
        "_model_from_env",
        lambda **_: ScriptedModel([ModelTurn(content="unused")]),
    )
    monkeypatch.setattr(
        "looplane.loop.AgentRunner",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("stop after setup")),
    )

    result = CliRunner().invoke(
        cli.app,
        ["--provider", "anthropic", "-C", str(tiny_bug_repo), "Explain this"],
    )

    assert isinstance(result.exception, RuntimeError)
    assert captured["locked_provider"] == "anthropic"
    assert captured["current"] == CliConfig(provider="anthropic")


@pytest.mark.parametrize(
    "present",
    ["CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN"],
)
def test_workers_ai_hint_requires_both_credentials(
    present: str, monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    monkeypatch.setenv(present, "configured")

    assert "CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN" in (
        cli._credential_hint("workers-ai") or ""
    )

"""Command-line entrypoint for interactive and headless coding-agent runs."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys
import webbrowser
from pathlib import Path
from typing import Annotated

import typer
import uvicorn

from coding_agent.approvals import TTYApprovalPolicy
from coding_agent.codex_oauth import (
    CodexCredentialManager,
    CodexCredentialStore,
    CodexOAuthClient,
    OpenAICodexResponsesModel,
)
from coding_agent.console import ConsoleEventSink, LiveEventProjection
from coding_agent.contracts import Limits, RunResult, TaskContract, VerificationCommand
from coding_agent.gateway import ModelGateway
from coding_agent.loop import AgentRunner, UnsafeLocalExecutionError
from coding_agent.models import (
    AnthropicModel,
    GeminiModel,
    ModelProvider,
    OpenAICompatibleModel,
    ProviderError,
    WorkersAIModel,
)
from coding_agent.oauth_login import wait_for_codex_callback
from coding_agent.session import SessionStore, SessionValidationError

app = typer.Typer(
    no_args_is_help=False,
    invoke_without_command=True,
    help="Interactive coding agent with a bounded headless harness.",
)
auth_app = typer.Typer(help="Manage provider credentials owned by this application.")
app.add_typer(auth_app, name="auth")


def _default_run_root() -> Path:
    configured = os.environ.get("PCA_RUN_ROOT")
    if configured:
        return Path(configured)
    state_root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return state_root / "python-coding-agent" / "runs"


DEFAULT_RUN_ROOT = _default_run_root()
DEFAULT_REPOSITORY = Path.cwd()


def _codex_credential_path() -> Path:
    state_root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return state_root / "python-coding-agent" / "auth" / "openai-codex.json"


def _commands(values: list[str] | None) -> tuple[VerificationCommand, ...]:
    configured = values or ["git diff --check"]
    return tuple(
        VerificationCommand(name=f"check-{index}", argv=tuple(shlex.split(value)))
        for index, value in enumerate(configured, 1)
    )


def _show_result(result: RunResult) -> None:
    status = result.status.value
    typer.echo(f"\n{status}: {result.summary}")
    typer.echo(f"session: {result.run_id}")
    typer.echo(f"patch: {result.artifacts['patch']}")


@app.callback(invoke_without_command=True)
def main(
    context: typer.Context,
    repository: Annotated[
        Path, typer.Option("--repo", exists=True, file_okay=False)
    ] = DEFAULT_REPOSITORY,
    instruction: Annotated[str | None, typer.Option("--task", "-t")] = None,
    provider: Annotated[
        str, typer.Option("--provider", "-p", envvar="PCA_PROVIDER")
    ] = "openai-compatible",
    model: Annotated[
        str | None, typer.Option("--model", "-m", envvar="CODING_AGENT_MODEL")
    ] = None,
    check: Annotated[
        list[str] | None, typer.Option("--check", help="Exact verification argv; repeatable")
    ] = None,
    api_url: Annotated[
        str | None,
        typer.Option("--api-url", envvar="PCA_API_URL", help="Provider or proxy API URL"),
    ] = None,
    run_root: Annotated[Path, typer.Option("--run-root")] = DEFAULT_RUN_ROOT,
    experimental_subscription: Annotated[
        bool,
        typer.Option(
            "--experimental-subscription",
            help="Enable the separately authenticated experimental ChatGPT/Codex transport.",
        ),
    ] = False,
) -> None:
    """Start this agent's own interactive loop when no subcommand is supplied."""

    if context.invoked_subcommand is not None:
        return
    if not sys.stdin.isatty() and instruction is None:
        raise typer.BadParameter("--task is required when stdin is not interactive")
    instruction = instruction or typer.prompt("What should I change?")
    if not model:
        if not sys.stdin.isatty():
            raise typer.BadParameter("--model is required when stdin is not interactive")
        model = typer.prompt("Model")
    try:
        selected_model = _model_from_env(
            provider=provider,
            model=model,
            base_url=api_url,
            tool_calling=True,
            allow_custom_provider_endpoint=api_url is not None,
            experimental_subscription=experimental_subscription,
        )
        task = TaskContract(
            repository=repository,
            instruction=instruction,
            allowed_paths=("**",),
            verification=_commands(check),
        )
        result = asyncio.run(
            _run_and_close(
                AgentRunner(
                    task,
                    selected_model,
                    run_root,
                    approval_policy=TTYApprovalPolicy(sys.stdin, sys.stderr),
                    event_sink=ConsoleEventSink(sys.stderr),
                ),
                selected_model,
            )
        )
    except (UnsafeLocalExecutionError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    _show_result(result)
    if result.status != "completed":
        raise typer.Exit(code=1)


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise typer.BadParameter(f"required environment variable is missing: {name}")
    return value


def _model_from_env(
    *,
    provider: str,
    model: str,
    base_url: str | None,
    tool_calling: bool,
    allow_custom_provider_endpoint: bool,
    experimental_subscription: bool = False,
) -> ModelProvider:
    if provider == "openai-compatible":
        return OpenAICompatibleModel(
            model=model,
            api_key=os.environ.get("OPENAI_API_KEY"),
            base_url=base_url or os.environ.get("OPENAI_BASE_URL"),
            supports_tool_calling=tool_calling,
        )
    if provider == "ollama":
        return OpenAICompatibleModel(
            model=model,
            base_url=base_url or os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434/v1"),
            supports_tool_calling=tool_calling,
            provider_name="ollama",
            extra_body={"think": False},
            # Qwen can spend more than 1K generated tokens on hidden reasoning before it emits
            # a tool call even with no-think hints. Keep a finite bound, but avoid turning each
            # useful action into several truncated agent steps.
            max_tokens=4_096,
            user_message_prefix="/no_think\n",
        )
    if provider == "openai-codex":
        oauth = CodexOAuthClient()
        manager = CodexCredentialManager(
            CodexCredentialStore(_codex_credential_path()),
            oauth,
        )
        return OpenAICodexResponsesModel(
            model=model,
            credentials=manager,
            experimental=experimental_subscription,
        )
    if provider == "anthropic":
        return AnthropicModel(
            model=model,
            api_key=_required_env("ANTHROPIC_API_KEY"),
            base_url=base_url or os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
            supports_tool_calling=tool_calling,
            allow_custom_endpoint=allow_custom_provider_endpoint,
        )
    if provider == "gemini":
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise typer.BadParameter(
                "required environment variable is missing: GEMINI_API_KEY or GOOGLE_API_KEY"
            )
        return GeminiModel(
            model=model,
            api_key=api_key,
            base_url=base_url or "https://generativelanguage.googleapis.com/v1beta",
            supports_tool_calling=tool_calling,
            allow_custom_endpoint=allow_custom_provider_endpoint,
        )
    if provider == "workers-ai":
        return WorkersAIModel(
            account_id=_required_env("CLOUDFLARE_ACCOUNT_ID"),
            api_token=_required_env("CLOUDFLARE_API_TOKEN"),
            model=model,
            base_url=base_url or "https://api.cloudflare.com/client/v4",
            supports_tool_calling=tool_calling,
            allow_custom_endpoint=allow_custom_provider_endpoint,
        )
    raise typer.BadParameter(f"unsupported provider: {provider}")


@auth_app.command("login-codex")
def login_codex() -> None:
    """Create this application's own experimental ChatGPT/Codex OAuth grant."""

    oauth = CodexOAuthClient()
    exchange_started = False
    try:
        authorization = oauth.begin_login(originator="python-coding-agent")
        typer.echo("Opening the ChatGPT/Codex authorization page in your browser.")
        typer.echo("This creates a separate grant; it does not read the Codex CLI credential.")
        if not webbrowser.open(authorization.url):
            typer.echo(authorization.url)
        code = wait_for_codex_callback(authorization)
        exchange_started = True
        credentials = asyncio.run(_exchange_codex_code(oauth, code, authorization.verifier))
        CodexCredentialStore(_codex_credential_path()).save(credentials)
    except (OSError, ProviderError, TimeoutError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    finally:
        if not exchange_started:
            asyncio.run(oauth.aclose())
    typer.echo("ChatGPT/Codex authorization saved for python-coding-agent.")


async def _exchange_codex_code(
    oauth: CodexOAuthClient,
    code: str,
    verifier: str,
):
    try:
        return await oauth.exchange_code(code=code, verifier=verifier)
    finally:
        await oauth.aclose()


def _resolve_resume_dir(run_root: Path, session: str) -> Path:
    root = run_root.resolve(strict=True)
    if session != "last":
        candidate = (root / session).resolve(strict=True)
        if candidate.parent != root or candidate.name != session:
            raise typer.BadParameter("session must be 'last' or one safe run id")
        return candidate
    candidates: list[Path] = []
    for path in root.glob("*/session.json"):
        if not path.is_file() or path.parent.is_symlink():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("terminal") is False:
            candidates.append(path.parent)
    if not candidates:
        raise typer.BadParameter("no persisted sessions were found")
    return max(candidates, key=lambda path: (path / "session.json").stat().st_mtime_ns)


@app.command()
def resume(
    session: Annotated[str, typer.Argument(help="Session id or 'last'")] = "last",
    run_root: Annotated[Path, typer.Option("--run-root")] = DEFAULT_RUN_ROOT,
    api_url: Annotated[
        str | None, typer.Option("--api-url", envvar="PCA_API_URL")
    ] = None,
    experimental_subscription: Annotated[
        bool, typer.Option("--experimental-subscription")
    ] = False,
) -> None:
    """Resume a validated non-terminal session in its existing disposable workspace."""

    try:
        run_dir = _resolve_resume_dir(run_root, session)
        manifest = asyncio.run(SessionStore(run_dir).load())
        selected_model = _model_from_env(
            provider=manifest.provider_name,
            model=manifest.model_id,
            base_url=api_url,
            tool_calling=True,
            allow_custom_provider_endpoint=api_url is not None,
            experimental_subscription=experimental_subscription,
        )
        projection = LiveEventProjection(
            run_id=manifest.run_id,
            last_sequence=manifest.last_event_sequence,
        )
        result = asyncio.run(
            _resume_and_close(
                run_dir,
                selected_model,
                approval_policy=TTYApprovalPolicy(sys.stdin, sys.stderr),
                event_sink=ConsoleEventSink(sys.stderr, projection),
            )
        )
    except (OSError, SessionValidationError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    _show_result(result)
    if result.status != "completed":
        raise typer.Exit(code=1)


@app.command("gateway")
def serve_gateway(
    model: Annotated[str, typer.Option("--model", "-m", envvar="CODING_AGENT_MODEL")],
    provider: Annotated[
        str, typer.Option("--provider", "-p", envvar="PCA_PROVIDER")
    ] = "openai-compatible",
    api_url: Annotated[
        str | None, typer.Option("--api-url", envvar="PCA_API_URL")
    ] = None,
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", min=1, max=65535)] = 8787,
    bearer_token: Annotated[
        str | None, typer.Option("--bearer-token", envvar="PCA_GATEWAY_TOKEN")
    ] = None,
    experimental_subscription: Annotated[
        bool, typer.Option("--experimental-subscription")
    ] = False,
) -> None:
    """Expose one configured provider through a bounded OpenAI Chat gateway."""

    if host not in {"localhost", "127.0.0.1", "::1"}:
        raise typer.BadParameter(
            "the MVP gateway only binds loopback; put an authenticated TLS proxy in front later"
        )
    selected_model = _model_from_env(
        provider=provider,
        model=model,
        base_url=api_url,
        tool_calling=True,
        allow_custom_provider_endpoint=api_url is not None,
        experimental_subscription=experimental_subscription,
    )
    gateway = ModelGateway(selected_model, bearer_token=bearer_token)
    uvicorn.run(gateway, host=host, port=port, lifespan="on")


@app.command()
def run(
    repository: Annotated[Path, typer.Option("--repo", exists=True, file_okay=False)],
    instruction: Annotated[str, typer.Option("--task", help="Bounded coding task")],
    model: Annotated[str, typer.Option("--model", envvar="CODING_AGENT_MODEL")],
    check: Annotated[
        list[str], typer.Option("--check", help="Exact verification argv; repeatable")
    ],
    provider: Annotated[
        str,
        typer.Option(
            "--provider",
            help=(
                "openai-compatible, ollama, openai-codex, anthropic, gemini, "
                "or workers-ai"
            ),
        ),
    ] = "openai-compatible",
    allowed_path: Annotated[
        list[str] | None,
        typer.Option("--allowed-path", help="Allowed repository glob; repeatable"),
    ] = None,
    base_sha: Annotated[str | None, typer.Option("--base-sha")] = None,
    base_url: Annotated[str | None, typer.Option("--base-url")] = None,
    run_root: Annotated[Path, typer.Option("--run-root")] = DEFAULT_RUN_ROOT,
    max_steps: Annotated[int, typer.Option("--max-steps", min=1)] = 12,
    wall_time_seconds: Annotated[float, typer.Option("--wall-time", min=1)] = 900,
    tool_calling: Annotated[
        bool,
        typer.Option(
            "--tool-calling",
            help="Assert that the configured provider model/API supports tool calling.",
        ),
    ] = False,
    allow_custom_provider_endpoint: Annotated[
        bool,
        typer.Option(
            "--allow-custom-provider-endpoint",
            help="Allow native-provider credentials to be sent to a non-official HTTPS host.",
        ),
    ] = False,
    experimental_subscription: Annotated[
        bool, typer.Option("--experimental-subscription")
    ] = False,
    unsafe_local_exec: Annotated[
        bool,
        typer.Option(
            "--unsafe-local-exec",
            help="Acknowledge that checks execute trusted repository code without a sandbox.",
        ),
    ] = False,
) -> None:
    """Run one local task; provider credentials are read only from environment variables."""

    commands = _commands(check)
    task = TaskContract(
        repository=repository,
        instruction=instruction,
        allowed_paths=tuple(allowed_path or ("**",)),
        verification=commands,
        limits=Limits(max_steps=max_steps, wall_time_seconds=wall_time_seconds),
        base_sha=base_sha,
    )
    selected_model = _model_from_env(
        provider=provider,
        model=model,
        base_url=base_url,
        tool_calling=tool_calling,
        allow_custom_provider_endpoint=allow_custom_provider_endpoint,
        experimental_subscription=experimental_subscription,
    )
    try:
        result = asyncio.run(
            _run_and_close(
                AgentRunner(
                    task,
                    selected_model,
                    run_root,
                    allow_unsafe_local_exec=unsafe_local_exec,
                ),
                selected_model,
            )
        )
    except (UnsafeLocalExecutionError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(result.model_dump_json(indent=2))
    if result.status != "completed":
        raise typer.Exit(code=1)


async def _run_and_close(runner: AgentRunner, model: ModelProvider):
    try:
        return await runner.run()
    finally:
        await model.aclose()


async def _resume_and_close(
    run_dir: Path,
    model: ModelProvider,
    *,
    approval_policy: TTYApprovalPolicy,
    event_sink: ConsoleEventSink,
):
    try:
        runner = await AgentRunner.resume(
            run_dir,
            model,
            approval_policy=approval_policy,
            event_sink=event_sink,
        )
        return await runner.run()
    finally:
        await model.aclose()


if __name__ == "__main__":
    app()

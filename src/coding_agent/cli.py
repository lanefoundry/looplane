"""Command-line entrypoint for local patch-only runs."""

from __future__ import annotations

import asyncio
import os
import shlex
import tempfile
from pathlib import Path
from typing import Annotated

import typer

from coding_agent.contracts import Limits, TaskContract, VerificationCommand
from coding_agent.loop import AgentRunner, UnsafeLocalExecutionError
from coding_agent.models import (
    AnthropicModel,
    GeminiModel,
    ModelProvider,
    OpenAICompatibleModel,
    WorkersAIModel,
)

app = typer.Typer(no_args_is_help=True, help="Run a bounded coding agent in a disposable repo.")
DEFAULT_RUN_ROOT = Path(tempfile.gettempdir()) / "python-coding-agent-runs"


@app.callback()
def main() -> None:
    """Python-first, patch-only coding-agent harness."""


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
) -> ModelProvider:
    if provider == "openai-compatible":
        return OpenAICompatibleModel(
            model=model,
            api_key=_required_env("OPENAI_API_KEY"),
            base_url=base_url or os.environ.get("OPENAI_BASE_URL"),
            supports_tool_calling=tool_calling,
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
            help="openai-compatible, anthropic, gemini, or workers-ai",
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
    unsafe_local_exec: Annotated[
        bool,
        typer.Option(
            "--unsafe-local-exec",
            help="Acknowledge that checks execute trusted repository code without a sandbox.",
        ),
    ] = False,
) -> None:
    """Run one local task; provider credentials are read only from environment variables."""

    commands = tuple(
        VerificationCommand(name=f"check-{index}", argv=tuple(shlex.split(value)))
        for index, value in enumerate(check, 1)
    )
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


if __name__ == "__main__":
    app()

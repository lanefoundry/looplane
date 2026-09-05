"""Serve command services."""

from __future__ import annotations

from pathlib import Path

import typer

import looplane.runtime_registry as runtime_registry
from looplane.commands import bootstrap as _bootstrap
from looplane.commands import settings as _settings
from looplane.commands.ports import CommandServices


def cloudflare_providers_apply(
    manifest: Path,
    secrets_env: Path | None = None,
    cloudflare_dir: Path = Path("cloudflare"),
    wrangler_env: str | None = None,
    dry_run: bool = False,
    allow_custom_endpoint: bool = False,
) -> None:
    """Upload all provider keys and deploy their hosted profiles in one batch."""

    from looplane.cloudflare_provider_setup import ProviderSetupError, setup_cloudflare_providers

    try:
        result = setup_cloudflare_providers(
            manifest,
            cloudflare_dir=cloudflare_dir,
            secrets_env_file=secrets_env,
            allow_custom_endpoint=allow_custom_endpoint,
            wrangler_env=wrangler_env,
            dry_run=dry_run,
        )
    except ProviderSetupError as exc:
        typer.echo(f"error: Cloudflare provider setup failed: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    action = "Validated" if result.dry_run else "Applied"
    typer.secho(
        f"✓ {action} {result.profile_count} Cloudflare provider profile(s) in one batch.",
        fg=typer.colors.GREEN,
    )


def serve_gateway(
    model: str | None = None,
    provider: str | None = None,
    api_url: str | None = None,
    host: str = "127.0.0.1",
    port: int = 8787,
    bearer_token: str | None = None,
    experimental_subscription: bool = False,
    allow_custom_provider_endpoint: bool = False,
    *,
    services: CommandServices,
) -> None:
    """Expose one configured provider through a bounded OpenAI Chat gateway."""

    import uvicorn

    from looplane.gateway import ModelGateway

    if host not in {"localhost", "127.0.0.1", "::1"}:
        raise typer.BadParameter(
            "the MVP gateway only binds loopback; put an authenticated TLS proxy in front later"
        )
    provider, model, api_url = _settings._resolve_cli_settings(
        provider=provider, model=model, api_url=api_url, services=services
    )
    if model is None:
        raise typer.BadParameter("--model is required when no config default exists")
    selected_model = services.model_factory(
        provider=provider,
        model=model,
        base_url=api_url,
        tool_calling=True,
        allow_custom_provider_endpoint=allow_custom_provider_endpoint,
        experimental_subscription=experimental_subscription,
    )
    gateway = ModelGateway(selected_model, bearer_token=bearer_token)
    uvicorn.run(gateway, host=host, port=port, lifespan="on")


def serve_conversation_server(
    repository: Path | None = None,
    runtime: str = "codex-cli",
    model: str | None = None,
    host: str = "127.0.0.1",
    port: int = 8788,
    path: str = "/v1/conversation/attach",
) -> None:
    """Expose a native conversation runtime through the WebSocket attach protocol."""

    import uvicorn

    from looplane.conversation_websocket import ConversationWebSocketApp

    if host not in {"localhost", "127.0.0.1", "::1"}:
        raise typer.BadParameter("conversation-server only binds loopback")
    repository = repository or Path.cwd()
    adapter = runtime_registry.RUNTIME_REGISTRY.get(runtime)
    if adapter is None or adapter.native_session is None:
        raise typer.BadParameter("runtime does not expose a native conversation session")
    session = _bootstrap.build_conversation_session(adapter, repository=repository, model=model)
    uvicorn.run(ConversationWebSocketApp(session, path=path), host=host, port=port, lifespan="off")

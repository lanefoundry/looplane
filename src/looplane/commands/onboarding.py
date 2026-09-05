"""Onboarding command services."""

from __future__ import annotations

import asyncio
import json

import typer

from looplane.cli_config import (
    CliConfig,
    default_cli_config_path,
    load_cli_config,
    save_cli_config,
)
from looplane.commands import bootstrap as _bootstrap
from looplane.commands.ports import CommandServices

OLLAMA_TAGS_URL = "http://127.0.0.1:11434/api/tags"


MAX_OLLAMA_TAGS_BYTES = 256 * 1024


ONBOARDING_PROVIDERS = (
    ("ollama", "Ollama local"),
    ("openai-compatible", "OpenAI or compatible API"),
    ("anthropic", "Anthropic API (API key billing)"),
    ("gemini", "Google Gemini API"),
    ("workers-ai", "Cloudflare Workers AI"),
    ("openrouter", "OpenRouter (100+ models, one key)"),
    ("deepseek", "DeepSeek API"),
    ("groq", "Groq (fast inference)"),
    ("moonshotai", "Moonshot AI / Kimi"),
    ("zai", "Z.ai / Zhipu (GLM)"),
    ("xai", "xAI (Grok)"),
    ("nvidia-nim", "NVIDIA NIM (build.nvidia.com)"),
    ("opencode-zen", "OpenCode Zen"),
    ("ollama-cloud", "Ollama Cloud (hosted, not local)"),
)


def _fetch_ollama_models() -> tuple[str, ...]:
    """Return bounded model names from the fixed loopback Ollama discovery endpoint."""

    import httpx

    try:
        with (
            httpx.Client(
                timeout=1.5,
                trust_env=False,
                headers={"Accept-Encoding": "identity"},
            ) as client,
            client.stream("GET", OLLAMA_TAGS_URL) as response,
        ):
            if response.status_code != 200:
                return ()
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > MAX_OLLAMA_TAGS_BYTES:
                    return ()
        payload = json.loads(body)
    except (httpx.HTTPError, OSError, UnicodeError, json.JSONDecodeError):
        return ()
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        return ()
    names: list[str] = []
    for entry in payload["models"][:100]:
        if not isinstance(entry, dict):
            continue
        value = entry.get("model") or entry.get("name")
        if not isinstance(value, str):
            continue
        value = value.strip()
        if value and len(value) <= 256 and value.isprintable():
            names.append(value)
    return tuple(dict.fromkeys(names))


def _discover_local_ollama_models(*, services: CommandServices) -> tuple[str, ...]:
    """Cached, single-flight wrapper around :func:`_fetch_ollama_models`."""

    from looplane.startup_cache import CACHE_SCHEMA_VERSION, cached_scan

    models = cached_scan(
        OLLAMA_TAGS_URL,
        CACHE_SCHEMA_VERSION,
        services.fetch_models,
        ttl_seconds=300.0,
    )
    return tuple(models)


def _choose_provider(*, current: str | None, ollama_models: tuple[str, ...]) -> str:
    typer.secho("\nFirst-time setup", bold=True)
    for index, (slug, label) in enumerate(ONBOARDING_PROVIDERS, 1):
        detail = ""
        if slug == "ollama":
            detail = (
                f"  ({len(ollama_models)} models detected)"
                if ollama_models
                else "  (local service not detected)"
            )
        typer.echo(f"  {index}  {label}{detail}")
    slugs = [slug for slug, _ in ONBOARDING_PROVIDERS]
    preferred = current if current in slugs else ("ollama" if ollama_models else slugs[1])
    default_index = slugs.index(preferred) + 1
    while True:
        answer = typer.prompt("Provider", default=str(default_index)).strip()
        if answer.isdigit() and 1 <= int(answer) <= len(slugs):
            return slugs[int(answer) - 1]
        if answer in slugs:
            return answer
        typer.secho("Choose a listed number or provider name.", fg=typer.colors.YELLOW, err=True)


def _choose_model(*, provider: str, current: str | None, ollama_models: tuple[str, ...]) -> str:
    if provider == "ollama" and ollama_models:
        choices = list(dict.fromkeys(([current] if current else []) + list(ollama_models)))
        typer.secho("\nChoose a model", bold=True)
        for index, name in enumerate(choices, 1):
            typer.echo(f"  {index}  {name}")
        while True:
            answer = typer.prompt("Model", default="1").strip()
            if answer.isdigit() and 1 <= int(answer) <= len(choices):
                return choices[int(answer) - 1]
            if answer in choices:
                return answer
            typer.secho("Choose a listed number or model name.", fg=typer.colors.YELLOW, err=True)
    while True:
        if current:
            answer = typer.prompt("Model ID", default=current).strip()
        else:
            answer = typer.prompt("Model ID").strip()
        if answer and "\x00" not in answer:
            return answer
        typer.secho("Model ID cannot be blank.", fg=typer.colors.YELLOW, err=True)


def _interactive_setup(
    *,
    current: CliConfig | None = None,
    locked_provider: str | None = None,
    services: CommandServices,
) -> CliConfig:
    """Run provider-aware setup and persist no credential material."""

    if not services.stdin_is_tty():
        raise typer.BadParameter("interactive setup requires a TTY")
    current = current or CliConfig()
    ollama_models = services.discover_models()
    if locked_provider is None:
        provider = _choose_provider(current=current.provider, ollama_models=ollama_models)
    else:
        provider = locked_provider
        label = dict(ONBOARDING_PROVIDERS).get(provider, provider)
        typer.secho("\nFirst-time setup", bold=True)
        typer.echo(f"  Provider  {label}")
    prior_model = current.model if current.provider == provider else None
    model = _choose_model(
        provider=provider,
        current=prior_model,
        ollama_models=ollama_models,
    )
    api_url = current.api_url if current.provider == provider else None
    configured = CliConfig(
        runtime="looplane-agent",
        provider=provider,
        model=model,
        api_url=api_url,
        deny_rules=current.deny_rules,
        allow_rules=current.allow_rules,
        sandbox_profile=current.sandbox_profile,
        sandbox_backend=current.sandbox_backend,
        sandbox_read_roots=current.sandbox_read_roots,
    )
    try:
        path = asyncio.run(save_cli_config(configured))
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(f"CLI config could not be saved: {exc}") from exc
    typer.secho(f"✓ Saved non-secret defaults: {provider}/{model}", fg=typer.colors.GREEN)
    typer.echo(f"  {path}")
    if hint := _bootstrap._credential_hint(provider, api_url=api_url):
        typer.secho(f"  {hint}", fg=typer.colors.YELLOW)
    return configured


def configure(
    provider: str | None = None,
    model: str | None = None,
    api_url: str | None = None,
    clear_api_url: bool = False,
    interactive: bool = False,
    *,
    services: CommandServices,
) -> None:
    """Show or update non-secret provider defaults."""

    path = default_cli_config_path()
    try:
        with services.startup.span("config.load"):
            current = load_cli_config(path)
        if interactive:
            if provider is not None or model is not None or api_url is not None or clear_api_url:
                raise typer.BadParameter(
                    "--interactive cannot be combined with config value options"
                )
            services.interactive_setup(current=current)
            return
        if provider is None and model is None and api_url is None and not clear_api_url:
            typer.echo(f"config: {path}")
            typer.echo(f"runtime: {current.runtime or '(automatic)'}")
            typer.echo(f"provider: {current.provider or '(not set)'}")
            typer.echo(f"model: {current.model or '(not set)'}")
            typer.echo(f"api_url: {current.api_url or '(not set)'}")
            typer.echo(f"deny_rules: {len(current.deny_rules)}")
            typer.echo(f"allow_rules: {len(current.allow_rules)}")
            typer.echo(f"sandbox_profile: {current.sandbox_profile or '(default)'}")
            typer.echo(f"sandbox_backend: {current.sandbox_backend or '(auto)'}")
            typer.echo(f"sandbox_read_roots: {len(current.sandbox_read_roots)}")
            return
        provider_changed = provider is not None and provider != current.provider
        updated = CliConfig(
            runtime="looplane-agent",
            runtime_model=None,
            provider=provider if provider is not None else current.provider,
            model=(model if model is not None else (None if provider_changed else current.model)),
            api_url=(
                None
                if clear_api_url or (provider_changed and api_url is None)
                else (api_url if api_url is not None else current.api_url)
            ),
            deny_rules=current.deny_rules,
            allow_rules=current.allow_rules,
            sandbox_profile=current.sandbox_profile,
            sandbox_backend=current.sandbox_backend,
            sandbox_read_roots=current.sandbox_read_roots,
        )
        asyncio.run(save_cli_config(updated, path))
    except (OSError, ValueError) as exc:
        typer.echo(f"error: config could not be saved: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"Saved non-secret defaults to {path}")

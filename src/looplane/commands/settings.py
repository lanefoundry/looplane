"""Settings command services."""

from __future__ import annotations

import typer

import looplane.provider_catalog as provider_catalog
from looplane.cli_config import (
    SUPPORTED_PROVIDERS,
    load_cli_config,
)
from looplane.commands.ports import CommandServices


def _parse_provider_model_spec(spec: str) -> tuple[str, str] | None:
    """Return provider/model for a prefixed model spec when the provider is supported."""

    if "/" not in spec:
        return None
    candidate, model = spec.split("/", 1)
    if candidate in SUPPORTED_PROVIDERS and model:
        return candidate, model
    return None


def _resolve_model_role_alias(
    spec: str, *, provider: str | None = None
) -> tuple[tuple[str, str], ...] | None:
    """Resolve ``@role`` aliases to static looplane-agent provider/model candidates."""

    if not spec.startswith("@") or len(spec) == 1:
        return None
    role_name = spec[1:]
    try:
        return provider_catalog.role_candidates(role_name, provider=provider)
    except ValueError as exc:
        known = ", ".join(f"@{role.value}" for role in provider_catalog.ModelRole)
        raise typer.BadParameter(f"unknown model role alias {spec!r}; choose {known}") from exc


def _first_model_role_candidate(
    spec: str, *, provider: str | None = None
) -> tuple[str, str] | None:
    candidates = _resolve_model_role_alias(spec, provider=provider)
    if candidates is None:
        return None
    if not candidates:
        raise typer.BadParameter(f"model role alias {spec!r} has no candidates")
    return candidates[0]


def _resolve_cli_settings(
    *,
    provider: str | None,
    model: str | None,
    api_url: str | None,
    allow_model_role_alias: bool = False,
    services: CommandServices,
) -> tuple[str, str | None, str | None]:
    try:
        with services.startup.span("config.load"):
            config = load_cli_config()
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(f"CLI config could not be loaded: {exc}") from exc

    model_provider: str | None = None
    if model and (parsed_model := _parse_provider_model_spec(model)) is not None:
        model_provider, model = parsed_model
    elif (
        allow_model_role_alias
        and model
        and (role_candidate := _first_model_role_candidate(model, provider=provider))
    ):
        model_provider, model = role_candidate
    if provider and model_provider and provider != model_provider:
        raise typer.BadParameter(
            f"--provider {provider} conflicts with model prefix {model_provider}/"
        )
    resolved_provider = provider or model_provider or config.provider or "openai-compatible"
    use_config_defaults = config.provider is None or resolved_provider == config.provider
    resolved_api_url = api_url or (config.api_url if use_config_defaults else None)
    resolved_model = model or (config.model if use_config_defaults else None)
    return resolved_provider, resolved_model, resolved_api_url

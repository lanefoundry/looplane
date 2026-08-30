"""Command-line entrypoint for interactive and headless Rivumi runs."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shlex
import sys
import time
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING, Annotated
from urllib.parse import urlsplit

import httpx
import typer
from typer.core import TyperCommand, TyperGroup

# NOTE: heavy, route-specific modules (provider SDKs, vendor backends, gateway,
# Textual, conversation controllers, runtime discovery) are imported lazily inside
# the command/helper that needs them so `rivumi --help`, `config`, and other
# lightweight routes never load the OpenAI/Anthropic SDKs, uvicorn, Textual, or
# external runtime implementations. See docs/startup-performance-playbook.md.
from rivumi.cli_config import (
    SUPPORTED_PROVIDERS,
    CliConfig,
    default_cli_config_path,
    load_cli_config,
    save_cli_config,
)
from rivumi.contracts import Limits, RunResult, TaskContract, VerificationCommand

if TYPE_CHECKING:
    from rivumi.approvals import TTYApprovalPolicy
    from rivumi.codex_oauth import CodexOAuthClient
    from rivumi.console import ConsoleEventSink
    from rivumi.conversation_controller import BackendTurnLimiter, ConversationController
    from rivumi.loop import AgentRunner
    from rivumi.models import ModelProvider

    NativeControllerCache = dict[
        tuple[str, Path, str | None, str | None], "ConversationController"
    ]

import rivumi.provider_catalog as provider_catalog
import rivumi.runtime_registry as runtime_registry
from rivumi.startup_trace import _STARTUP

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

# Single API key, fixed OpenAI-compatible endpoint providers: (base_url, env var). Values
# verified against @earendil-works/pi-ai's own provider source (the package pi/omp depend on),
# except nvidia-nim/opencode-zen/ollama-cloud which come from the free-llm-models skill notes.
# OpenCode Zen: some free-tier models train on submitted data per its own docs; do not route
# sensitive/confidential code through it. Never the default provider.
_SIMPLE_API_KEY_PROVIDERS: dict[str, str] = {
    "openrouter": "OPENROUTER_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "groq": "GROQ_API_KEY",
    "moonshotai": "MOONSHOT_API_KEY",
    "zai": "ZAI_API_KEY",
    "xai": "XAI_API_KEY",
    "nvidia-nim": "NVIDIA_API_KEY",
    "opencode-zen": "OPENCODE_ZEN_API_KEY",
    "ollama-cloud": "OLLAMA_CLOUD_API_KEY",
}
# Base URLs live in provider_catalog (shared with provider_verification's connection checks).
_SIMPLE_API_KEY_BASE_URLS: dict[str, str] = provider_catalog.OPENAI_COMPATIBLE_BASE_URLS


async def _dispose_controller(controller: ConversationController) -> None:
    with contextlib.suppress(BaseException):
        await controller.aclose()


def _schedule_controller_cleanup(controller: ConversationController) -> None:
    """Best-effort release of a dead controller's workspace and process."""

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_dispose_controller(controller))


def _acquire_native_controller(
    cache: NativeControllerCache,
    identity: tuple[str, Path, str | None, str | None],
    *,
    adapter: runtime_registry.RuntimeAdapter,
    repository: Path,
    model: str | None,
    backend_limiter: BackendTurnLimiter | None = None,
) -> ConversationController:
    """Return the cached controller for ``identity`` or build a fresh one.

    A controller that closed itself after a failed turn (see
    :meth:`ConversationTurnHandle.run`) is discarded and replaced so a single
    protocol failure does not poison every later run in the session.

    The native in-process session class comes from the runtime registry's
    ``native_session`` import path, so adding a new native-driven runtime is a
    registry entry rather than a branch here.
    """

    from rivumi.conversation_controller import ConversationController
    from rivumi.hooks import load_project_hook_runner

    assert adapter.native_session is not None
    controller = cache.get(identity)
    if controller is not None and controller.is_closed:
        _schedule_controller_cleanup(controller)
        cache.pop(identity, None)
        controller = None
    if controller is None:
        with _STARTUP.span("controller.build"):
            session_cls = runtime_registry._resolve_class(adapter.native_session)
            session = session_cls(repository, model=model)
            controller = ConversationController(
                session,
                backend_limiter=backend_limiter,
                hook_runner=load_project_hook_runner(repository),
            )
            cache[identity] = controller
    return controller


class DefaultCommandGroup(TyperGroup):
    """Route command-less invocations to the hidden interactive command."""

    def get_usage(self, ctx) -> str:
        return (
            f"Usage: {ctx.command_path} [OPTIONS] [PROMPT]\n"
            f"       {ctx.command_path} COMMAND [ARGS]...\n"
        )

    def parse_args(self, ctx, args: list[str]) -> list[str]:
        routed = list(args)
        known_commands = set(self.commands)
        group_options = {"--help", "-h", "--install-completion", "--show-completion"}
        completion_active = any(
            os.environ.get(name)
            for name in ("_RIVUMI_COMPLETE", "_ROOT_COMPLETE")
        )
        if not completion_active and (
            not routed or (routed[0] not in known_commands and routed[0] not in group_options)
        ):
            routed.insert(0, "chat")
        return super().parse_args(ctx, routed)

    def shell_complete(self, ctx, incomplete: str):
        results = super().shell_complete(ctx, incomplete)
        chat_command = self.commands.get("chat")
        if chat_command is None or not incomplete.startswith("-"):
            return results
        combined = [*results, *chat_command.shell_complete(ctx, incomplete)]
        unique = []
        seen: set[tuple[str, str | None, str]] = set()
        for item in combined:
            identity = (item.value, item.help, item.type)
            if identity not in seen:
                seen.add(identity)
                unique.append(item)
        return unique


class DefaultChatContext(typer.Context):
    """Present the hidden default command as the root command in diagnostics."""

    @property
    def command_path(self) -> str:
        if self.parent is not None:
            return self.parent.command_path
        return super().command_path


class DefaultChatCommand(TyperCommand):
    """Hide the internal routing command from public usage text."""

    context_class = DefaultChatContext

    def get_usage(self, ctx) -> str:
        command_path = ctx.command_path.removesuffix(" chat")
        return f"Usage: {command_path} [OPTIONS] [PROMPT]\n"


if os.environ.get("RIVUMI_DEBUG"):
    import logging

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

app = typer.Typer(
    cls=DefaultCommandGroup,
    no_args_is_help=False,
    help="A familiar interactive coding agent with a bounded headless harness.",
    epilog=(
        "Daily use: rivumi [PROMPT] | rivumi -p [PROMPT] | rivumi exec [PROMPT] | "
        "rivumi resume. Primary options: -C/--cd/--repo, -m/--model, --provider, "
        "--api-url, --check, --plain. Save non-secret defaults with rivumi config."
    ),
)
auth_app = typer.Typer(help="Manage provider credentials owned by this application.")
backend_app = typer.Typer(help="Run a clearly separated external agent backend.")
policy_app = typer.Typer(help="Inspect effective permission policy.")
plugin_app = typer.Typer(help="Install and inspect repository-local plugin packages.")
cloudflare_app = typer.Typer(help="Operate the Cloudflare-hosted control plane.")
cloudflare_providers_app = typer.Typer(help="Batch-configure hosted model providers.")
app.add_typer(auth_app, name="auth")
app.add_typer(backend_app, name="backend")
app.add_typer(policy_app, name="policy")
app.add_typer(plugin_app, name="plugin")
cloudflare_app.add_typer(cloudflare_providers_app, name="providers")
app.add_typer(cloudflare_app, name="cloudflare")


def _default_run_root() -> Path:
    configured = os.environ.get("RIVUMI_RUN_ROOT") or os.environ.get("PCA_RUN_ROOT")
    if configured:
        return Path(configured)
    state_root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    root = state_root / "rivumi" / "runs"
    legacy_root = state_root / "python-coding-agent" / "runs"
    return legacy_root if not root.exists() and legacy_root.exists() else root


DEFAULT_RUN_ROOT = _default_run_root()


def _codex_credential_path() -> Path:
    state_root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    path = state_root / "rivumi" / "auth" / "openai-codex.json"
    legacy_path = state_root / "python-coding-agent" / "auth" / "openai-codex.json"
    return legacy_path if not path.exists() and legacy_path.exists() else path


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


@plugin_app.command("list")
def plugin_list(
    repo: Annotated[Path | None, typer.Option("--repo", "-C", help="Repository root.")] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """List installed repository-local plugin manifests."""

    from rivumi.plugins import PluginError, load_project_plugins

    project_root = repo or Path.cwd()
    try:
        plugins = load_project_plugins(project_root)
    except PluginError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    if json_output:
        typer.echo(
            json.dumps(
                [
                    {
                        "name": plugin.name,
                        "description": plugin.description,
                        "discovery": plugin.discovery.model_dump(mode="json"),
                        "source": plugin.source,
                        "skills": [skill.path for skill in plugin.skills],
                        "hook_events": sorted(plugin.hooks),
                    }
                    for plugin in plugins
                ],
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return
    if not plugins:
        typer.echo("No repository plugins installed.")
        return
    for plugin in plugins:
        skills = ", ".join(skill.path for skill in plugin.skills) or "-"
        hooks = ", ".join(sorted(plugin.hooks)) or "-"
        description = f" - {plugin.description}" if plugin.description else ""
        typer.echo(f"{plugin.name}{description}")
        typer.echo(f"  source: {plugin.source}")
        typer.echo(f"  skills: {skills}")
        typer.echo(f"  hooks: {hooks}")
        if plugin.discovery.keywords:
            typer.echo(f"  keywords: {', '.join(plugin.discovery.keywords)}")
        if plugin.discovery.homepage:
            typer.echo(f"  homepage: {plugin.discovery.homepage}")
        if plugin.discovery.repository:
            typer.echo(f"  repository: {plugin.discovery.repository}")
        if plugin.discovery.license:
            typer.echo(f"  license: {plugin.discovery.license}")
        if plugin.discovery.author:
            typer.echo(f"  author: {plugin.discovery.author}")


@plugin_app.command("install")
def plugin_install(
    manifest: Annotated[Path, typer.Argument(help="Local plugin manifest JSON to install.")],
    repo: Annotated[Path | None, typer.Option("--repo", "-C", help="Repository root.")] = None,
    name: Annotated[
        str | None,
        typer.Option("--name", help="Override installed plugin name."),
    ] = None,
    overwrite: Annotated[
        bool,
        typer.Option("--overwrite", help="Replace an existing plugin."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """Install a local plugin manifest and referenced skills into `.rivumi/plugins`."""

    from rivumi.plugins import PluginError, install_project_plugin_manifest

    project_root = repo or Path.cwd()
    try:
        plugin = install_project_plugin_manifest(
            manifest,
            project_root=project_root,
            name=name,
            overwrite=overwrite,
        )
    except PluginError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    payload = {
        "name": plugin.name,
        "description": plugin.description,
        "discovery": plugin.discovery.model_dump(mode="json"),
        "source": plugin.source,
        "skills": [skill.path for skill in plugin.skills],
        "hook_events": sorted(plugin.hooks),
    }
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
    typer.echo(f"Installed plugin {plugin.name} at {plugin.source}.")


def _stdin_is_tty() -> bool:
    return sys.stdin.isatty()


def _terminal_supports_tui() -> bool:
    return (
        _stdin_is_tty()
        and sys.stdout.isatty()
        and os.environ.get("TERM", "").lower() != "dumb"
        and (os.environ.get("RIVUMI_NO_TUI") or os.environ.get("PCA_NO_TUI")) != "1"
    )


def _prompt_or_task(prompt: str | None, task: str | None) -> str | None:
    if prompt is not None and task is not None:
        raise typer.BadParameter("use either positional PROMPT or --task, not both")
    return prompt if prompt is not None else task


def _parse_provider_model_spec(spec: str) -> tuple[str, str] | None:
    """Return provider/model for a prefixed model spec when the provider is supported."""

    if "/" not in spec:
        return None
    candidate, model = spec.split("/", 1)
    if candidate in SUPPORTED_PROVIDERS and model:
        return candidate, model
    return None


def _resolve_model_role_alias(
    spec: str,
    *,
    provider: str | None = None,
) -> tuple[tuple[str, str], ...] | None:
    """Resolve ``@role`` aliases to static rivumi-agent provider/model candidates."""

    if not spec.startswith("@") or len(spec) == 1:
        return None
    role_name = spec[1:]
    try:
        return provider_catalog.role_candidates(role_name, provider=provider)
    except ValueError as exc:
        known = ", ".join(f"@{role.value}" for role in provider_catalog.ModelRole)
        raise typer.BadParameter(f"unknown model role alias {spec!r}; choose {known}") from exc


def _first_model_role_candidate(
    spec: str,
    *,
    provider: str | None = None,
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
) -> tuple[str, str | None, str | None]:
    try:
        with _STARTUP.span("config.load"):
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


def _fetch_ollama_models() -> tuple[str, ...]:
    """Return bounded model names from the fixed loopback Ollama discovery endpoint."""

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


def _discover_local_ollama_models() -> tuple[str, ...]:
    """Cached, single-flight wrapper around :func:`_fetch_ollama_models`."""

    from rivumi.startup_cache import CACHE_SCHEMA_VERSION, cached_scan

    models = cached_scan(
        OLLAMA_TAGS_URL,
        CACHE_SCHEMA_VERSION,
        _fetch_ollama_models,
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


def _credential_hint(provider: str, *, api_url: str | None = None) -> str | None:
    from rivumi.native_credentials import resolve_native_field

    if provider == "ollama":
        return None
    if provider == "openai-compatible":
        endpoint = api_url or os.environ.get("OPENAI_BASE_URL")
        if resolve_native_field("openai-compatible", "api_key") or (
            endpoint and _loopback_url(endpoint)
        ):
            return None
        return "Set OPENAI_API_KEY, or run `rivumi auth set-key openai-compatible`."
    if provider == "anthropic":
        return (
            None
            if resolve_native_field("anthropic", "api_key")
            else "Set ANTHROPIC_API_KEY, or run `rivumi auth set-key anthropic`."
        )
    if provider == "gemini":
        return (
            None
            if resolve_native_field("gemini", "api_key")
            else "Set GEMINI_API_KEY or GOOGLE_API_KEY, or run `rivumi auth set-key gemini`."
        )
    if provider == "workers-ai":
        ready = resolve_native_field("workers-ai", "account_id") and resolve_native_field(
            "workers-ai", "api_token"
        )
        return (
            None
            if ready
            else (
                "Set CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN, "
                "or run `rivumi auth set-key workers-ai`."
            )
        )
    env_var = _SIMPLE_API_KEY_PROVIDERS.get(provider)
    if env_var is not None:
        return (
            None
            if resolve_native_field(provider, "api_key")
            else f"Set {env_var}, or run `rivumi auth set-key {provider}`."
        )
    return None


def _interactive_setup(
    *,
    current: CliConfig | None = None,
    locked_provider: str | None = None,
) -> CliConfig:
    """Run provider-aware setup and persist no credential material."""

    if not _stdin_is_tty():
        raise typer.BadParameter("interactive setup requires a TTY")
    current = current or CliConfig()
    ollama_models = _discover_local_ollama_models()
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
        runtime="rivumi-agent",
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
    if hint := _credential_hint(provider, api_url=api_url):
        typer.secho(f"  {hint}", fg=typer.colors.YELLOW)
    return configured


def _show_context_header(*, repository: Path, provider: str, model: str) -> None:
    typer.secho("\nRivumi", bold=True, nl=False)
    typer.echo(f"  ·  {provider}/{model}  ·  {repository.name}")


def _dangerous_acceptance_path() -> Path:
    state_root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return state_root / "rivumi" / "dangerous-mode-accepted"


def _enter_dangerous_mode(dangerous: bool):
    """Resolve the effective approval mode, gating --dangerous entry."""

    from rivumi.permissions import ApprovalMode, DangerousModeError, plan_dangerous_mode_entry

    if not dangerous:
        return ApprovalMode.DEFAULT
    acceptance_path = _dangerous_acceptance_path()
    try:
        outcome = plan_dangerous_mode_entry(
            accepted=acceptance_path.exists(),
            env_acknowledged=os.environ.get("RIVUMI_ACCEPT_DANGEROUS_MODE") == "1",
            is_tty=_stdin_is_tty(),
            is_root=hasattr(os, "getuid") and os.getuid() == 0,
            sandboxed=os.environ.get("RIVUMI_SANDBOX") == "1",
        )
    except DangerousModeError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if outcome == "prompt":
        typer.confirm(
            "You are enabling --dangerous: read/modify actions run without approval "
            "prompts. Forbidden-operation rules still apply. Continue?",
            default=False,
            abort=True,
        )
        try:
            acceptance_path.parent.mkdir(parents=True, exist_ok=True)
            acceptance_path.write_text(f"accepted {time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n")
        except OSError as exc:
            raise typer.BadParameter(
                f"could not record dangerous-mode acceptance: {exc}"
            ) from exc
    return ApprovalMode.DANGEROUS


def _permission_guard_from_config(
    *,
    config: CliConfig,
    repository: Path,
    deny_tool: list[str] | None = None,
    dangerous: bool = False,
):
    from rivumi.permissions import ApprovalMode, PermissionGuard
    from rivumi.policy_config import discover_policy_rules

    try:
        discovery = discover_policy_rules(
            repository=repository,
            user_deny_rules=config.deny_rules,
            user_allow_rules=config.allow_rules,
            extra_user_deny_rules=deny_tool or (),
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    mode = _enter_dangerous_mode(dangerous)
    return (
        PermissionGuard(
            mode=mode,
            deny_rules=discovery.rules.deny_rules,
            allow_rules=discovery.rules.allow_rules,
        ),
        mode is ApprovalMode.DANGEROUS
        or bool(discovery.rules.deny_rules)
        or bool(discovery.rules.allow_rules),
    )


@app.command("chat", hidden=True, cls=DefaultChatCommand)
def chat(
    prompt: Annotated[str | None, typer.Argument(help="Initial task prompt")] = None,
    repository: Annotated[
        Path | None,
        typer.Option("--cd", "-C", "--repo", exists=True, file_okay=False),
    ] = None,
    instruction: Annotated[str | None, typer.Option("--task", "-t")] = None,
    print_mode: Annotated[
        bool,
        typer.Option("--print", "-p", help="Run non-interactively and print JSON."),
    ] = False,
    plain: Annotated[
        bool,
        typer.Option(
            "--plain",
            help="Use the line-oriented terminal UI instead of the full-screen application.",
        ),
    ] = False,
    provider: Annotated[
        str | None,
        typer.Option("--provider", envvar=["RIVUMI_PROVIDER", "PCA_PROVIDER"]),
    ] = None,
    model: Annotated[str | None, typer.Option("--model", "-m", envvar="CODING_AGENT_MODEL")] = None,
    check: Annotated[
        list[str] | None, typer.Option("--check", help="Exact verification argv; repeatable")
    ] = None,
    api_url: Annotated[
        str | None,
        typer.Option(
            "--api-url",
            envvar=["RIVUMI_API_URL", "PCA_API_URL"],
            help="Provider or proxy API URL",
        ),
    ] = None,
    run_root: Annotated[Path, typer.Option("--run-root")] = DEFAULT_RUN_ROOT,
    experimental_subscription: Annotated[
        bool,
        typer.Option(
            "--experimental-subscription",
            help="Enable the separately authenticated experimental ChatGPT/Codex transport.",
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
            help="Allow exact checks from this trusted repository to run on the host.",
        ),
    ] = False,
    dangerous: Annotated[
        bool,
        typer.Option(
            "--dangerous",
            help=(
                "Auto-approve read/modify actions without prompting (EXTREMELY "
                "DANGEROUS). Forbidden-operation rules still apply."
            ),
        ),
    ] = False,
    deny_tool: Annotated[
        list[str] | None,
        typer.Option(
            "--deny-tool",
            help=(
                "Forbidden-operation rule like 'read_file(.env*)' or "
                "'run_check(git push:*)'; repeatable."
            ),
        ),
    ] = None,
    fallback_model: Annotated[
        list[str] | None,
        typer.Option(
            "--fallback-model",
            help=(
                "Provider/model takeover after retries are exhausted, "
                "e.g. 'ollama/qwen3'; repeatable."
            ),
        ),
    ] = None,
    auto_review: Annotated[
        bool,
        typer.Option(
            "--auto-review/--no-auto-review",
            help="After verified native edits, run a read-only reviewer model lane.",
        ),
    ] = False,
    sandbox_checks: Annotated[
        bool,
        typer.Option(
            "--sandbox-checks/--no-sandbox-checks",
            help="Run native verification checks through the local OS sandbox.",
        ),
    ] = True,
) -> None:
    """Start this agent's own loop in the current repository."""

    from rivumi.approvals import HeadlessApprovalPolicy, TTYApprovalPolicy
    from rivumi.console import ConsoleEventSink
    from rivumi.conversation import ConversationStore
    from rivumi.conversation_controller import decide_runtime_approval
    from rivumi.external_runner import ExternalCodingRunner
    from rivumi.loop import AgentRunner, UnsafeLocalExecutionError
    from rivumi.permissions import GuardedApprovalPolicy

    requested_provider = provider
    requested_model = model
    requested_api_url = api_url
    repository = repository or Path.cwd()
    try:
        with _STARTUP.span("config.load"):
            current_config = load_cli_config()
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(f"CLI config could not be loaded: {exc}") from exc
    permission_guard, guard_needed = _permission_guard_from_config(
        config=current_config,
        repository=repository,
        deny_tool=deny_tool,
        dangerous=dangerous,
    )

    def guarded(policy):
        if not guard_needed:
            return policy
        base = (
            policy
            if policy is not None
            else HeadlessApprovalPolicy(
                allow_modify=True,
                allow_execute=unsafe_local_exec,
            )
        )
        return GuardedApprovalPolicy(base, permission_guard)

    fallback_specs = fallback_model or ()
    fallback_cache: list = []
    review_model_cache: list = []

    def build_fallback_models():
        """Construct --fallback-model candidates lazily (credentials resolve at call time)."""

        if not fallback_specs:
            return ()
        if not fallback_cache:
            for spec in fallback_specs:
                parsed_candidates = (
                    _resolve_model_role_alias(spec) if spec.startswith("@") else None
                )
                if parsed_candidates is None:
                    parsed = _parse_provider_model_spec(spec)
                    if parsed is None:
                        raise typer.BadParameter(
                            f"--fallback-model requires provider/model or @role format: {spec!r}"
                        )
                    parsed_candidates = (parsed,)
                if not parsed_candidates:
                    raise typer.BadParameter(
                        f"--fallback-model role alias has no candidates: {spec!r}"
                    )
                for fb_provider, fb_model in parsed_candidates:
                    fallback_cache.append(
                        _model_from_env(
                            provider=fb_provider,
                            model=fb_model,
                            base_url=None,
                            tool_calling=True,
                            allow_custom_provider_endpoint=allow_custom_provider_endpoint,
                            experimental_subscription=experimental_subscription,
                        )
                    )
        return tuple(fallback_cache)

    def build_review_model(selected_provider: str | None):
        """Construct the optional reviewer lane lazily."""

        if not auto_review:
            return None
        if review_model_cache:
            return review_model_cache[0]
        candidates = _resolve_model_role_alias("@reviewer", provider=selected_provider)
        if not candidates:
            raise typer.BadParameter(
                f"--auto-review has no @reviewer candidate for provider {selected_provider!r}"
            )
        review_provider, review_model = candidates[0]
        review_model_cache.append(
            _model_from_env(
                provider=review_provider,
                model=review_model,
                base_url=None,
                tool_calling=False,
                allow_custom_provider_endpoint=allow_custom_provider_endpoint,
                experimental_subscription=experimental_subscription,
            )
        )
        return review_model_cache[0]

    if print_mode and prompt in SUPPORTED_PROVIDERS and provider is None:
        raise typer.BadParameter("-p now means --print; select a provider with --provider instead")
    instruction = _prompt_or_task(prompt, instruction)
    provider, model, api_url = _resolve_cli_settings(
        provider=provider,
        model=model,
        api_url=api_url,
        allow_model_role_alias=True,
    )
    if not print_mode and not plain and _terminal_supports_tui():
        current = current_config
        explicit_rivumi_runtime = any(
            value is not None for value in (requested_provider, requested_model, requested_api_url)
        )
        if explicit_rivumi_runtime:
            selected_provider = requested_provider or current.provider or provider
            initial_config = CliConfig(
                runtime="rivumi-agent",
                provider=selected_provider,
                model=(
                    model
                    if requested_model is not None
                    else (current.model if selected_provider == current.provider else None)
                ),
                api_url=api_url,
                deny_rules=current.deny_rules,
                allow_rules=current.allow_rules,
                sandbox_profile=current.sandbox_profile,
                sandbox_backend=current.sandbox_backend,
                sandbox_read_roots=current.sandbox_read_roots,
            )
        else:
            initial_config = current

        try:
            from rivumi.tui import RivumiApp, TuiRunRequest
        except ModuleNotFoundError as exc:
            if exc.name == "textual" or (exc.name or "").startswith("textual."):
                refresh_script = Path(__file__).resolve().parents[2] / "scripts" / "install-dev-cli"
                refresh_command = (
                    shlex.join([str(refresh_script)])
                    if refresh_script.is_file()
                    else "uv tool install --force rivumi"
                )
                raise typer.BadParameter(
                    "Full-screen UI dependencies are out of sync. Refresh this editable "
                    f"installation with: {refresh_command}"
                ) from None
            raise

        native_controllers: dict[
            tuple[str, Path, str | None, str | None], ConversationController
        ] = {}
        from rivumi.conversation_controller import BackendTurnLimiter

        native_backend_limiter = BackendTurnLimiter()

        def make_runner(request: TuiRunRequest, approval_policy, event_sink):
            approval_policy = guarded(approval_policy)
            adapter = runtime_registry.RUNTIME_REGISTRY.get(request.runtime)
            if adapter is None:
                raise ValueError(f"Unknown runtime: {request.runtime}")
            if adapter.native_session is not None:
                identity = (
                    request.runtime,
                    request.repository.resolve(),
                    request.model,
                    request.context_id,
                )
                controller = _acquire_native_controller(
                    native_controllers,
                    identity,
                    adapter=adapter,
                    repository=request.repository,
                    model=request.model,
                    backend_limiter=native_backend_limiter,
                )
                return (
                    controller.turn(
                        request.instruction,
                        event_sink=event_sink,
                        approval_callback=lambda event: decide_runtime_approval(
                            approval_policy, event
                        ),
                    ),
                    controller,
                )
            if request.mode == "ask":
                raise ValueError("read-only Ask mode is no longer a separate runtime")
            task = TaskContract(
                repository=request.repository,
                instruction=request.instruction,
                allowed_paths=("**",),
                verification=_commands(check),
                limits=Limits(
                    wall_time_seconds=(
                        300.0
                        if adapter.kind is runtime_registry.RuntimeKind.EXTERNAL
                        else 900.0
                    )
                ),
            )
            if (
                adapter.kind is runtime_registry.RuntimeKind.EXTERNAL
                and adapter.backend is not None
            ):
                backend_cls = runtime_registry._resolve_class(adapter.backend)
                backend = backend_cls(model=request.model, timeout_seconds=300.0)
                return (
                    ExternalCodingRunner(
                        task,
                        backend,
                        run_root,
                        allow_external_modify=False,
                        allow_unsafe_local_exec=unsafe_local_exec,
                        approval_policy=approval_policy,
                        event_sink=event_sink,
                    ),
                    None,
                )
            if request.provider is None or request.model is None:
                raise ValueError("Rivumi requires a provider and model")
            if hint := _credential_hint(request.provider, api_url=request.api_url):
                raise ValueError(f"Provider is not ready. {hint}")
            selected_model = _model_from_env(
                provider=request.provider,
                model=request.model,
                base_url=request.api_url,
                tool_calling=True,
                allow_custom_provider_endpoint=allow_custom_provider_endpoint,
                experimental_subscription=experimental_subscription,
            )
            return (
                AgentRunner(
                    task,
                    selected_model,
                    run_root,
                    allow_unsafe_local_exec=unsafe_local_exec,
                    approval_policy=approval_policy,
                    permission_guard=permission_guard,
                    fallback_models=build_fallback_models(),
                    review_model=build_review_model(request.provider),
                    sandbox_checks=_effective_sandbox_checks(sandbox_checks),
                    sandbox_profile=initial_config.sandbox_profile,
                    sandbox_backend=initial_config.sandbox_backend,
                    sandbox_read_roots=tuple(
                        Path(root).expanduser()
                        for root in initial_config.sandbox_read_roots
                    ),
                    event_sink=event_sink,
                ),
                ModelBundleResource(selected_model, build_review_model(request.provider)),
            )

        async def _warmup_native_controller() -> None:
            runtime = initial_config.runtime
            adapter = runtime_registry.RUNTIME_REGISTRY.get(runtime)
            if adapter is None or adapter.native_session is None:
                return
            try:
                model = initial_config.runtime_model or initial_config.model
                identity = (
                    runtime,
                    repository.resolve(),
                    model,
                    tui_app._runtime_context_id,
                )
                controller = _acquire_native_controller(
                    native_controllers,
                    identity,
                    adapter=adapter,
                    repository=repository,
                    model=model,
                    backend_limiter=native_backend_limiter,
                )
                await controller._ensure_started()
            except BaseException:
                # Warm-up is best-effort: a failure must never break app startup
                # or the first real turn.
                pass

        tui_app = RivumiApp(
            repository=repository,
            config=initial_config,
            runner_factory=make_runner,
            runtimes=runtime_registry.runtime_options(),
            runtime_models=runtime_registry.runtime_model_map(),
            providers=ONBOARDING_PROVIDERS,
            ollama_models=(
                _discover_local_ollama_models()
                if initial_config.model is None or initial_config.runtime is None
                else ()
            ),
            initial_prompt=instruction,
            locked_provider=requested_provider,
            conversation_store=ConversationStore(),
            runner_warmup=_warmup_native_controller,
        )
        result = tui_app.run()
        final_transcript = tui_app.final_transcript_text
        if final_transcript:
            typer.echo(final_transcript)
        if tui_app.last_error is not None:
            typer.echo(f"error: {tui_app.last_error}", err=True)
            raise typer.Exit(code=2)
        if result is not None and result.status != "completed":
            raise typer.Exit(code=1)
        return
    headless = print_mode or not _stdin_is_tty()
    if model is None:
        if headless:
            raise typer.BadParameter(
                "no model is configured; run `rivumi config --interactive` in a terminal or pass "
                "`--provider PROVIDER --model MODEL`"
            )
        current = current_config
        preferred_provider = requested_provider or current.provider
        setup_current = CliConfig(
            provider=preferred_provider,
            model=current.model if preferred_provider == current.provider else None,
            api_url=api_url,
            deny_rules=current.deny_rules,
            allow_rules=current.allow_rules,
            sandbox_profile=current.sandbox_profile,
            sandbox_backend=current.sandbox_backend,
            sandbox_read_roots=current.sandbox_read_roots,
        )
        configured = _interactive_setup(
            current=setup_current,
            locked_provider=requested_provider,
        )
        provider, model, api_url = (
            configured.provider,
            configured.model,
            configured.api_url,
        )
        if provider is None or model is None:
            raise typer.BadParameter("interactive setup did not select a provider and model")
    if not print_mode:
        _show_context_header(repository=repository, provider=provider, model=model)
    if hint := _credential_hint(provider, api_url=api_url):
        typer.secho(f"Provider is not ready. {hint}", fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(code=2)
    if headless and instruction is None:
        raise typer.BadParameter("PROMPT is required in non-interactive mode")
    if instruction is None:
        typer.echo("\nWhat would you like me to do in this repository?")
        instruction = typer.prompt("›", prompt_suffix=" ")
    try:
        selected_model = _model_from_env(
            provider=provider,
            model=model,
            base_url=api_url,
            tool_calling=True,
            allow_custom_provider_endpoint=allow_custom_provider_endpoint,
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
                    allow_unsafe_local_exec=unsafe_local_exec,
                    approval_policy=guarded(
                        None if print_mode else TTYApprovalPolicy(sys.stdin, sys.stderr)
                    ),
                    permission_guard=permission_guard,
                    fallback_models=build_fallback_models(),
                    review_model=build_review_model(provider),
                    sandbox_checks=_effective_sandbox_checks(sandbox_checks),
                    sandbox_profile=current_config.sandbox_profile,
                    sandbox_backend=current_config.sandbox_backend,
                    sandbox_read_roots=tuple(
                        Path(root).expanduser()
                        for root in current_config.sandbox_read_roots
                    ),
                    event_sink=None if print_mode else ConsoleEventSink(sys.stderr),
                ),
                selected_model,
                build_review_model(provider),
            )
        )
    except (UnsafeLocalExecutionError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    if print_mode:
        typer.echo(result.model_dump_json(indent=2))
    else:
        _show_result(result)
    if result.status != "completed":
        raise typer.Exit(code=1)


def _effective_sandbox_checks(requested: bool) -> bool:
    """Enable default verification sandboxing only where the local profile is reliable."""

    return requested and sys.platform.startswith("linux")


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise typer.BadParameter(f"required environment variable is missing: {name}")
    return value


def _required_native_field(provider: str, field: str, *, env_hint: str) -> str:
    from rivumi.native_credentials import resolve_native_field

    value = resolve_native_field(provider, field)
    if not value:
        raise typer.BadParameter(
            f"missing {field.replace('_', ' ')} for {provider}: set {env_hint}, "
            f"or run `rivumi auth set-key {provider}`"
        )
    return value


def _loopback_url(value: str) -> bool:
    return urlsplit(value).hostname in {"localhost", "127.0.0.1", "::1"}


def _model_from_env(
    *,
    provider: str,
    model: str,
    base_url: str | None,
    tool_calling: bool,
    allow_custom_provider_endpoint: bool,
    experimental_subscription: bool = False,
) -> ModelProvider:
    from rivumi.codex_oauth import (
        CodexCredentialManager,
        CodexCredentialStore,
        CodexOAuthClient,
        OpenAICodexResponsesModel,
    )
    from rivumi.models import (
        AnthropicModel,
        GeminiModel,
        OpenAICompatibleModel,
        ResponsesModel,
        WorkersAIModel,
    )
    from rivumi.native_credentials import resolve_native_field

    if provider == "openai-compatible":
        return OpenAICompatibleModel(
            model=model,
            api_key=resolve_native_field("openai-compatible", "api_key"),
            base_url=base_url or os.environ.get("OPENAI_BASE_URL"),
            supports_tool_calling=tool_calling,
        )
    if provider == "ollama":
        ollama_url = base_url or os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434/v1")
        return OpenAICompatibleModel(
            model=model,
            base_url=ollama_url,
            api_key=None if _loopback_url(ollama_url) else os.environ.get("OLLAMA_API_KEY"),
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
        with _STARTUP.span("model.codex_oauth"):
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
            api_key=_required_native_field("anthropic", "api_key", env_hint="ANTHROPIC_API_KEY"),
            base_url=base_url
            or os.environ.get("ANTHROPIC_BASE_URL", provider_catalog.ANTHROPIC_BASE_URL),
            supports_tool_calling=tool_calling,
            allow_custom_endpoint=allow_custom_provider_endpoint,
        )
    if provider == "gemini":
        api_key = _required_native_field(
            "gemini", "api_key", env_hint="GEMINI_API_KEY or GOOGLE_API_KEY"
        )
        return GeminiModel(
            model=model,
            api_key=api_key,
            base_url=base_url or provider_catalog.GEMINI_BASE_URL,
            supports_tool_calling=tool_calling,
            allow_custom_endpoint=allow_custom_provider_endpoint,
        )
    if provider == "workers-ai":
        return WorkersAIModel(
            account_id=_required_native_field(
                "workers-ai", "account_id", env_hint="CLOUDFLARE_ACCOUNT_ID"
            ),
            api_token=_required_native_field(
                "workers-ai", "api_token", env_hint="CLOUDFLARE_API_TOKEN"
            ),
            model=model,
            base_url=base_url or provider_catalog.WORKERS_AI_BASE_URL,
            supports_tool_calling=tool_calling,
            allow_custom_endpoint=allow_custom_provider_endpoint,
        )
    if provider in _SIMPLE_API_KEY_PROVIDERS:
        api_key = _required_native_field(
            provider, "api_key", env_hint=_SIMPLE_API_KEY_PROVIDERS[provider]
        )
        base_url = base_url or _SIMPLE_API_KEY_BASE_URLS[provider]
        if provider_catalog.uses_responses_protocol(provider, model):
            return ResponsesModel(
                model=model,
                api_key=api_key,
                base_url=base_url,
                supports_tool_calling=tool_calling,
                allow_custom_endpoint=True,  # base_url comes from the fixed provider catalog
            )
        return OpenAICompatibleModel(
            model=model,
            api_key=api_key,
            base_url=base_url,
            supports_tool_calling=tool_calling,
            provider_name=provider,
        )
    raise typer.BadParameter(f"unsupported provider: {provider}")


@backend_app.command("claude-code")
def run_claude_code_backend(
    prompt: Annotated[str | None, typer.Argument(help="Delegated coding task")] = None,
    instruction: Annotated[
        str | None, typer.Option("--task", "-t", help="Compatibility alias for PROMPT")
    ] = None,
    repository: Annotated[
        Path | None,
        typer.Option("--cd", "-C", "--repo", exists=True, file_okay=False),
    ] = None,
    check: Annotated[
        list[str] | None, typer.Option("--check", help="Exact final verification argv; repeatable")
    ] = None,
    allowed_path: Annotated[
        list[str] | None,
        typer.Option("--allowed-path", help="Allowed changed path or glob; repeatable"),
    ] = None,
    run_root: Annotated[Path, typer.Option("--run-root")] = DEFAULT_RUN_ROOT,
    task_id: Annotated[str, typer.Option("--task-id")] = "claude-code-task",
    timeout_seconds: Annotated[
        float, typer.Option("--timeout", min=1, help="Maximum delegated runtime in seconds.")
    ] = 300.0,
    experimental_subscription: Annotated[
        bool,
        typer.Option(
            "--experimental-subscription",
            help="Acknowledge this local-only official Claude Code delegation boundary.",
        ),
    ] = False,
    allow_external_modify: Annotated[
        bool,
        typer.Option(
            "--allow-external-modify",
            help="Approve this external CLI editing only Rivumi's disposable clone.",
        ),
    ] = False,
    unsafe_local_exec: Annotated[
        bool,
        typer.Option(
            "--unsafe-local-exec",
            help="Allow exact final checks from this trusted repository to run on the host.",
        ),
    ] = False,
) -> None:
    """Let official Claude Code edit a disposable clone, then audit it with Rivumi."""

    from rivumi.claude_backend import ClaudeCodeBackend
    from rivumi.external_runner import (
        ExternalCodingRunner,
        ExternalModificationApprovalError,
        UnsafeExternalVerificationError,
    )

    instruction = _prompt_or_task(prompt, instruction)
    if instruction is None:
        raise typer.BadParameter("PROMPT or --task is required")
    if not experimental_subscription:
        raise typer.BadParameter(
            "Claude Code delegation is local-only and experimental; pass "
            "--experimental-subscription"
        )
    if not check:
        raise typer.BadParameter("external coding requires at least one explicit --check command")
    backend = ClaudeCodeBackend(timeout_seconds=timeout_seconds)
    try:
        result = asyncio.run(
            ExternalCodingRunner(
                TaskContract(
                    repository=repository or Path.cwd(),
                    instruction=instruction,
                    allowed_paths=tuple(allowed_path or ("**",)),
                    verification=_commands(check),
                    limits=Limits(wall_time_seconds=timeout_seconds),
                    task_id=task_id,
                ),
                backend,
                run_root,
                allow_external_modify=allow_external_modify,
                allow_unsafe_local_exec=unsafe_local_exec,
            ).run()
        )
    except (
        ExternalModificationApprovalError,
        UnsafeExternalVerificationError,
        OSError,
        ValueError,
    ) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    _show_result(result)
    if result.status != "completed":
        raise typer.Exit(code=1)


@backend_app.command("codex-cli")
def run_codex_cli_backend(
    prompt: Annotated[str | None, typer.Argument(help="Delegated coding task")] = None,
    instruction: Annotated[
        str | None, typer.Option("--task", "-t", help="Compatibility alias for PROMPT")
    ] = None,
    repository: Annotated[
        Path | None,
        typer.Option("--cd", "-C", "--repo", exists=True, file_okay=False),
    ] = None,
    check: Annotated[
        list[str] | None, typer.Option("--check", help="Exact final verification argv; repeatable")
    ] = None,
    allowed_path: Annotated[
        list[str] | None,
        typer.Option("--allowed-path", help="Allowed changed path or glob; repeatable"),
    ] = None,
    run_root: Annotated[Path, typer.Option("--run-root")] = DEFAULT_RUN_ROOT,
    task_id: Annotated[str, typer.Option("--task-id")] = "codex-cli-task",
    timeout_seconds: Annotated[
        float, typer.Option("--timeout", min=1, help="Maximum delegated runtime in seconds.")
    ] = 300.0,
    experimental_subscription: Annotated[
        bool,
        typer.Option(
            "--experimental-subscription",
            help="Acknowledge use of the separately authenticated official Codex CLI.",
        ),
    ] = False,
    allow_external_modify: Annotated[
        bool,
        typer.Option(
            "--allow-external-modify",
            help="Approve this external CLI editing only Rivumi's disposable clone.",
        ),
    ] = False,
    unsafe_local_exec: Annotated[
        bool,
        typer.Option(
            "--unsafe-local-exec",
            help="Allow exact final checks from this trusted repository to run on the host.",
        ),
    ] = False,
) -> None:
    """Let official Codex CLI edit a sandboxed clone, then audit it with Rivumi."""

    from rivumi.codex_backend import CodexCliBackend
    from rivumi.external_runner import (
        ExternalCodingRunner,
        ExternalModificationApprovalError,
        UnsafeExternalVerificationError,
    )

    instruction = _prompt_or_task(prompt, instruction)
    if instruction is None:
        raise typer.BadParameter("PROMPT or --task is required")
    if not experimental_subscription:
        raise typer.BadParameter(
            "Codex CLI delegation is local-only and experimental; pass --experimental-subscription"
        )
    if not check:
        raise typer.BadParameter("external coding requires at least one explicit --check command")
    backend = CodexCliBackend(timeout_seconds=timeout_seconds)
    try:
        result = asyncio.run(
            ExternalCodingRunner(
                TaskContract(
                    repository=repository or Path.cwd(),
                    instruction=instruction,
                    allowed_paths=tuple(allowed_path or ("**",)),
                    verification=_commands(check),
                    limits=Limits(wall_time_seconds=timeout_seconds),
                    task_id=task_id,
                ),
                backend,
                run_root,
                allow_external_modify=allow_external_modify,
                allow_unsafe_local_exec=unsafe_local_exec,
            ).run()
        )
    except (
        ExternalModificationApprovalError,
        UnsafeExternalVerificationError,
        OSError,
        ValueError,
    ) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    _show_result(result)
    if result.status != "completed":
        raise typer.Exit(code=1)


def _run_external_coding(
    *,
    prompt: str | None,
    instruction: str | None,
    repository: Path | None,
    check: list[str] | None,
    allowed_path: list[str] | None,
    run_root: Path,
    task_id: str,
    timeout_seconds: float,
    allow_external_modify: bool,
    unsafe_local_exec: bool,
    backend: object,
    require_model: bool,
    model: str | None,
) -> None:
    """Shared runner path for the registry-backed external coding CLIs."""

    from rivumi.external_runner import (
        ExternalCodingRunner,
        ExternalModificationApprovalError,
        UnsafeExternalVerificationError,
    )

    instruction = _prompt_or_task(prompt, instruction)
    if instruction is None:
        raise typer.BadParameter("PROMPT or --task is required")
    if require_model and not model:
        raise typer.BadParameter("--model is required for this external runtime")
    if not check:
        raise typer.BadParameter("external coding requires at least one explicit --check command")
    try:
        result = asyncio.run(
            ExternalCodingRunner(
                TaskContract(
                    repository=repository or Path.cwd(),
                    instruction=instruction,
                    allowed_paths=tuple(allowed_path or ("**",)),
                    verification=_commands(check),
                    limits=Limits(wall_time_seconds=timeout_seconds),
                    task_id=task_id,
                ),
                backend,  # type: ignore[arg-type]
                run_root,
                allow_external_modify=allow_external_modify,
                allow_unsafe_local_exec=unsafe_local_exec,
            ).run()
        )
    except (
        ExternalModificationApprovalError,
        UnsafeExternalVerificationError,
        OSError,
        ValueError,
    ) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    _show_result(result)
    if result.status != "completed":
        raise typer.Exit(code=1)


@backend_app.command("opencode")
def _run_opencode_backend(
    prompt: Annotated[str | None, typer.Argument(help="Delegated coding task")] = None,
    instruction: Annotated[
        str | None, typer.Option("--task", "-t", help="Compatibility alias for PROMPT")
    ] = None,
    repository: Annotated[
        Path | None, typer.Option("--cd", "-C", "--repo", exists=True, file_okay=False)
    ] = None,
    check: Annotated[
        list[str] | None, typer.Option("--check", help="Exact final verification argv; repeatable")
    ] = None,
    allowed_path: Annotated[
        list[str] | None, typer.Option("--allowed-path", help="Allowed path/glob; repeatable")
    ] = None,
    model: Annotated[
        str | None,
        typer.Option("--model", "-m", help="OpenCode provider/model id; e.g. ollama/gemma4."),
    ] = None,
    run_root: Annotated[Path, typer.Option("--run-root")] = DEFAULT_RUN_ROOT,
    task_id: Annotated[str, typer.Option("--task-id")] = "opencode-task",
    timeout_seconds: Annotated[
        float, typer.Option("--timeout", min=1, help="Maximum delegated runtime in seconds.")
    ] = 300.0,
    allow_external_modify: Annotated[
        bool, typer.Option("--allow-external-modify", help="Approve CLI edits in Rivumi's clone.")
    ] = False,
    unsafe_local_exec: Annotated[
        bool, typer.Option("--unsafe-local-exec", help="Allow trusted repo checks to run on host.")
    ] = False,
) -> None:
    """Delegate to the installed OpenCode CLI in an isolated clone, then audit it with Rivumi."""

    from rivumi.opencode_backend import OpenCodeBackend

    backend = OpenCodeBackend(executable="opencode", model=model, timeout_seconds=timeout_seconds)
    _run_external_coding(
        require_model=True,
        backend=backend,
        prompt=prompt,
        instruction=instruction,
        repository=repository,
        check=check,
        allowed_path=allowed_path,
        run_root=run_root,
        task_id=task_id,
        timeout_seconds=timeout_seconds,
        allow_external_modify=allow_external_modify,
        unsafe_local_exec=unsafe_local_exec,
        model=model,
    )


@backend_app.command("pi")
def _run_pi_backend(
    prompt: Annotated[str | None, typer.Argument(help="Delegated coding task")] = None,
    instruction: Annotated[
        str | None, typer.Option("--task", "-t", help="Compatibility alias for PROMPT")
    ] = None,
    repository: Annotated[
        Path | None, typer.Option("--cd", "-C", "--repo", exists=True, file_okay=False)
    ] = None,
    check: Annotated[
        list[str] | None, typer.Option("--check", help="Exact final verification argv; repeatable")
    ] = None,
    allowed_path: Annotated[
        list[str] | None, typer.Option("--allowed-path", help="Allowed path/glob; repeatable")
    ] = None,
    model: Annotated[
        str | None,
        typer.Option("--model", "-m", help="Pi provider/model id; Pi owns its auth."),
    ] = None,
    run_root: Annotated[Path, typer.Option("--run-root")] = DEFAULT_RUN_ROOT,
    task_id: Annotated[str, typer.Option("--task-id")] = "pi-task",
    timeout_seconds: Annotated[
        float, typer.Option("--timeout", min=1, help="Maximum delegated runtime in seconds.")
    ] = 300.0,
    allow_external_modify: Annotated[
        bool, typer.Option("--allow-external-modify", help="Approve CLI edits in Rivumi's clone.")
    ] = False,
    unsafe_local_exec: Annotated[
        bool, typer.Option("--unsafe-local-exec", help="Allow trusted repo checks to run on host.")
    ] = False,
) -> None:
    """Delegate to the installed Pi coding agent in an isolated clone, then audit it with Rivumi."""

    from rivumi.pi_backend import PiBackend

    backend = PiBackend(executable="pi", model=model, timeout_seconds=timeout_seconds)
    _run_external_coding(
        require_model=True,
        backend=backend,
        prompt=prompt,
        instruction=instruction,
        repository=repository,
        check=check,
        allowed_path=allowed_path,
        run_root=run_root,
        task_id=task_id,
        timeout_seconds=timeout_seconds,
        allow_external_modify=allow_external_modify,
        unsafe_local_exec=unsafe_local_exec,
        model=model,
    )


@backend_app.command("omp")
def _run_omp_backend(
    prompt: Annotated[str | None, typer.Argument(help="Delegated coding task")] = None,
    instruction: Annotated[
        str | None, typer.Option("--task", "-t", help="Compatibility alias for PROMPT")
    ] = None,
    repository: Annotated[
        Path | None, typer.Option("--cd", "-C", "--repo", exists=True, file_okay=False)
    ] = None,
    check: Annotated[
        list[str] | None, typer.Option("--check", help="Exact final verification argv; repeatable")
    ] = None,
    allowed_path: Annotated[
        list[str] | None, typer.Option("--allowed-path", help="Allowed path/glob; repeatable")
    ] = None,
    model: Annotated[
        str | None,
        typer.Option("--model", "-m", help="OMP provider/model id; OMP owns its auth."),
    ] = None,
    run_root: Annotated[Path, typer.Option("--run-root")] = DEFAULT_RUN_ROOT,
    task_id: Annotated[str, typer.Option("--task-id")] = "omp-task",
    timeout_seconds: Annotated[
        float, typer.Option("--timeout", min=1, help="Maximum delegated runtime in seconds.")
    ] = 300.0,
    allow_external_modify: Annotated[
        bool, typer.Option("--allow-external-modify", help="Approve CLI edits in Rivumi's clone.")
    ] = False,
    unsafe_local_exec: Annotated[
        bool, typer.Option("--unsafe-local-exec", help="Allow trusted repo checks to run on host.")
    ] = False,
) -> None:
    """Delegate to the installed OMP agent in an isolated clone, then audit it with Rivumi."""

    from rivumi.omp_backend import OmpBackend

    backend = OmpBackend(executable="omp", model=model, timeout_seconds=timeout_seconds)
    _run_external_coding(
        require_model=True,
        backend=backend,
        prompt=prompt,
        instruction=instruction,
        repository=repository,
        check=check,
        allowed_path=allowed_path,
        run_root=run_root,
        task_id=task_id,
        timeout_seconds=timeout_seconds,
        allow_external_modify=allow_external_modify,
        unsafe_local_exec=unsafe_local_exec,
        model=model,
    )


@auth_app.command("login-codex")
def login_codex(
    timeout_seconds: Annotated[
        float,
        typer.Option(
            "--timeout",
            min=1,
            help="Seconds to wait for the loopback browser callback.",
        ),
    ] = 300.0,
    manual: Annotated[
        bool,
        typer.Option(
            "--manual",
            help="Print the authorization URL and securely paste the final callback URL.",
        ),
    ] = False,
) -> None:
    """Create this application's own experimental ChatGPT/Codex OAuth grant."""

    from rivumi.codex_oauth import CodexCredentialStore, CodexOAuthClient
    from rivumi.models import ProviderError
    from rivumi.oauth_login import parse_codex_callback, wait_for_codex_callback

    oauth = CodexOAuthClient()
    exchange_started = False
    try:
        authorization = oauth.begin_login(originator="rivumi")
        typer.echo("This creates a separate grant; it does not read the Codex CLI credential.")
        if manual:
            typer.echo(authorization.url)
            callback_url = typer.prompt(
                "Paste the final localhost callback URL",
                hide_input=True,
            )
            code = parse_codex_callback(callback_url, expected_state=authorization.state)
        else:

            def open_browser(_: tuple[str, int]) -> None:
                typer.echo("Opening the ChatGPT/Codex authorization page in your browser.")
                if not webbrowser.open(authorization.url):
                    typer.echo("The browser did not open. Open this URL manually:")
                    typer.echo(authorization.url)

            code = wait_for_codex_callback(
                authorization,
                timeout_seconds=timeout_seconds,
                on_listening=open_browser,
            )
        exchange_started = True
        credentials = asyncio.run(_exchange_codex_code(oauth, code, authorization.verifier))
        CodexCredentialStore(_codex_credential_path()).save(credentials)
    except (OSError, ProviderError, TimeoutError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    finally:
        if not exchange_started:
            asyncio.run(oauth.aclose())
    typer.echo("ChatGPT/Codex authorization saved for Rivumi.")


@auth_app.command("status-codex")
def status_codex() -> None:
    """Report redacted status for this application's Codex grant."""

    from rivumi.codex_oauth import CodexCredentialStore

    try:
        credentials = CodexCredentialStore(_codex_credential_path()).load()
    except (OSError, PermissionError, ValueError) as exc:
        typer.echo(f"error: Codex authorization is unreadable: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    if credentials is None:
        typer.echo("ChatGPT/Codex authorization: not configured")
        raise typer.Exit(code=1)
    expiry = "expired" if credentials.expires_at <= time.time() else "valid"
    typer.echo(f"ChatGPT/Codex authorization: configured ({expiry})")


@auth_app.command("logout-codex")
def logout_codex() -> None:
    """Delete this application's Codex grant without touching another CLI."""

    from rivumi.codex_oauth import CodexCredentialStore

    path = _codex_credential_path()
    store = CodexCredentialStore(path)
    try:
        credentials = store.load()
        if credentials is None:
            typer.echo("ChatGPT/Codex authorization was not configured.")
            return
        path.unlink()
    except (OSError, PermissionError, ValueError) as exc:
        typer.echo(f"error: Codex authorization could not be removed: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo("ChatGPT/Codex authorization removed from Rivumi.")


@auth_app.command("login-mcp")
def login_mcp(
    server: Annotated[str, typer.Argument(help="Allowlisted MCP server name from .mcp.json")],
    repository: Annotated[
        Path | None,
        typer.Option("--repo", "--cd", "-C", exists=True, file_okay=False),
    ] = None,
    manual: Annotated[
        bool,
        typer.Option("--manual", help="Print the authorization URL instead of opening a browser."),
    ] = False,
) -> None:
    """Create this application's own MCP OAuth authorization-code grant."""

    from rivumi.mcp_client import (
        McpError,
        McpOAuthClient,
        McpOAuthCredentialStore,
        load_native_mcp_server_configs,
        mcp_oauth_credential_path,
        parse_mcp_oauth_callback,
    )

    try:
        repository = repository or Path.cwd()
        configs = load_native_mcp_server_configs(repository, allowlist=(server,))
        if len(configs) != 1 or configs[0].oauth is None:
            raise typer.BadParameter("MCP server must exist and define oauth metadata")
        oauth = McpOAuthClient()
        try:
            authorization = oauth.begin_login(configs[0].oauth)
            typer.echo("This creates a Rivumi-owned MCP grant; no other client store is read.")
            if manual:
                typer.echo(authorization.url)
            elif not webbrowser.open(authorization.url):
                typer.echo("The browser did not open. Open this URL manually:")
                typer.echo(authorization.url)
            callback_url = typer.prompt("Paste the final OAuth callback URL", hide_input=True)
            code = parse_mcp_oauth_callback(callback_url, expected_state=authorization.state)
            credential = oauth.exchange_code(
                configs[0].oauth,
                code=code,
                verifier=authorization.verifier,
            )
        finally:
            oauth.close()
        path = mcp_oauth_credential_path(server)
        McpOAuthCredentialStore(path).save(credential)
    except (McpError, OSError, PermissionError, ValueError) as exc:
        typer.echo(f"error: MCP authorization failed: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"MCP authorization saved for {server!r}.")


@auth_app.command("status-mcp")
def status_mcp(
    server: Annotated[str, typer.Argument(help="MCP server name")],
) -> None:
    """Report redacted status for one Rivumi-owned MCP OAuth grant."""

    from rivumi.mcp_client import McpOAuthCredentialStore, mcp_oauth_credential_path

    try:
        credential = McpOAuthCredentialStore(mcp_oauth_credential_path(server)).load()
    except (OSError, PermissionError, ValueError) as exc:
        typer.echo(f"error: MCP authorization is unreadable: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    if credential is None:
        typer.echo(f"MCP authorization for {server!r}: not configured")
        raise typer.Exit(code=1)
    expiry = (
        "unknown"
        if credential.expires_at is None
        else ("expired" if credential.expires_at <= time.time() else "valid")
    )
    typer.echo(f"MCP authorization for {server!r}: configured ({expiry})")


@auth_app.command("logout-mcp")
def logout_mcp(
    server: Annotated[str, typer.Argument(help="MCP server name")],
) -> None:
    """Delete this application's MCP OAuth grant for one server."""

    from rivumi.mcp_client import McpOAuthCredentialStore, mcp_oauth_credential_path

    try:
        cleared = McpOAuthCredentialStore(mcp_oauth_credential_path(server)).clear()
    except (OSError, PermissionError, ValueError) as exc:
        typer.echo(f"error: MCP authorization could not be removed: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    if cleared:
        typer.echo(f"MCP authorization for {server!r} removed from Rivumi.")
    else:
        typer.echo(f"MCP authorization for {server!r} was not configured.")


@auth_app.command("set-key")
def auth_set_key(
    provider: Annotated[
        str,
        typer.Argument(help="anthropic | gemini | openai-compatible | workers-ai"),
    ],
) -> None:
    """Store an API key/secret for a rivumi-agent provider, local to this application only."""

    from rivumi.native_credentials import NATIVE_CREDENTIAL_FIELDS, save_native_credential

    fields = NATIVE_CREDENTIAL_FIELDS.get(provider)
    if fields is None:
        choices = ", ".join(sorted(NATIVE_CREDENTIAL_FIELDS))
        raise typer.BadParameter(f"provider must be one of: {choices}")
    if not _stdin_is_tty():
        raise typer.BadParameter("rivumi auth set-key requires a TTY")
    values = {
        field: typer.prompt(field.replace("_", " ").title(), hide_input=True) for field in fields
    }
    try:
        path = save_native_credential(provider, values)
    except (OSError, PermissionError, ValueError) as exc:
        typer.echo(f"error: could not save {provider} credentials: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    from rivumi.provider_verification import verify_native_credential

    result = asyncio.run(verify_native_credential(provider, values))
    if result.skipped:
        typer.echo(f"Saved {provider} credentials for rivumi-agent at {path}")
    elif result.ok:
        typer.secho(f"✓ Saved and verified {provider} credentials at {path}", fg=typer.colors.GREEN)
    else:
        # Verification failed, but the credential is already saved -- don't lock the user
        # out of a key they may just not be able to verify from here (offline, provider
        # outage). They can re-run `rivumi auth set-key` or `rivumi auth list --verify`.
        typer.secho(
            f"⚠ Saved {provider} credentials at {path}, but verification failed: "
            f"{result.message}",
            fg=typer.colors.YELLOW,
        )


@auth_app.command("clear-key")
def auth_clear_key(
    provider: Annotated[
        str,
        typer.Argument(help="anthropic | gemini | openai-compatible | workers-ai"),
    ],
) -> None:
    """Delete a stored rivumi-agent provider credential."""

    from rivumi.native_credentials import NATIVE_CREDENTIAL_FIELDS, clear_native_credential

    if provider not in NATIVE_CREDENTIAL_FIELDS:
        choices = ", ".join(sorted(NATIVE_CREDENTIAL_FIELDS))
        raise typer.BadParameter(f"provider must be one of: {choices}")
    try:
        cleared = clear_native_credential(provider)
    except (OSError, PermissionError, ValueError) as exc:
        typer.echo(f"error: could not clear {provider} credentials: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    if cleared:
        typer.echo(f"Cleared stored {provider} credentials for rivumi-agent.")
    else:
        typer.echo(f"No stored {provider} credentials were found.")


@auth_app.command("list")
def auth_list(
    verify: Annotated[
        bool,
        typer.Option(
            "--verify",
            help="Also call each stored provider's API to confirm it still works "
            "(slower, needs network).",
        ),
    ] = False,
) -> None:
    """Show which rivumi-agent providers have stored credentials, and their status.

    Without --verify this only reads local state (env vars / the credential store) and
    never touches the network, so it stays fast. --verify calls each already-configured
    provider's API once, the same connection check `auth set-key` runs after saving.
    """

    from rivumi.native_credentials import NATIVE_CREDENTIAL_FIELDS, missing_native_fields

    for provider in sorted(NATIVE_CREDENTIAL_FIELDS):
        if missing_native_fields(provider):
            typer.echo(f"· {provider:<18} not set · run `rivumi auth set-key {provider}`")
            continue
        if not verify:
            typer.secho(
                f"⚠ {provider:<18} saved, not verified this run · re-run with --verify",
                fg=typer.colors.YELLOW,
            )
            continue

        from rivumi.native_credentials import resolve_native_field
        from rivumi.provider_verification import verify_native_credential

        fields: dict[str, str] = {}
        for field in NATIVE_CREDENTIAL_FIELDS[provider]:
            value = resolve_native_field(provider, field)
            assert value is not None  # guaranteed non-missing by the check above
            fields[field] = value
        result = asyncio.run(verify_native_credential(provider, fields))
        if result.ok:
            typer.secho(f"✓ {provider:<18} verified", fg=typer.colors.GREEN)
        else:
            typer.secho(f"✗ {provider:<18} invalid · {result.message}", fg=typer.colors.RED)


@cloudflare_providers_app.command("apply")
def cloudflare_providers_apply(
    manifest: Annotated[
        Path,
        typer.Argument(help="Non-secret JSON manifest containing every hosted provider profile."),
    ],
    secrets_env: Annotated[
        Path | None,
        typer.Option(
            "--secrets-env",
            help="Private mode-0600 dotenv file containing all referenced provider keys.",
        ),
    ] = None,
    cloudflare_dir: Annotated[
        Path,
        typer.Option(
            "--cloudflare-dir",
            help="Directory containing wrangler.jsonc and the Cloudflare package scripts.",
        ),
    ] = Path("cloudflare"),
    wrangler_env: Annotated[
        str | None,
        typer.Option("--env", help="Optional named Wrangler environment."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Validate and build without changing Cloudflare state."),
    ] = False,
    allow_custom_endpoint: Annotated[
        bool,
        typer.Option(
            "--allow-custom-endpoint",
            help="Allow a manifest to send its named credential to an unbundled HTTPS endpoint.",
        ),
    ] = False,
) -> None:
    """Upload all provider keys and deploy their hosted profiles in one batch."""

    from rivumi.cloudflare_provider_setup import ProviderSetupError, setup_cloudflare_providers

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
        str | None,
        typer.Option("--api-url", envvar=["RIVUMI_API_URL", "PCA_API_URL"]),
    ] = None,
    experimental_subscription: Annotated[bool, typer.Option("--experimental-subscription")] = False,
    allow_custom_provider_endpoint: Annotated[
        bool, typer.Option("--allow-custom-provider-endpoint")
    ] = False,
) -> None:
    """Resume a validated non-terminal session in its existing disposable workspace."""

    from rivumi.approvals import TTYApprovalPolicy
    from rivumi.console import ConsoleEventSink, LiveEventProjection
    from rivumi.session import SessionStore, SessionValidationError

    try:
        run_dir = _resolve_resume_dir(run_root, session)
        manifest = asyncio.run(SessionStore(run_dir).load())
        _, _, api_url = _resolve_cli_settings(
            provider=manifest.provider_name,
            model=manifest.model_id,
            api_url=api_url,
        )
        selected_model = _model_from_env(
            provider=manifest.provider_name,
            model=manifest.model_id,
            base_url=api_url,
            tool_calling=True,
            allow_custom_provider_endpoint=allow_custom_provider_endpoint,
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


@app.command("sessions")
def sessions(
    run_root: Annotated[Path, typer.Option("--run-root")] = DEFAULT_RUN_ROOT,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 20,
    show: Annotated[
        str | None,
        typer.Option("--show", help="Show a compact timeline for one run id or id prefix."),
    ] = None,
    replay: Annotated[
        str | None,
        typer.Option(
            "--replay",
            help="Replay deterministic state and timeline for one run id or id prefix.",
        ),
    ] = None,
    replay_json: Annotated[
        str | None,
        typer.Option(
            "--replay-json",
            help="Print deterministic replay JSON for one run id or id prefix.",
        ),
    ] = None,
    fork_from_event: Annotated[
        str | None,
        typer.Option(
            "--fork-from-event",
            help="Print a safe fork seed artifact for one run id or id prefix.",
        ),
    ] = None,
    analyze_subagents: Annotated[
        str | None,
        typer.Option(
            "--analyze-subagents",
            help="Analyze subagent schedule traces for one run id or id prefix.",
        ),
    ] = None,
    sequence: Annotated[
        int | None,
        typer.Option(
            "--sequence",
            help="Target event sequence for --fork-from-event.",
        ),
    ] = None,
    query: Annotated[
        str | None,
        typer.Option(
            "--query",
            "-q",
            help="Filter sessions by id, status, model, task, or summary.",
        ),
    ] = None,
) -> None:
    """List recent agent runs and saved conversations with their usage."""

    max_json_bytes = 16 * 1024 * 1024
    max_event_search_parts = 256
    max_event_search_part_chars = 4096
    normalized_query = query.casefold().strip() if query else None

    detail_modes = tuple(
        name
        for name, value in (
            ("--show", show),
            ("--replay", replay),
            ("--replay-json", replay_json),
            ("--fork-from-event", fork_from_event),
            ("--analyze-subagents", analyze_subagents),
        )
        if value is not None
    )
    if len(detail_modes) > 1:
        raise typer.BadParameter(f"{' and '.join(detail_modes)} cannot be used together")
    if sequence is not None and fork_from_event is None:
        raise typer.BadParameter("--sequence requires --fork-from-event")

    def _read_json(path: Path) -> dict[str, object] | None:
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > max_json_bytes:
                return None
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def _safe_session_dir(path: Path) -> bool:
        return (
            not path.name.startswith(".")
            and "/" not in path.name
            and "\\" not in path.name
            and path.name not in {".", ".."}
            and not path.is_symlink()
            and path.is_dir()
        )

    def _matches(parts: list[object]) -> bool:
        if normalized_query is None:
            return True
        haystack = " ".join(str(part) for part in parts if part is not None).casefold()
        return normalized_query in haystack

    def _bounded_event_search_parts(value: object, parts: list[str]) -> None:
        if len(parts) >= max_event_search_parts:
            return
        if isinstance(value, str):
            text = value.strip()
            if text:
                parts.append(text[:max_event_search_part_chars])
            return
        if isinstance(value, dict):
            for item in value.values():
                _bounded_event_search_parts(item, parts)
                if len(parts) >= max_event_search_parts:
                    return
            return
        if isinstance(value, list | tuple):
            for item in value:
                _bounded_event_search_parts(item, parts)
                if len(parts) >= max_event_search_parts:
                    return

    def _event_search_parts(path: Path) -> list[str]:
        if normalized_query is None:
            return []
        parts: list[str] = []
        for event in _read_events(path):
            _bounded_event_search_parts(event, parts)
            if len(parts) >= max_event_search_parts:
                break
        return parts

    def _conversation_event_search_parts(events: object) -> list[str]:
        if normalized_query is None:
            return []
        parts: list[str] = []
        for event in (events if isinstance(events, tuple) else ()):
            payload = event.model_dump(mode="json") if hasattr(event, "model_dump") else event
            _bounded_event_search_parts(payload, parts)
            if len(parts) >= max_event_search_parts:
                break
        return parts

    def _usage_total(source: dict[str, object]) -> object:
        usage = source.get("usage") or {}
        if not isinstance(usage, dict):
            return 0
        total = usage.get("provider_total_tokens") or usage.get("input_tokens", 0)
        return 0 if isinstance(total, dict) else total

    def _resolve_run_dir(identifier: str) -> Path | None:
        if not identifier or "/" in identifier or "\\" in identifier or identifier in {".", ".."}:
            return None
        if not run_root.exists() or run_root.is_symlink() or not run_root.is_dir():
            return None
        exact = run_root / identifier
        if _safe_session_dir(exact):
            return exact
        matches = [
            path
            for path in run_root.iterdir()
            if _safe_session_dir(path) and path.name.startswith(identifier)
        ]
        if len(matches) == 1:
            return matches[0]
        return None

    def _read_events(path: Path) -> list[dict[str, object]]:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > max_json_bytes:
            return []
        events: list[dict[str, object]] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                value = json.loads(line)
                if isinstance(value, dict):
                    events.append(value)
        except (OSError, UnicodeError, ValueError):
            return []
        return sorted(
            events,
            key=lambda value: value.get("sequence")
            if isinstance(value.get("sequence"), int)
            else -1,
        )

    def _event_detail(event: dict[str, object]) -> str:
        data = event.get("data")
        data = data if isinstance(data, dict) else {}
        for key in (
            "summary",
            "terminal_reason",
            "reason",
            "tool",
            "name",
            "model",
            "provider",
            "base_sha",
        ):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
        return ""

    if show is not None:
        run_dir = _resolve_run_dir(show)
        if run_dir is None:
            typer.echo(f"error: no unique run matching {show!r} under {run_root}", err=True)
            raise typer.Exit(code=2)
        manifest = _read_json(run_dir / "session.json")
        result = _read_json(run_dir / "result.json")
        request = _read_json(run_dir / "request.json")
        source = result or manifest or {}
        typer.echo(f"Run {run_dir.name}")
        typer.echo(f"status: {source.get('status') or source.get('phase') or '?'}")
        provider_name = source.get("provider_name") or source.get("provider") or "?"
        model_name = source.get("model_id") or source.get("model") or "?"
        typer.echo(f"model: {provider_name} / {model_name}")
        if request and request.get("instruction"):
            typer.echo(f"task: {request['instruction']}")
        if source.get("summary"):
            typer.echo(f"summary: {source['summary']}")
        events = _read_events(run_dir / "events.jsonl")
        if not events:
            typer.echo("events: none")
            return
        typer.echo("events:")
        for event in events:
            sequence = event.get("sequence")
            event_type = event.get("event_type")
            detail = _event_detail(event)
            prefix = f"{sequence:>4}" if isinstance(sequence, int) else "   ?"
            line = f"{prefix}  {event_type or '?'}"
            if detail:
                line = f"{line}  {detail}"
            typer.echo(line)
        return

    if replay is not None:
        from rivumi.session_replay import ReplayValidationError, reduce_jsonl

        run_dir = _resolve_run_dir(replay)
        if run_dir is None:
            typer.echo(f"error: no unique run matching {replay!r} under {run_root}", err=True)
            raise typer.Exit(code=2)
        try:
            replay_state = reduce_jsonl(run_dir / "events.jsonl")
        except ReplayValidationError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=2) from exc

        typer.echo(f"Replay {run_dir.name}")
        typer.echo("state:")
        for key, value in replay_state.as_dict().items():
            if key == "timeline":
                continue
            typer.echo(f"  {key}: {json.dumps(value, ensure_ascii=False, sort_keys=True)}")
        typer.echo("timeline:")
        if not replay_state.timeline:
            typer.echo("  none")
            return
        for item in replay_state.timeline:
            parts = [f"{item.sequence:>4}", item.event_type]
            if item.turn_id is not None:
                parts.append(f"turn={item.turn_id}")
            if item.text is not None:
                parts.append(f"text={json.dumps(item.text, ensure_ascii=False)}")
            if item.detail is not None:
                parts.append(f"detail={json.dumps(item.detail, ensure_ascii=False)}")
            typer.echo("  " + "  ".join(parts))
        return

    if replay_json is not None:
        from rivumi.session_replay import ReplayValidationError, reduce_jsonl

        run_dir = _resolve_run_dir(replay_json)
        if run_dir is None:
            typer.echo(f"error: no unique run matching {replay_json!r} under {run_root}", err=True)
            raise typer.Exit(code=2)
        try:
            replay_state = reduce_jsonl(run_dir / "events.jsonl")
        except ReplayValidationError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=2) from exc
        typer.echo(replay_state.canonical_json())
        return

    if fork_from_event is not None:
        from rivumi.session_replay import ReplayValidationError, create_forked_run_from_event

        if sequence is None:
            raise typer.BadParameter("--fork-from-event requires --sequence")
        run_dir = _resolve_run_dir(fork_from_event)
        if run_dir is None:
            typer.echo(
                f"error: no unique run matching {fork_from_event!r} under {run_root}",
                err=True,
            )
            raise typer.Exit(code=2)
        try:
            fork_seed = create_forked_run_from_event(
                source_run_dir=run_dir,
                run_root=run_root,
                sequence=sequence,
            )
        except ReplayValidationError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=2) from exc
        typer.echo(fork_seed.canonical_json())
        return

    if analyze_subagents is not None:
        from rivumi.subagents import analyze_subagent_schedule_jsonl

        run_dir = _resolve_run_dir(analyze_subagents)
        if run_dir is None:
            typer.echo(
                f"error: no unique run matching {analyze_subagents!r} under {run_root}",
                err=True,
            )
            raise typer.Exit(code=2)
        try:
            analysis = analyze_subagent_schedule_jsonl(run_dir / "events.jsonl")
        except ValueError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=2) from exc
        typer.echo(json.dumps(analysis.as_dict(), ensure_ascii=False, sort_keys=True))
        return

    rows: list[tuple[float, str, str, str, str, str]] = []
    if run_root.exists() and not run_root.is_symlink() and run_root.is_dir():
        run_dirs = sorted(
            (path for path in run_root.iterdir() if _safe_session_dir(path)),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for run_dir in run_dirs:
            manifest = _read_json(run_dir / "session.json")
            result = _read_json(run_dir / "result.json")
            request = _read_json(run_dir / "request.json")
            if manifest is None and result is None and request is None:
                continue
            source = manifest or result or request or {}
            status = str(source.get("status") or source.get("phase") or "?")
            model = str(source.get("model_id") or source.get("model") or "?")
            total = _usage_total(source)
            wall = source.get("active_wall_time_seconds")
            wall_text = f"{wall:.0f}s" if isinstance(wall, (int, float)) else "-"
            search_parts = [
                run_dir.name,
                status,
                model,
                source.get("provider_name"),
                source.get("provider"),
                source.get("summary"),
                source.get("changed_files"),
                request.get("instruction") if request else None,
            ]
            search_parts.extend(_event_search_parts(run_dir / "events.jsonl"))
            if not _matches(search_parts):
                continue
            rows.append(
                (
                    run_dir.stat().st_mtime,
                    run_dir.name[:12],
                    status,
                    model,
                    f"{total:,}",
                    wall_text,
                )
            )
            if len(rows) >= limit:
                break

    state_root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    conversation_root = state_root / "rivumi" / "conversations"
    if conversation_root.exists() and not conversation_root.is_symlink():
        from rivumi.conversation import ConversationStore

        try:
            conversations = asyncio.run(ConversationStore(conversation_root).list())
        except (OSError, ValueError):
            conversations = ()
        for conversation in conversations:
            model = conversation.model_override or "-"
            search_parts = [
                conversation.conversation_id,
                "conversation",
                conversation.runtime,
                conversation.model_override,
                conversation.title,
            ]
            if normalized_query is not None:
                try:
                    snapshot = asyncio.run(
                        ConversationStore(conversation_root).load(conversation.conversation_id)
                    )
                except (OSError, ValueError):
                    snapshot = None
                if snapshot is not None:
                    search_parts.extend(_conversation_event_search_parts(snapshot.events))
            if not _matches(search_parts):
                continue
            rows.append(
                (
                    conversation.updated_at.timestamp(),
                    conversation.conversation_id[:12],
                    "conversation",
                    model,
                    "-",
                    "-",
                )
            )
            if len(rows) >= limit:
                break

    rows.sort(key=lambda row: row[0], reverse=True)
    visible = rows[:limit]
    if not visible:
        if normalized_query:
            typer.echo(f"No sessions matching {query!r} under {run_root}")
        else:
            typer.echo(f"No sessions found under {run_root}")
        return
    typer.echo(f"{'ID':<14}{'STATUS':<16}{'MODEL':<24}{'TOKENS':>12}  TIME")
    for _mtime, session_id, status, model, tokens, wall_text in visible:
        typer.echo(f"{session_id:<14}{status:<16}{model:<24}{tokens:>12}  {wall_text}")


@policy_app.command("inspect")
def policy_inspect(
    repository: Annotated[
        Path | None,
        typer.Option("--cd", "-C", "--repo", exists=True, file_okay=False),
    ] = None,
    org_policy: Annotated[
        Path | None,
        typer.Option("--org-policy", help="Override RIVUMI_ORG_POLICY for diagnostics."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable policy diagnostics."),
    ] = False,
) -> None:
    """Show user/org/project policy sources and effective precedence."""

    from rivumi.policy_config import ProjectPolicyError, discover_policy_rules

    repository = repository or Path.cwd()
    config_path = default_cli_config_path()
    try:
        config = load_cli_config(config_path)
        discovery = discover_policy_rules(
            repository=repository,
            user_deny_rules=config.deny_rules,
            user_allow_rules=config.allow_rules,
            user_config_path=config_path,
            org_policy_path=org_policy,
        )
    except (ValueError, ProjectPolicyError) as exc:
        if json_output:
            typer.echo(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        else:
            typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    diagnostics = {
        "ok": True,
        "repository": str(repository.resolve()),
        "precedence": list(discovery.source_precedence),
        "sources": {
            "user": {
                "path": str(discovery.user_config_path) if discovery.user_config_path else None,
                "exists": bool(
                    discovery.user_config_path and discovery.user_config_path.exists()
                ),
                "deny_rules": list(config.deny_rules),
                "allow_rules": list(config.allow_rules),
            },
            "org": {
                "path": str(discovery.org_policy_path) if discovery.org_policy_path else None,
                "exists": bool(
                    discovery.org_policy_path and discovery.org_policy_path.exists()
                ),
                "deny_rules": list(discovery.org_policy.deny_rules),
                "allow_rules": list(discovery.org_policy.allow_rules),
            },
            "project": {
                "path": str(discovery.project_policy_path),
                "exists": discovery.project_policy_path.exists(),
                "deny_rules": list(discovery.project_policy.deny_rules),
                "allow_rules": list(discovery.project_policy.allow_rules),
            },
        },
        "effective": {
            "deny_rules": [rule.raw_spec for rule in discovery.rules.deny_rules],
            "allow_rules": [rule.raw_spec for rule in discovery.rules.allow_rules],
        },
    }
    if json_output:
        typer.echo(json.dumps(diagnostics, ensure_ascii=False, indent=2, sort_keys=True))
        return

    typer.echo("Policy diagnostics")
    typer.echo(f"repository: {diagnostics['repository']}")
    typer.echo("precedence:")
    for index, source in enumerate(discovery.source_precedence, 1):
        typer.echo(f"  {index}. {source}")
    typer.echo("sources:")
    for name, source in diagnostics["sources"].items():
        assert isinstance(source, dict)
        typer.echo(f"  {name}: {source['path'] or 'not configured'}")
        typer.echo(f"    exists: {source['exists']}")
        typer.echo(f"    deny_rules: {json.dumps(source['deny_rules'], ensure_ascii=False)}")
        typer.echo(f"    allow_rules: {json.dumps(source['allow_rules'], ensure_ascii=False)}")
    typer.echo("effective:")
    effective = diagnostics["effective"]
    assert isinstance(effective, dict)
    typer.echo(f"  deny_rules: {json.dumps(effective['deny_rules'], ensure_ascii=False)}")
    typer.echo(f"  allow_rules: {json.dumps(effective['allow_rules'], ensure_ascii=False)}")


@app.command("config")
def configure(
    provider: Annotated[
        str | None,
        typer.Option("--provider", help="Default provider; credentials are never stored."),
    ] = None,
    model: Annotated[str | None, typer.Option("--model", "-m")] = None,
    api_url: Annotated[
        str | None,
        typer.Option("--api-url", help="Default provider endpoint; never include credentials."),
    ] = None,
    clear_api_url: Annotated[
        bool, typer.Option("--clear-api-url", help="Remove the saved API URL default.")
    ] = False,
    interactive: Annotated[
        bool, typer.Option("--interactive", help="Run provider/model setup in this terminal.")
    ] = False,
) -> None:
    """Show or update non-secret provider defaults."""

    path = default_cli_config_path()
    try:
        with _STARTUP.span("config.load"):
            current = load_cli_config(path)
        if interactive:
            if provider is not None or model is not None or api_url is not None or clear_api_url:
                raise typer.BadParameter(
                    "--interactive cannot be combined with config value options"
                )
            _interactive_setup(current=current)
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
            runtime="rivumi-agent",
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


@app.command("gateway")
def serve_gateway(
    model: Annotated[str | None, typer.Option("--model", "-m", envvar="CODING_AGENT_MODEL")] = None,
    provider: Annotated[
        str | None,
        typer.Option("--provider", envvar=["RIVUMI_PROVIDER", "PCA_PROVIDER"]),
    ] = None,
    api_url: Annotated[
        str | None,
        typer.Option("--api-url", envvar=["RIVUMI_API_URL", "PCA_API_URL"]),
    ] = None,
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", min=1, max=65535)] = 8787,
    bearer_token: Annotated[
        str | None,
        typer.Option(
            "--bearer-token", envvar=["RIVUMI_GATEWAY_TOKEN", "PCA_GATEWAY_TOKEN"]
        ),
    ] = None,
    experimental_subscription: Annotated[bool, typer.Option("--experimental-subscription")] = False,
    allow_custom_provider_endpoint: Annotated[
        bool, typer.Option("--allow-custom-provider-endpoint")
    ] = False,
) -> None:
    """Expose one configured provider through a bounded OpenAI Chat gateway."""

    import uvicorn

    from rivumi.gateway import ModelGateway

    if host not in {"localhost", "127.0.0.1", "::1"}:
        raise typer.BadParameter(
            "the MVP gateway only binds loopback; put an authenticated TLS proxy in front later"
        )
    provider, model, api_url = _resolve_cli_settings(
        provider=provider,
        model=model,
        api_url=api_url,
    )
    if model is None:
        raise typer.BadParameter("--model is required when no config default exists")
    selected_model = _model_from_env(
        provider=provider,
        model=model,
        base_url=api_url,
        tool_calling=True,
        allow_custom_provider_endpoint=allow_custom_provider_endpoint,
        experimental_subscription=experimental_subscription,
    )
    gateway = ModelGateway(selected_model, bearer_token=bearer_token)
    uvicorn.run(gateway, host=host, port=port, lifespan="on")


@app.command("conversation-server")
def serve_conversation_server(
    repository: Annotated[
        Path | None,
        typer.Option("--repo", "--cd", "-C", exists=True, file_okay=False),
    ] = None,
    runtime: Annotated[
        str,
        typer.Option("--runtime", help="Native conversation runtime to attach."),
    ] = "codex-cli",
    model: Annotated[str | None, typer.Option("--model", "-m")] = None,
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", min=1, max=65535)] = 8788,
    path: Annotated[str, typer.Option("--path")] = "/v1/conversation/attach",
) -> None:
    """Expose a native conversation runtime through the WebSocket attach protocol."""

    import uvicorn

    from rivumi.conversation_websocket import ConversationWebSocketApp

    if host not in {"localhost", "127.0.0.1", "::1"}:
        raise typer.BadParameter("conversation-server only binds loopback")
    repository = repository or Path.cwd()
    adapter = runtime_registry.RUNTIME_REGISTRY.get(runtime)
    if adapter is None or adapter.native_session is None:
        raise typer.BadParameter("runtime does not expose a native conversation session")
    session_cls = runtime_registry._resolve_class(adapter.native_session)
    session = session_cls(source_repository=repository, model=model)
    uvicorn.run(ConversationWebSocketApp(session, path=path), host=host, port=port, lifespan="off")


@app.command("exec")
@app.command("run")
def run(
    prompt: Annotated[str | None, typer.Argument(help="Bounded coding task")] = None,
    repository: Annotated[
        Path | None,
        typer.Option("--cd", "-C", "--repo", exists=True, file_okay=False),
    ] = None,
    instruction: Annotated[
        str | None, typer.Option("--task", "-t", help="Compatibility alias for PROMPT")
    ] = None,
    model: Annotated[str | None, typer.Option("--model", "-m", envvar="CODING_AGENT_MODEL")] = None,
    check: Annotated[
        list[str] | None,
        typer.Option("--check", help="Exact verification argv; repeatable"),
    ] = None,
    provider: Annotated[
        str | None,
        typer.Option(
            "--provider",
            envvar=["RIVUMI_PROVIDER", "PCA_PROVIDER"],
            help=("openai-compatible, ollama, openai-codex, anthropic, gemini, or workers-ai"),
        ),
    ] = None,
    allowed_path: Annotated[
        list[str] | None,
        typer.Option("--allowed-path", help="Allowed repository glob; repeatable"),
    ] = None,
    base_sha: Annotated[str | None, typer.Option("--base-sha")] = None,
    base_url: Annotated[
        str | None,
        typer.Option(
            "--api-url", "--base-url", envvar=["RIVUMI_API_URL", "PCA_API_URL"]
        ),
    ] = None,
    run_root: Annotated[Path, typer.Option("--run-root")] = DEFAULT_RUN_ROOT,
    max_steps: Annotated[int, typer.Option("--max-steps", min=1)] = 12,
    wall_time_seconds: Annotated[float, typer.Option("--wall-time", min=1)] = 900,
    tool_calling: Annotated[
        bool,
        typer.Option(
            "--tool-calling/--no-tool-calling",
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
    experimental_subscription: Annotated[bool, typer.Option("--experimental-subscription")] = False,
    unsafe_local_exec: Annotated[
        bool,
        typer.Option(
            "--unsafe-local-exec",
            help=(
                "Acknowledge that checks execute trusted repository code; "
                "use --no-sandbox-checks only for a trusted local escape hatch."
            ),
        ),
    ] = False,
    sandbox_checks: Annotated[
        bool,
        typer.Option(
            "--sandbox-checks/--no-sandbox-checks",
            help="Run native verification checks through the local OS sandbox.",
        ),
    ] = True,
) -> None:
    """Run one non-interactive task (Codex-style `exec`; `run` remains an alias)."""

    from rivumi.loop import AgentRunner, UnsafeLocalExecutionError

    instruction = _prompt_or_task(prompt, instruction)
    if instruction is None:
        raise typer.BadParameter("PROMPT or --task is required")
    repository = repository or Path.cwd()
    try:
        with _STARTUP.span("config.load"):
            current_config = load_cli_config()
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(f"CLI config could not be loaded: {exc}") from exc
    permission_guard, _guard_needed = _permission_guard_from_config(
        config=current_config,
        repository=repository,
    )
    provider, model, base_url = _resolve_cli_settings(
        provider=provider,
        model=model,
        api_url=base_url,
        allow_model_role_alias=True,
    )
    if model is None:
        raise typer.BadParameter("--model is required when no config default exists")
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
                    permission_guard=permission_guard,
                    sandbox_profile=current_config.sandbox_profile,
                    sandbox_backend=current_config.sandbox_backend,
                    sandbox_read_roots=tuple(
                        Path(root).expanduser()
                        for root in current_config.sandbox_read_roots
                    ),
                    sandbox_checks=_effective_sandbox_checks(sandbox_checks),
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


class ModelBundleResource:
    def __init__(self, *models: ModelProvider | None) -> None:
        self._models = tuple(model for model in models if model is not None)

    async def aclose(self) -> None:
        for model in self._models:
            await model.aclose()


async def _run_and_close(runner: AgentRunner, *models: ModelProvider | None):
    try:
        return await runner.run()
    finally:
        await ModelBundleResource(*models).aclose()


@app.command("export-otel")
def export_otel(
    run_id: Annotated[str, typer.Argument(help="Run id (or 'last')")],
    run_root: Annotated[Path, typer.Option("--run-root")] = DEFAULT_RUN_ROOT,
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Write OTLP-JSON to a file")
    ] = None,
) -> None:
    """Export a run as OpenTelemetry GenAI OTLP-JSON."""

    from rivumi.otel_export import export_run

    run_dir = run_root / run_id
    if run_id == "last" or not run_dir.exists():
        candidates = sorted(
            (
                path
                for path in run_root.glob("*/result.json")
                if path.parent.name.startswith(run_id)
            ),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            typer.echo(f"error: no run matching '{run_id}' under {run_root}", err=True)
            raise typer.Exit(code=2)
        run_dir = candidates[0].parent
    try:
        payload = export_run(run_dir, output)
    except FileNotFoundError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    if output is None:
        typer.echo(payload)


async def _resume_and_close(
    run_dir: Path,
    model: ModelProvider,
    *,
    approval_policy: TTYApprovalPolicy,
    event_sink: ConsoleEventSink,
):
    from rivumi.loop import AgentRunner
    from rivumi.permissions import PermissionGuard

    try:
        runner = await AgentRunner.resume(
            run_dir,
            model,
            approval_policy=approval_policy,
            permission_guard=PermissionGuard(),
            event_sink=event_sink,
        )
        return await runner.run()
    finally:
        await model.aclose()


if __name__ == "__main__":
    app()

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
    from rivumi.conversation_controller import ConversationController
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
            controller = ConversationController(session)
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
app.add_typer(auth_app, name="auth")
app.add_typer(backend_app, name="backend")


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


def _resolve_cli_settings(
    *,
    provider: str | None,
    model: str | None,
    api_url: str | None,
) -> tuple[str, str | None, str | None]:
    try:
        with _STARTUP.span("config.load"):
            config = load_cli_config()
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(f"CLI config could not be loaded: {exc}") from exc

    model_provider: str | None = None
    if model and "/" in model:
        candidate, unqualified = model.split("/", 1)
        if candidate in SUPPORTED_PROVIDERS and unqualified:
            model_provider = candidate
            model = unqualified
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
    configured = CliConfig(runtime="rivumi-agent", provider=provider, model=model, api_url=api_url)
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
) -> None:
    """Start this agent's own loop in the current repository."""

    from rivumi.approvals import TTYApprovalPolicy
    from rivumi.console import ConsoleEventSink
    from rivumi.conversation import ConversationStore
    from rivumi.conversation_controller import decide_runtime_approval
    from rivumi.external_runner import ExternalCodingRunner
    from rivumi.loop import AgentRunner, UnsafeLocalExecutionError

    requested_provider = provider
    requested_model = model
    requested_api_url = api_url
    if print_mode and prompt in SUPPORTED_PROVIDERS and provider is None:
        raise typer.BadParameter("-p now means --print; select a provider with --provider instead")
    instruction = _prompt_or_task(prompt, instruction)
    provider, model, api_url = _resolve_cli_settings(
        provider=provider,
        model=model,
        api_url=api_url,
    )
    repository = repository or Path.cwd()
    if not print_mode and not plain and _terminal_supports_tui():
        try:
            with _STARTUP.span("config.load"):
                current = load_cli_config()
        except (OSError, ValueError) as exc:
            raise typer.BadParameter(f"CLI config could not be loaded: {exc}") from exc
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

        def make_runner(request: TuiRunRequest, approval_policy, event_sink):
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
                    event_sink=event_sink,
                ),
                selected_model,
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
        try:
            with _STARTUP.span("config.load"):
                current = load_cli_config()
        except (OSError, ValueError) as exc:
            raise typer.BadParameter(f"CLI config could not be loaded: {exc}") from exc
        preferred_provider = requested_provider or current.provider
        setup_current = CliConfig(
            provider=preferred_provider,
            model=current.model if preferred_provider == current.provider else None,
            api_url=api_url,
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
                    approval_policy=(
                        None if print_mode else TTYApprovalPolicy(sys.stdin, sys.stderr)
                    ),
                    event_sink=None if print_mode else ConsoleEventSink(sys.stderr),
                ),
                selected_model,
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
        return OpenAICompatibleModel(
            model=model,
            api_key=api_key,
            base_url=base_url or _SIMPLE_API_KEY_BASE_URLS[provider],
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
            help="Acknowledge that checks execute trusted repository code without a sandbox.",
        ),
    ] = False,
) -> None:
    """Run one non-interactive task (Codex-style `exec`; `run` remains an alias)."""

    from rivumi.loop import AgentRunner, UnsafeLocalExecutionError

    instruction = _prompt_or_task(prompt, instruction)
    if instruction is None:
        raise typer.BadParameter("PROMPT or --task is required")
    provider, model, base_url = _resolve_cli_settings(
        provider=provider,
        model=model,
        api_url=base_url,
    )
    if model is None:
        raise typer.BadParameter("--model is required when no config default exists")
    commands = _commands(check)
    task = TaskContract(
        repository=repository or Path.cwd(),
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
    from rivumi.loop import AgentRunner

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

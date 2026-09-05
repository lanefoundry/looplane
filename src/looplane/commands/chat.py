"""Interactive and headless route orchestration."""

from __future__ import annotations

import asyncio
import contextlib
import shlex
import sys
from pathlib import Path

import typer

import looplane.runtime_registry as runtime_registry
from looplane.cli_config import (
    SUPPORTED_PROVIDERS,
    CliConfig,
    load_cli_config,
)
from looplane.commands import bootstrap as _bootstrap
from looplane.commands import common as _common
from looplane.commands import onboarding as _onboarding
from looplane.commands import paths as _paths
from looplane.commands import policy as _policy
from looplane.commands import settings as _settings
from looplane.commands import terminal_io as _terminal_io
from looplane.commands.ports import CommandServices
from looplane.contracts import Limits, TaskContract


def chat(
    prompt: str | None = None,
    repository: Path | None = None,
    instruction: str | None = None,
    print_mode: bool = False,
    plain: bool = False,
    no_alt_screen: bool = False,
    provider: str | None = None,
    model: str | None = None,
    check: list[str] | None = None,
    api_url: str | None = None,
    run_root: Path = _paths.DEFAULT_RUN_ROOT,
    experimental_subscription: bool = False,
    allow_custom_provider_endpoint: bool = False,
    unsafe_local_exec: bool = False,
    dangerous: bool = False,
    edit_real_repo: bool = False,
    deny_tool: list[str] | None = None,
    fallback_model: list[str] | None = None,
    auto_review: bool = False,
    sandbox_checks: bool = True,
    *,
    services: CommandServices,
) -> None:
    """Start this agent's own loop in the current repository."""

    services.startup.mark("cli_routed")

    from looplane.approvals import HeadlessApprovalPolicy, TTYApprovalPolicy
    from looplane.console import ConsoleEventSink
    from looplane.conversation import ConversationStore

    _, UnsafeLocalExecutionError = services.runtime.native_runtime()
    from looplane.permissions import GuardedApprovalPolicy

    requested_provider = provider
    requested_model = model
    requested_api_url = api_url
    repository = repository or Path.cwd()
    try:
        with services.startup.span("config.load"):
            current_config = load_cli_config()
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(f"CLI config could not be loaded: {exc}") from exc
    permission_guard, guard_needed = _policy._permission_guard_from_config(
        config=current_config,
        repository=repository,
        deny_tool=deny_tool,
        dangerous=dangerous,
        services=services,
    )
    _policy._confirm_direct_edit_with_dangerous_mode(
        edit_real_repo=edit_real_repo, dangerous=dangerous, services=services
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

    model_selection = _bootstrap.ModelSelection(
        services=services,
        fallback_specs=tuple(fallback_model or ()),
        auto_review=auto_review,
        allow_custom_provider_endpoint=allow_custom_provider_endpoint,
        experimental_subscription=experimental_subscription,
    )

    if print_mode and prompt in SUPPORTED_PROVIDERS and provider is None:
        raise typer.BadParameter("-p now means --print; select a provider with --provider instead")
    instruction = _common._prompt_or_task(prompt, instruction)
    provider, model, api_url = _settings._resolve_cli_settings(
        provider=provider,
        model=model,
        api_url=api_url,
        allow_model_role_alias=True,
        services=services,
    )
    if not print_mode and not plain and services.supports_tui():
        _terminal_io._validate_tui_terminal_size(services=services)
        current = current_config
        explicit_looplane_runtime = any(
            value is not None for value in (requested_provider, requested_model, requested_api_url)
        )
        if explicit_looplane_runtime:
            selected_provider = requested_provider or current.provider or provider
            initial_config = CliConfig(
                runtime="looplane-agent",
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
            looplaneApp = services.runtime.terminal_app()
        except ModuleNotFoundError as exc:
            if exc.name == "textual" or (exc.name or "").startswith("textual."):
                refresh_script = Path(__file__).resolve().parents[3] / "scripts" / "install-dev-cli"
                refresh_command = (
                    shlex.join([str(refresh_script)])
                    if refresh_script.is_file()
                    else "uv tool install --force looplane"
                )
                raise typer.BadParameter(
                    "Full-screen UI dependencies are out of sync. Refresh this editable "
                    f"installation with: {refresh_command}"
                ) from None
            raise

        runtime_factory = _bootstrap.ChatRuntimeFactory(
            services=services,
            repository=repository,
            check=check,
            run_root=run_root,
            unsafe_local_exec=unsafe_local_exec,
            edit_real_repo=edit_real_repo,
            permission_guard=permission_guard,
            sandbox_checks=sandbox_checks,
            initial_config=initial_config,
            allow_custom_provider_endpoint=allow_custom_provider_endpoint,
            experimental_subscription=experimental_subscription,
            model_selection=model_selection,
            guarded=guarded,
        )

        async def _warmup_native_controller() -> None:
            # The root adapts the temporary app context API to a typed callback.
            with contextlib.suppress(BaseException):
                await runtime_factory.warmup(services.runtime.terminal_context_id(tui_app))

        runtimes = runtime_registry.runtime_options()
        runtime_models = runtime_registry.runtime_model_map()
        services.startup.mark("runtime_discovered")

        tui_app = looplaneApp(
            repository=repository,
            config=initial_config,
            runner_factory=runtime_factory.make_runner,
            runtimes=runtimes,
            runtime_models=runtime_models,
            providers=_onboarding.ONBOARDING_PROVIDERS,
            ollama_models=(
                services.discover_models()
                if initial_config.model is None or initial_config.runtime is None
                else ()
            ),
            initial_prompt=instruction,
            locked_provider=requested_provider,
            conversation_store=ConversationStore(),
            runner_warmup=_warmup_native_controller,
        )
        services.startup.mark("tui_constructed")
        result = tui_app.run(**({"inline": True} if no_alt_screen else {}))
        final_transcript = tui_app.final_transcript_text
        if final_transcript:
            typer.echo(final_transcript)
        if tui_app.last_error is not None:
            typer.echo(f"error: {tui_app.last_error}", err=True)
            raise typer.Exit(code=2)
        if result is not None and result.status != "completed":
            raise typer.Exit(code=1)
        return
    headless = print_mode or not services.stdin_is_tty()
    if model is None:
        if headless:
            raise typer.BadParameter(
                "no model is configured; run `looplane config --interactive` in a terminal or pass "
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
        configured = services.interactive_setup(
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
        _terminal_io._show_context_header(repository=repository, provider=provider, model=model)
    if hint := _bootstrap._credential_hint(provider, api_url=api_url):
        typer.secho(f"Provider is not ready. {hint}", fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(code=2)
    if headless and instruction is None:
        raise typer.BadParameter("PROMPT is required in non-interactive mode")
    if instruction is None:
        typer.echo("\nWhat would you like me to do in this repository?")
        instruction = typer.prompt("›", prompt_suffix=" ")
    try:
        selected_model = services.model_factory(
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
            verification=_common._commands(check),
        )
        result = asyncio.run(
            _bootstrap._run_and_close(
                _bootstrap.build_native_runner(
                    services,
                    task,
                    selected_model,
                    run_root,
                    allow_unsafe_local_exec=unsafe_local_exec,
                    allow_direct_repo_edit=edit_real_repo,
                    approval_policy=guarded(
                        None if print_mode else TTYApprovalPolicy(sys.stdin, sys.stderr)
                    ),
                    permission_guard=permission_guard,
                    fallback_models=model_selection.build_fallback_models(),
                    review_model=model_selection.build_review_model(provider),
                    sandbox_checks=_policy._effective_sandbox_checks(sandbox_checks),
                    sandbox_profile=current_config.sandbox_profile,
                    sandbox_backend=current_config.sandbox_backend,
                    sandbox_read_roots=tuple(
                        Path(root).expanduser() for root in current_config.sandbox_read_roots
                    ),
                    event_sink=None if print_mode else ConsoleEventSink(sys.stderr),
                ),
                selected_model,
                model_selection.build_review_model(provider),
            )
        )
    except (UnsafeLocalExecutionError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    if print_mode:
        typer.echo(result.model_dump_json(indent=2))
    else:
        _common._show_result(result)
    if result.status != "completed":
        raise typer.Exit(code=1)


def run(
    prompt: str | None = None,
    repository: Path | None = None,
    instruction: str | None = None,
    model: str | None = None,
    check: list[str] | None = None,
    provider: str | None = None,
    allowed_path: list[str] | None = None,
    base_sha: str | None = None,
    base_url: str | None = None,
    run_root: Path = _paths.DEFAULT_RUN_ROOT,
    max_steps: int = 12,
    wall_time_seconds: float = 900,
    tool_calling: bool = False,
    allow_custom_provider_endpoint: bool = False,
    experimental_subscription: bool = False,
    unsafe_local_exec: bool = False,
    sandbox_checks: bool = True,
    dialect: str = "auto",
    *,
    services: CommandServices,
) -> None:
    """Run one non-interactive task (Codex-style `exec`; `run` remains an alias)."""

    _, UnsafeLocalExecutionError = services.runtime.native_runtime()

    instruction = _common._prompt_or_task(prompt, instruction)
    if instruction is None:
        raise typer.BadParameter("PROMPT or --task is required")
    repository = repository or Path.cwd()
    try:
        with services.startup.span("config.load"):
            current_config = load_cli_config()
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(f"CLI config could not be loaded: {exc}") from exc
    permission_guard, _guard_needed = _policy._permission_guard_from_config(
        config=current_config, repository=repository, services=services
    )
    provider, model, base_url = _settings._resolve_cli_settings(
        provider=provider,
        model=model,
        api_url=base_url,
        allow_model_role_alias=True,
        services=services,
    )
    if model is None:
        raise typer.BadParameter("--model is required when no config default exists")
    commands = _common._commands(check)
    task = TaskContract(
        repository=repository,
        instruction=instruction,
        allowed_paths=tuple(allowed_path or ("**",)),
        verification=commands,
        limits=Limits(max_steps=max_steps, wall_time_seconds=wall_time_seconds),
        base_sha=base_sha,
    )
    selected_model = services.model_factory(
        provider=provider,
        model=model,
        base_url=base_url,
        tool_calling=tool_calling,
        allow_custom_provider_endpoint=allow_custom_provider_endpoint,
        experimental_subscription=experimental_subscription,
        dialect_flag=dialect,
    )
    try:
        result = asyncio.run(
            _bootstrap._run_and_close(
                _bootstrap.build_native_runner(
                    services,
                    task,
                    selected_model,
                    run_root,
                    allow_unsafe_local_exec=unsafe_local_exec,
                    permission_guard=permission_guard,
                    sandbox_profile=current_config.sandbox_profile,
                    sandbox_backend=current_config.sandbox_backend,
                    sandbox_read_roots=tuple(
                        Path(root).expanduser() for root in current_config.sandbox_read_roots
                    ),
                    sandbox_checks=_policy._effective_sandbox_checks(sandbox_checks),
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

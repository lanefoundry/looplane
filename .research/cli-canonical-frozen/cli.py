"""Typer declarations and lazy compatibility composition for looplane."""

from __future__ import annotations

import os
import sys as sys
import webbrowser as webbrowser
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from typer.core import TyperCommand, TyperGroup

from looplane.cli_config import CliConfig
from looplane.commands.paths import DEFAULT_RUN_ROOT
from looplane.startup_trace import _STARTUP

if TYPE_CHECKING:
    from looplane import runtime_registry
    from looplane.approvals import TTYApprovalPolicy
    from looplane.commands.bootstrap import NativeControllerCache
    from looplane.commands.ports import CommandServices
    from looplane.console import ConsoleEventSink
    from looplane.conversation_controller import BackendTurnLimiter, ConversationController
    from looplane.models import ModelProvider
_STARTUP.mark("imports_done")


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
            os.environ.get(name) for name in ("_LOOPLANE_COMPLETE", "_ROOT_COMPLETE")
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
        "Daily use: looplane [PROMPT] | looplane -p [PROMPT] | looplane exec [PROMPT] | "
        "looplane resume. Primary options: -C/--cd/--repo, -m/--model, --provider, "
        "--api-url, --check, --plain, --no-alt-screen. Save non-secret defaults with "
        "looplane config."
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


def _native_runtime():
    from looplane.loop import AgentRunner, UnsafeLocalExecutionError

    return AgentRunner, UnsafeLocalExecutionError


def _terminal_app():
    from looplane.tui import looplaneApp

    return looplaneApp


async def _start_controller(controller):
    await controller._ensure_started()


def _command_services() -> CommandServices:
    from looplane.commands.ports import CommandServices, RuntimePorts

    return CommandServices(
        startup=_STARTUP,
        model_factory=_model_from_env,
        stdin_is_tty=_stdin_is_tty,
        supports_tui=_terminal_supports_tui,
        terminal_size=_terminal_size,
        interactive_setup=_interactive_setup,
        discover_models=_discover_local_ollama_models,
        credential_path=_codex_credential_path,
        fetch_models=_fetch_ollama_models,
        runtime=RuntimePorts(
            native_runtime=_native_runtime,
            terminal_app=_terminal_app,
            terminal_context_id=lambda application: application._runtime_context_id,
            start_controller=_start_controller,
        ),
    )


def _codex_credential_path() -> Path:
    from looplane.commands import paths

    return paths._codex_credential_path()


@plugin_app.command("list")
def plugin_list(
    repo: Annotated[Path | None, typer.Option("--repo", "-C", help="Repository root.")] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    "List installed repository-local plugin manifests."

    from looplane.commands import plugins

    return plugins.plugin_list(repo=repo, json_output=json_output)


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
    "Install a local plugin manifest and referenced skills into `.looplane/plugins`."

    from looplane.commands import plugins

    return plugins.plugin_install(
        manifest=manifest, repo=repo, name=name, overwrite=overwrite, json_output=json_output
    )


def _stdin_is_tty() -> bool:
    from looplane.commands import terminal_io

    return terminal_io._stdin_is_tty()


def _terminal_supports_tui() -> bool:
    from looplane.commands import terminal_io

    return terminal_io._terminal_supports_tui(services=_command_services())


def _terminal_size() -> os.terminal_size | None:
    "Return the active output terminal size when it can be queried."

    from looplane.commands import terminal_io

    return terminal_io._terminal_size()


def _fetch_ollama_models() -> tuple[str, ...]:
    "Return bounded model names from the fixed loopback Ollama discovery endpoint."

    from looplane.commands import onboarding

    return onboarding._fetch_ollama_models()


def _discover_local_ollama_models() -> tuple[str, ...]:
    "Cached, single-flight wrapper around :func:`_fetch_ollama_models`."

    from looplane.commands import onboarding

    return onboarding._discover_local_ollama_models(services=_command_services())


def _interactive_setup(
    *,
    current: CliConfig | None = None,
    locked_provider: str | None = None,
) -> CliConfig:
    "Run provider-aware setup and persist no credential material."

    from looplane.commands import onboarding

    return onboarding._interactive_setup(
        current=current, locked_provider=locked_provider, services=_command_services()
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
    no_alt_screen: Annotated[
        bool,
        typer.Option(
            "--no-alt-screen",
            help=(
                "Run the interactive UI inline in the normal terminal buffer; "
                "useful for scrollback and terminal accessibility."
            ),
        ),
    ] = False,
    provider: Annotated[
        str | None,
        typer.Option("--provider", envvar=["LOOPLANE_PROVIDER", "PCA_PROVIDER"]),
    ] = None,
    model: Annotated[str | None, typer.Option("--model", "-m", envvar="CODING_AGENT_MODEL")] = None,
    check: Annotated[
        list[str] | None, typer.Option("--check", help="Exact verification argv; repeatable")
    ] = None,
    api_url: Annotated[
        str | None,
        typer.Option(
            "--api-url",
            envvar=["LOOPLANE_API_URL", "PCA_API_URL"],
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
    edit_real_repo: Annotated[
        bool,
        typer.Option(
            "--edit-real-repo",
            help=(
                "Let the agent edit this repository's real working tree directly "
                "instead of a disposable clone. Diffs are still shown for approval "
                "unless combined with --dangerous."
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
    "Start this agent's own loop in the current repository."

    from looplane.commands import chat

    return chat.chat(
        prompt=prompt,
        repository=repository,
        instruction=instruction,
        print_mode=print_mode,
        plain=plain,
        no_alt_screen=no_alt_screen,
        provider=provider,
        model=model,
        check=check,
        api_url=api_url,
        run_root=run_root,
        experimental_subscription=experimental_subscription,
        allow_custom_provider_endpoint=allow_custom_provider_endpoint,
        unsafe_local_exec=unsafe_local_exec,
        dangerous=dangerous,
        edit_real_repo=edit_real_repo,
        deny_tool=deny_tool,
        fallback_model=fallback_model,
        auto_review=auto_review,
        sandbox_checks=sandbox_checks,
        services=_command_services(),
    )


def _model_from_env(
    *,
    provider: str,
    model: str,
    base_url: str | None,
    tool_calling: bool,
    allow_custom_provider_endpoint: bool,
    experimental_subscription: bool = False,
    dialect_flag: str = "auto",
) -> ModelProvider:
    from looplane.commands import bootstrap

    return bootstrap._model_from_env(
        provider=provider,
        model=model,
        base_url=base_url,
        tool_calling=tool_calling,
        allow_custom_provider_endpoint=allow_custom_provider_endpoint,
        experimental_subscription=experimental_subscription,
        dialect_flag=dialect_flag,
        services=_command_services(),
    )


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
            help="Approve this external CLI editing only looplane's disposable clone.",
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
    "Let official Claude Code edit a disposable clone, then audit it with looplane."

    from looplane.commands import external

    return external.run_claude_code_backend(
        prompt=prompt,
        instruction=instruction,
        repository=repository,
        check=check,
        allowed_path=allowed_path,
        run_root=run_root,
        task_id=task_id,
        timeout_seconds=timeout_seconds,
        experimental_subscription=experimental_subscription,
        allow_external_modify=allow_external_modify,
        unsafe_local_exec=unsafe_local_exec,
    )


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
            help="Approve this external CLI editing only looplane's disposable clone.",
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
    "Let official Codex CLI edit a sandboxed clone, then audit it with looplane."

    from looplane.commands import external

    return external.run_codex_cli_backend(
        prompt=prompt,
        instruction=instruction,
        repository=repository,
        check=check,
        allowed_path=allowed_path,
        run_root=run_root,
        task_id=task_id,
        timeout_seconds=timeout_seconds,
        experimental_subscription=experimental_subscription,
        allow_external_modify=allow_external_modify,
        unsafe_local_exec=unsafe_local_exec,
    )


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
        bool, typer.Option("--allow-external-modify", help="Approve CLI edits in looplane's clone.")
    ] = False,
    unsafe_local_exec: Annotated[
        bool, typer.Option("--unsafe-local-exec", help="Allow trusted repo checks to run on host.")
    ] = False,
) -> None:
    "Delegate to the installed OpenCode CLI in an isolated clone, then audit it with looplane."

    from looplane.commands import external

    return external._run_opencode_backend(
        prompt=prompt,
        instruction=instruction,
        repository=repository,
        check=check,
        allowed_path=allowed_path,
        model=model,
        run_root=run_root,
        task_id=task_id,
        timeout_seconds=timeout_seconds,
        allow_external_modify=allow_external_modify,
        unsafe_local_exec=unsafe_local_exec,
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
        bool, typer.Option("--allow-external-modify", help="Approve CLI edits in looplane's clone.")
    ] = False,
    unsafe_local_exec: Annotated[
        bool, typer.Option("--unsafe-local-exec", help="Allow trusted repo checks to run on host.")
    ] = False,
) -> None:
    "Delegate to the installed Pi coding agent in an isolated clone,\nthen audit it with looplane."

    from looplane.commands import external

    return external._run_pi_backend(
        prompt=prompt,
        instruction=instruction,
        repository=repository,
        check=check,
        allowed_path=allowed_path,
        model=model,
        run_root=run_root,
        task_id=task_id,
        timeout_seconds=timeout_seconds,
        allow_external_modify=allow_external_modify,
        unsafe_local_exec=unsafe_local_exec,
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
        bool, typer.Option("--allow-external-modify", help="Approve CLI edits in looplane's clone.")
    ] = False,
    unsafe_local_exec: Annotated[
        bool, typer.Option("--unsafe-local-exec", help="Allow trusted repo checks to run on host.")
    ] = False,
) -> None:
    "Delegate to the installed OMP agent in an isolated clone, then audit it with looplane."

    from looplane.commands import external

    return external._run_omp_backend(
        prompt=prompt,
        instruction=instruction,
        repository=repository,
        check=check,
        allowed_path=allowed_path,
        model=model,
        run_root=run_root,
        task_id=task_id,
        timeout_seconds=timeout_seconds,
        allow_external_modify=allow_external_modify,
        unsafe_local_exec=unsafe_local_exec,
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
    "Create this application's own experimental ChatGPT/Codex OAuth grant."

    from looplane.commands import auth

    return auth.login_codex(
        timeout_seconds=timeout_seconds, manual=manual, services=_command_services()
    )


@auth_app.command("status-codex")
def status_codex() -> None:
    "Report redacted status for this application's Codex grant."

    from looplane.commands import auth

    return auth.status_codex(services=_command_services())


@auth_app.command("logout-codex")
def logout_codex() -> None:
    "Delete this application's Codex grant without touching another CLI."

    from looplane.commands import auth

    return auth.logout_codex(services=_command_services())


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
    "Create this application's own MCP OAuth authorization-code grant."

    from looplane.commands import auth

    return auth.login_mcp(server=server, repository=repository, manual=manual)


@auth_app.command("status-mcp")
def status_mcp(
    server: Annotated[str, typer.Argument(help="MCP server name")],
) -> None:
    "Report redacted status for one looplane-owned MCP OAuth grant."

    from looplane.commands import auth

    return auth.status_mcp(server=server)


@auth_app.command("logout-mcp")
def logout_mcp(
    server: Annotated[str, typer.Argument(help="MCP server name")],
) -> None:
    "Delete this application's MCP OAuth grant for one server."

    from looplane.commands import auth

    return auth.logout_mcp(server=server)


@auth_app.command("set-key")
def auth_set_key(
    provider: Annotated[
        str,
        typer.Argument(help="anthropic | gemini | openai-compatible | workers-ai"),
    ],
) -> None:
    "Store an API key/secret for a looplane-agent provider, local to this application only."

    from looplane.commands import auth

    return auth.auth_set_key(provider=provider, services=_command_services())


@auth_app.command("clear-key")
def auth_clear_key(
    provider: Annotated[
        str,
        typer.Argument(help="anthropic | gemini | openai-compatible | workers-ai"),
    ],
) -> None:
    "Delete a stored looplane-agent provider credential."

    from looplane.commands import auth

    return auth.auth_clear_key(provider=provider)


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
    """Show which looplane-agent providers have stored credentials, and their status.

    Without --verify this only reads local state (env vars / the credential store) and
    never touches the network, so it stays fast. --verify calls each already-configured
    provider's API once, the same connection check `auth set-key` runs after saving.
    """

    from looplane.commands import auth

    return auth.auth_list(verify=verify)


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
    "Upload all provider keys and deploy their hosted profiles in one batch."

    from looplane.commands import serve

    return serve.cloudflare_providers_apply(
        manifest=manifest,
        secrets_env=secrets_env,
        cloudflare_dir=cloudflare_dir,
        wrangler_env=wrangler_env,
        dry_run=dry_run,
        allow_custom_endpoint=allow_custom_endpoint,
    )


@app.command()
def resume(
    session: Annotated[str, typer.Argument(help="Session id or 'last'")] = "last",
    run_root: Annotated[Path, typer.Option("--run-root")] = DEFAULT_RUN_ROOT,
    api_url: Annotated[
        str | None,
        typer.Option("--api-url", envvar=["LOOPLANE_API_URL", "PCA_API_URL"]),
    ] = None,
    experimental_subscription: Annotated[bool, typer.Option("--experimental-subscription")] = False,
    allow_custom_provider_endpoint: Annotated[
        bool, typer.Option("--allow-custom-provider-endpoint")
    ] = False,
) -> None:
    "Resume a validated non-terminal session in its existing disposable workspace."

    from looplane.commands import sessions

    return sessions.resume(
        session=session,
        run_root=run_root,
        api_url=api_url,
        experimental_subscription=experimental_subscription,
        allow_custom_provider_endpoint=allow_custom_provider_endpoint,
        services=_command_services(),
    )


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
    "List recent agent runs and saved conversations with their usage."

    from looplane.commands import sessions

    return sessions.sessions(
        run_root=run_root,
        limit=limit,
        show=show,
        replay=replay,
        replay_json=replay_json,
        fork_from_event=fork_from_event,
        analyze_subagents=analyze_subagents,
        sequence=sequence,
        query=query,
    )


@policy_app.command("inspect")
def policy_inspect(
    repository: Annotated[
        Path | None,
        typer.Option("--cd", "-C", "--repo", exists=True, file_okay=False),
    ] = None,
    org_policy: Annotated[
        Path | None,
        typer.Option("--org-policy", help="Override LOOPLANE_ORG_POLICY for diagnostics."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable policy diagnostics."),
    ] = False,
) -> None:
    "Show user/org/project policy sources and effective precedence."

    from looplane.commands import policy

    return policy.policy_inspect(
        repository=repository, org_policy=org_policy, json_output=json_output
    )


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
    "Show or update non-secret provider defaults."

    from looplane.commands import onboarding

    return onboarding.configure(
        provider=provider,
        model=model,
        api_url=api_url,
        clear_api_url=clear_api_url,
        interactive=interactive,
        services=_command_services(),
    )


@app.command("gateway")
def serve_gateway(
    model: Annotated[str | None, typer.Option("--model", "-m", envvar="CODING_AGENT_MODEL")] = None,
    provider: Annotated[
        str | None,
        typer.Option("--provider", envvar=["LOOPLANE_PROVIDER", "PCA_PROVIDER"]),
    ] = None,
    api_url: Annotated[
        str | None,
        typer.Option("--api-url", envvar=["LOOPLANE_API_URL", "PCA_API_URL"]),
    ] = None,
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", min=1, max=65535)] = 8787,
    bearer_token: Annotated[
        str | None,
        typer.Option("--bearer-token", envvar=["LOOPLANE_GATEWAY_TOKEN", "PCA_GATEWAY_TOKEN"]),
    ] = None,
    experimental_subscription: Annotated[bool, typer.Option("--experimental-subscription")] = False,
    allow_custom_provider_endpoint: Annotated[
        bool, typer.Option("--allow-custom-provider-endpoint")
    ] = False,
) -> None:
    "Expose one configured provider through a bounded OpenAI Chat gateway."

    from looplane.commands import serve

    return serve.serve_gateway(
        model=model,
        provider=provider,
        api_url=api_url,
        host=host,
        port=port,
        bearer_token=bearer_token,
        experimental_subscription=experimental_subscription,
        allow_custom_provider_endpoint=allow_custom_provider_endpoint,
        services=_command_services(),
    )


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
    "Expose a native conversation runtime through the WebSocket attach protocol."

    from looplane.commands import serve

    return serve.serve_conversation_server(
        repository=repository, runtime=runtime, model=model, host=host, port=port, path=path
    )


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
            envvar=["LOOPLANE_PROVIDER", "PCA_PROVIDER"],
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
        typer.Option("--api-url", "--base-url", envvar=["LOOPLANE_API_URL", "PCA_API_URL"]),
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
    dialect: Annotated[
        str,
        typer.Option(
            "--dialect",
            help=(
                "Tool calling mode. 'auto' uses in-band XML for models that lack "
                "native tool calling; 'xml' forces in-band; 'native' forces native."
            ),
        ),
    ] = "auto",
) -> None:
    "Run one non-interactive task (Codex-style `exec`; `run` remains an alias)."

    from looplane.commands import chat

    return chat.run(
        prompt=prompt,
        repository=repository,
        instruction=instruction,
        model=model,
        check=check,
        provider=provider,
        allowed_path=allowed_path,
        base_sha=base_sha,
        base_url=base_url,
        run_root=run_root,
        max_steps=max_steps,
        wall_time_seconds=wall_time_seconds,
        tool_calling=tool_calling,
        allow_custom_provider_endpoint=allow_custom_provider_endpoint,
        experimental_subscription=experimental_subscription,
        unsafe_local_exec=unsafe_local_exec,
        sandbox_checks=sandbox_checks,
        dialect=dialect,
        services=_command_services(),
    )


@app.command("export-otel")
def export_otel(
    run_id: Annotated[str, typer.Argument(help="Run id (or 'last')")],
    run_root: Annotated[Path, typer.Option("--run-root")] = DEFAULT_RUN_ROOT,
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Write OTLP-JSON to a file")
    ] = None,
) -> None:
    "Export a run as OpenTelemetry GenAI OTLP-JSON."

    from looplane.commands import sessions

    return sessions.export_otel(run_id=run_id, run_root=run_root, output=output)


def _acquire_native_controller(
    cache: NativeControllerCache,
    identity: tuple[str, Path, str | None, str | None],
    *,
    adapter: runtime_registry.RuntimeAdapter,
    repository: Path,
    model: str | None,
    backend_limiter: BackendTurnLimiter | None = None,
) -> ConversationController:
    from looplane.commands import bootstrap

    return bootstrap._acquire_native_controller(
        cache=cache,
        identity=identity,
        adapter=adapter,
        repository=repository,
        model=model,
        backend_limiter=backend_limiter,
        services=_command_services(),
    )


def _validate_tui_terminal_size() -> None:
    from looplane.commands import terminal_io

    return terminal_io._validate_tui_terminal_size(services=_command_services())


def _resolve_cli_settings(
    *,
    provider: str | None,
    model: str | None,
    api_url: str | None,
    allow_model_role_alias: bool = False,
) -> tuple[str, str | None, str | None]:
    from looplane.commands import settings

    return settings._resolve_cli_settings(
        provider=provider,
        model=model,
        api_url=api_url,
        allow_model_role_alias=allow_model_role_alias,
        services=_command_services(),
    )


def _enter_dangerous_mode(dangerous: bool):
    from looplane.commands import policy

    return policy._enter_dangerous_mode(dangerous=dangerous, services=_command_services())


def _confirm_direct_edit_with_dangerous_mode(*, edit_real_repo: bool, dangerous: bool) -> None:
    from looplane.commands import policy

    return policy._confirm_direct_edit_with_dangerous_mode(
        edit_real_repo=edit_real_repo, dangerous=dangerous, services=_command_services()
    )


def _permission_guard_from_config(
    *,
    config: CliConfig,
    repository: Path,
    deny_tool: list[str] | None = None,
    dangerous: bool = False,
):
    from looplane.commands import policy

    return policy._permission_guard_from_config(
        config=config,
        repository=repository,
        deny_tool=deny_tool,
        dangerous=dangerous,
        services=_command_services(),
    )


async def _resume_and_close(
    run_dir: Path,
    model: ModelProvider,
    *,
    approval_policy: TTYApprovalPolicy,
    event_sink: ConsoleEventSink,
):
    from looplane.commands import bootstrap

    return await bootstrap._resume_and_close(
        run_dir=run_dir,
        model=model,
        approval_policy=approval_policy,
        event_sink=event_sink,
        services=_command_services(),
    )


_COMPAT_EXPORTS = {
    "load_cli_config": "looplane.cli_config",
    "save_cli_config": "looplane.cli_config",
    "default_cli_config_path": "looplane.cli_config",
    "SUPPORTED_PROVIDERS": "looplane.cli_config",
    "Limits": "looplane.contracts",
    "RunResult": "looplane.contracts",
    "TaskContract": "looplane.contracts",
    "VerificationCommand": "looplane.contracts",
    "_default_run_root": "looplane.commands.paths",
    "_commands": "looplane.commands.common",
    "_show_result": "looplane.commands.common",
    "_prompt_or_task": "looplane.commands.common",
    "_validate_tui_terminal_size": "looplane.commands.terminal_io",
    "MIN_TUI_TERMINAL_HEIGHT": "looplane.commands.terminal_io",
    "_show_context_header": "looplane.commands.terminal_io",
    "_parse_provider_model_spec": "looplane.commands.settings",
    "_resolve_model_role_alias": "looplane.commands.settings",
    "_first_model_role_candidate": "looplane.commands.settings",
    "_resolve_cli_settings": "looplane.commands.settings",
    "_choose_provider": "looplane.commands.onboarding",
    "_choose_model": "looplane.commands.onboarding",
    "OLLAMA_TAGS_URL": "looplane.commands.onboarding",
    "MAX_OLLAMA_TAGS_BYTES": "looplane.commands.onboarding",
    "ONBOARDING_PROVIDERS": "looplane.commands.onboarding",
    "_dangerous_acceptance_path": "looplane.commands.policy",
    "_enter_dangerous_mode": "looplane.commands.policy",
    "_direct_edit_dangerous_acceptance_path": "looplane.commands.policy",
    "_confirm_direct_edit_with_dangerous_mode": "looplane.commands.policy",
    "_permission_guard_from_config": "looplane.commands.policy",
    "_effective_sandbox_checks": "looplane.commands.policy",
    "_dispose_controller": "looplane.commands.bootstrap",
    "_schedule_controller_cleanup": "looplane.commands.bootstrap",
    "_acquire_native_controller": "looplane.commands.bootstrap",
    "_required_env": "looplane.commands.bootstrap",
    "_required_native_field": "looplane.commands.bootstrap",
    "_loopback_url": "looplane.commands.bootstrap",
    "_credential_hint": "looplane.commands.bootstrap",
    "ModelBundleResource": "looplane.commands.bootstrap",
    "_run_and_close": "looplane.commands.bootstrap",
    "_resume_and_close": "looplane.commands.bootstrap",
    "_SIMPLE_API_KEY_PROVIDERS": "looplane.commands.bootstrap",
    "_SIMPLE_API_KEY_BASE_URLS": "looplane.commands.bootstrap",
    "_exchange_codex_code": "looplane.commands.auth",
    "_run_external_coding": "looplane.commands.external",
    "_resolve_resume_dir": "looplane.commands.sessions",
    "runtime_registry": "looplane.runtime_registry",
    "provider_catalog": "looplane.provider_catalog",
}


def __getattr__(name: str):
    """Read-only lazy exports; mutable test seams above have explicit callbacks."""
    module_name = _COMPAT_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(module_name)
    if name in {"runtime_registry", "provider_catalog"}:
        return module
    return getattr(module, name)


if __name__ == "__main__":
    app()

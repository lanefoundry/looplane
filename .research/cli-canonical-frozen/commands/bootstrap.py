"""Lazy model and runtime construction with owned per-command caches."""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import typer

import looplane.provider_catalog as provider_catalog
import looplane.runtime_registry as runtime_registry
from looplane.cli_config import (
    CliConfig,
)
from looplane.commands.ports import CommandServices
from looplane.contracts import Limits, TaskContract

if TYPE_CHECKING:
    from looplane.approvals import ApprovalPolicy, TTYApprovalPolicy
    from looplane.commands.ports import AsyncResource, RunHandle
    from looplane.console import ConsoleEventSink
    from looplane.conversation_controller import (
        BackendTurnLimiter,
        ConversationController,
        ConversationEventSink,
    )
    from looplane.events import EventSink
    from looplane.external_agents import ExternalEventSink
    from looplane.models import ModelProvider
    from looplane.permissions import PermissionGuard
    from looplane.terminal.types import TuiRunRequest

    NativeControllerCache = dict[tuple[str, Path, str | None, str | None], ConversationController]

from looplane.commands import common as _common
from looplane.commands import policy as _policy
from looplane.commands import settings as _settings

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
    services: CommandServices,
) -> ConversationController:
    """Return the cached controller for ``identity`` or build a fresh one.

    A controller that closed itself after a failed turn (see
    :meth:`ConversationTurnHandle.run`) is discarded and replaced so a single
    protocol failure does not poison every later run in the session.

    The native in-process session class comes from the runtime registry's
    ``native_session`` import path, so adding a new native-driven runtime is a
    registry entry rather than a branch here.
    """

    from looplane.conversation_controller import ConversationController
    from looplane.hooks import load_project_hook_runner

    assert adapter.native_session is not None
    controller = cache.get(identity)
    if controller is not None and controller.is_closed:
        _schedule_controller_cleanup(controller)
        cache.pop(identity, None)
        controller = None
    if controller is None:
        with services.startup.span("controller.build"):
            session_cls = runtime_registry._resolve_class(adapter.native_session)
            session = session_cls(repository, model=model)
            controller = ConversationController(
                session,
                backend_limiter=backend_limiter,
                hook_runner=load_project_hook_runner(repository),
            )
            cache[identity] = controller
    return controller


def _credential_hint(provider: str, *, api_url: str | None = None) -> str | None:
    from looplane.native_credentials import resolve_native_field

    if provider == "ollama":
        return None
    if provider == "openai-compatible":
        endpoint = api_url or os.environ.get("OPENAI_BASE_URL")
        if resolve_native_field("openai-compatible", "api_key") or (
            endpoint and _loopback_url(endpoint)
        ):
            return None
        return "Set OPENAI_API_KEY, or run `looplane auth set-key openai-compatible`."
    if provider == "anthropic":
        return (
            None
            if resolve_native_field("anthropic", "api_key")
            else "Set ANTHROPIC_API_KEY, or run `looplane auth set-key anthropic`."
        )
    if provider == "gemini":
        return (
            None
            if resolve_native_field("gemini", "api_key")
            else "Set GEMINI_API_KEY or GOOGLE_API_KEY, or run `looplane auth set-key gemini`."
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
                "or run `looplane auth set-key workers-ai`."
            )
        )
    env_var = _SIMPLE_API_KEY_PROVIDERS.get(provider)
    if env_var is not None:
        return (
            None
            if resolve_native_field(provider, "api_key")
            else f"Set {env_var}, or run `looplane auth set-key {provider}`."
        )
    return None


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise typer.BadParameter(f"required environment variable is missing: {name}")
    return value


def _required_native_field(provider: str, field: str, *, env_hint: str) -> str:
    from looplane.native_credentials import resolve_native_field

    value = resolve_native_field(provider, field)
    if not value:
        raise typer.BadParameter(
            f"missing {field.replace('_', ' ')} for {provider}: set {env_hint}, "
            f"or run `looplane auth set-key {provider}`"
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
    dialect_flag: str = "auto",
    services: CommandServices,
) -> ModelProvider:
    from looplane.codex_oauth import (
        CodexCredentialManager,
        CodexCredentialStore,
        CodexOAuthClient,
        OpenAICodexResponsesModel,
    )
    from looplane.dialect import resolve_dialect
    from looplane.models import (
        AnthropicModel,
        GeminiModel,
        OpenAICompatibleModel,
        ResponsesModel,
        WorkersAIModel,
    )
    from looplane.native_credentials import resolve_native_field

    force = dialect_flag if dialect_flag != "auto" else None

    if provider == "openai-compatible":
        dialect = resolve_dialect(
            model,
            supports_tool_calling=tool_calling or None,
            force_dialect=force,
        )
        return OpenAICompatibleModel(
            model=model,
            api_key=resolve_native_field("openai-compatible", "api_key"),
            base_url=base_url or os.environ.get("OPENAI_BASE_URL"),
            supports_tool_calling=tool_calling,
            dialect=dialect,
        )
    if provider == "ollama":
        ollama_url = base_url or os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434/v1")
        dialect = resolve_dialect(
            model,
            supports_tool_calling=tool_calling or None,
            force_dialect=force,
        )
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
            dialect=dialect,
        )
    if provider == "openai-codex":
        with services.startup.span("model.codex_oauth"):
            oauth = CodexOAuthClient()
            manager = CodexCredentialManager(
                CodexCredentialStore(services.credential_path()),
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
        dialect = resolve_dialect(
            model,
            supports_tool_calling=tool_calling or None,
            force_dialect=force,
        )
        return OpenAICompatibleModel(
            model=model,
            api_key=api_key,
            base_url=base_url,
            supports_tool_calling=tool_calling,
            provider_name=provider,
            dialect=dialect,
        )
    raise typer.BadParameter(f"unsupported provider: {provider}")


class ModelBundleResource:
    def __init__(self, *models: ModelProvider | None) -> None:
        self._models = tuple(model for model in models if model is not None)

    async def aclose(self) -> None:
        for model in self._models:
            await model.aclose()


async def _run_and_close(runner: RunHandle, *models: ModelProvider | None):
    try:
        return await runner.run()
    finally:
        await ModelBundleResource(*models).aclose()


async def _resume_and_close(
    run_dir: Path,
    model: ModelProvider,
    *,
    approval_policy: TTYApprovalPolicy,
    event_sink: ConsoleEventSink,
    services: CommandServices,
):
    AgentRunner, _ = services.runtime.native_runtime()
    from looplane.permissions import PermissionGuard

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


def _new_turn_limiter():
    from looplane.conversation_controller import BackendTurnLimiter

    return BackendTurnLimiter()


def build_native_runner(
    services: CommandServices,
    task: TaskContract,
    model: ModelProvider,
    run_root: Path,
    **kwargs,
) -> RunHandle:
    constructor, _ = services.runtime.native_runtime()
    return constructor(task, model, run_root, **kwargs)


def build_external_backend(runtime: str, **kwargs):
    adapter = runtime_registry.RUNTIME_REGISTRY[runtime]
    assert adapter.backend is not None
    constructor = runtime_registry._resolve_class(adapter.backend)
    return constructor(**kwargs)


def build_conversation_session(
    adapter: runtime_registry.RuntimeAdapter, *, repository: Path, model: str | None
):
    assert adapter.native_session is not None
    constructor = runtime_registry._resolve_class(adapter.native_session)
    return constructor(source_repository=repository, model=model)


@dataclass
class ModelSelection:
    """Own the lazy fallback/reviewer models for one command invocation."""

    services: CommandServices
    fallback_specs: tuple[str, ...]
    auto_review: bool
    allow_custom_provider_endpoint: bool
    experimental_subscription: bool
    fallback_cache: list[ModelProvider] = field(default_factory=list)
    review_model_cache: list[ModelProvider] = field(default_factory=list)

    def build_fallback_models(self) -> tuple[ModelProvider, ...]:
        """Construct --fallback-model candidates lazily (credentials resolve at call time)."""
        if not self.fallback_specs:
            return ()
        if not self.fallback_cache:
            for spec in self.fallback_specs:
                parsed_candidates = (
                    _settings._resolve_model_role_alias(spec) if spec.startswith("@") else None
                )
                if parsed_candidates is None:
                    parsed = _settings._parse_provider_model_spec(spec)
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
                    self.fallback_cache.append(
                        self.services.model_factory(
                            provider=fb_provider,
                            model=fb_model,
                            base_url=None,
                            tool_calling=True,
                            allow_custom_provider_endpoint=self.allow_custom_provider_endpoint,
                            experimental_subscription=self.experimental_subscription,
                        )
                    )
        return tuple(self.fallback_cache)

    def build_review_model(self, selected_provider: str | None) -> ModelProvider | None:
        """Construct the optional reviewer lane lazily."""
        if not self.auto_review:
            return None
        if self.review_model_cache:
            return self.review_model_cache[0]
        candidates = _settings._resolve_model_role_alias("@reviewer", provider=selected_provider)
        if not candidates:
            raise typer.BadParameter(
                f"--auto-review has no @reviewer candidate for provider {selected_provider!r}"
            )
        review_provider, review_model = candidates[0]
        self.review_model_cache.append(
            self.services.model_factory(
                provider=review_provider,
                model=review_model,
                base_url=None,
                tool_calling=False,
                allow_custom_provider_endpoint=self.allow_custom_provider_endpoint,
                experimental_subscription=self.experimental_subscription,
            )
        )
        return self.review_model_cache[0]


@dataclass
class ChatRuntimeFactory:
    """Own runtime construction and controller reuse for one interactive application."""

    services: CommandServices
    repository: Path
    check: list[str] | None
    run_root: Path
    unsafe_local_exec: bool
    edit_real_repo: bool
    permission_guard: PermissionGuard
    sandbox_checks: bool
    initial_config: CliConfig
    allow_custom_provider_endpoint: bool
    experimental_subscription: bool
    model_selection: ModelSelection
    guarded: Callable[[ApprovalPolicy | None], ApprovalPolicy | None]
    native_controllers: NativeControllerCache = field(default_factory=dict)
    native_backend_limiter: BackendTurnLimiter = field(default_factory=_new_turn_limiter)

    def make_runner(
        self,
        request: TuiRunRequest,
        approval_policy: ApprovalPolicy | None,
        event_sink: EventSink | ExternalEventSink | ConversationEventSink | None,
    ) -> tuple[RunHandle, AsyncResource | None]:
        from looplane.conversation_controller import decide_runtime_approval

        approval_policy = self.guarded(approval_policy)
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
                self.native_controllers,
                identity,
                adapter=adapter,
                repository=request.repository,
                model=request.model,
                backend_limiter=self.native_backend_limiter,
                services=self.services,
            )
            return (
                controller.turn(
                    request.instruction,
                    event_sink=event_sink,
                    approval_callback=lambda event: decide_runtime_approval(approval_policy, event),
                ),
                controller,
            )
        if request.mode == "ask":
            raise ValueError("read-only Ask mode is no longer a separate runtime")
        task = TaskContract(
            repository=request.repository,
            instruction=request.instruction,
            allowed_paths=("**",),
            verification=_common._commands(self.check),
            limits=Limits(
                wall_time_seconds=300.0
                if adapter.kind is runtime_registry.RuntimeKind.EXTERNAL
                else 900.0
            ),
        )
        if adapter.kind is runtime_registry.RuntimeKind.EXTERNAL and adapter.backend is not None:
            backend = build_external_backend(
                request.runtime, model=request.model, timeout_seconds=300.0
            )
            return (
                build_external_runner(
                    task,
                    backend,
                    self.run_root,
                    allow_external_modify=False,
                    allow_unsafe_local_exec=self.unsafe_local_exec,
                    approval_policy=approval_policy,
                    event_sink=event_sink,
                ),
                None,
            )
        if request.provider is None or request.model is None:
            raise ValueError("looplane requires a provider and model")
        if hint := _credential_hint(request.provider, api_url=request.api_url):
            raise ValueError(f"Provider is not ready. {hint}")
        selected_model = self.services.model_factory(
            provider=request.provider,
            model=request.model,
            base_url=request.api_url,
            tool_calling=True,
            allow_custom_provider_endpoint=self.allow_custom_provider_endpoint,
            experimental_subscription=self.experimental_subscription,
        )
        if request.continuation_run_dir is not None:
            run_root_for_call: Path = request.continuation_run_dir.parent
            run_id_for_call: str | None = request.continuation_run_dir.name
            continuation = True
        else:
            run_root_for_call = self.run_root
            run_id_for_call = None
            continuation = False
        return (
            build_native_runner(
                self.services,
                task,
                selected_model,
                run_root_for_call,
                run_id=run_id_for_call,
                continuation=continuation,
                allow_unsafe_local_exec=self.unsafe_local_exec,
                allow_direct_repo_edit=self.edit_real_repo,
                approval_policy=approval_policy,
                permission_guard=self.permission_guard,
                fallback_models=self.model_selection.build_fallback_models(),
                review_model=self.model_selection.build_review_model(request.provider),
                sandbox_checks=_policy._effective_sandbox_checks(self.sandbox_checks),
                sandbox_profile=self.initial_config.sandbox_profile,
                sandbox_backend=self.initial_config.sandbox_backend,
                sandbox_read_roots=tuple(
                    Path(root).expanduser() for root in self.initial_config.sandbox_read_roots
                ),
                event_sink=event_sink,
            ),
            ModelBundleResource(
                selected_model, self.model_selection.build_review_model(request.provider)
            ),
        )

    async def warmup(self, context_id: str | None) -> None:
        runtime = self.initial_config.runtime
        adapter = runtime_registry.RUNTIME_REGISTRY.get(runtime)
        if adapter is None or adapter.native_session is None:
            return
        try:
            model = self.initial_config.runtime_model or self.initial_config.model
            identity = (runtime, self.repository.resolve(), model, context_id)
            controller = _acquire_native_controller(
                self.native_controllers,
                identity,
                adapter=adapter,
                repository=self.repository,
                model=model,
                backend_limiter=self.native_backend_limiter,
                services=self.services,
            )
            await self.services.runtime.start_controller(controller)
        except BaseException:
            pass


def build_external_runner(task, backend, run_root, **kwargs) -> RunHandle:
    """Construct the bounded external runner after the command validates its flags."""
    from looplane.external_runner import ExternalCodingRunner

    return ExternalCodingRunner(task, backend, run_root, **kwargs)

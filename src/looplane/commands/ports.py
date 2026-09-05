"""Narrow callbacks supplied by the CLI root, without importing legacy facades."""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from looplane.cli_config import CliConfig
    from looplane.contracts import RunResult, TaskContract
    from looplane.conversation_controller import ConversationController
    from looplane.models import ModelProvider
    from looplane.startup_trace import _StartupTracer


class RunHandle(Protocol):
    async def run(self) -> RunResult: ...


class AsyncResource(Protocol):
    async def aclose(self) -> None: ...


class NativeRunnerFactory(Protocol):
    def __call__(
        self, task: TaskContract, model: ModelProvider, run_root: Path, **kwargs: Any
    ) -> RunHandle: ...

    async def resume(self, run_dir: Path, model: ModelProvider, **kwargs: Any) -> RunHandle: ...


class TerminalApplication(Protocol):
    final_transcript_text: str
    last_error: str | None

    def run(self, *, inline: bool = False) -> RunResult | None: ...


class TerminalFactory(Protocol):
    def __call__(self, **kwargs: Any) -> TerminalApplication: ...


class ModelFactory(Protocol):
    def __call__(
        self,
        *,
        provider: str,
        model: str,
        base_url: str | None,
        tool_calling: bool,
        allow_custom_provider_endpoint: bool,
        experimental_subscription: bool = False,
        dialect_flag: str = "auto",
    ) -> ModelProvider: ...


class SetupFactory(Protocol):
    def __call__(
        self, *, current: CliConfig | None = None, locked_provider: str | None = None
    ) -> CliConfig: ...


@dataclass(frozen=True)
class RuntimePorts:
    """Explicit lazy runtime adapters supplied by the CLI composition root."""

    native_runtime: Callable[[], tuple[NativeRunnerFactory, type[Exception]]]
    terminal_app: Callable[[], TerminalFactory]
    terminal_context_id: Callable[[TerminalApplication], str | None]
    start_controller: Callable[[ConversationController], Awaitable[None]]


@dataclass(frozen=True)
class CommandServices:
    """Per-invocation dependencies; legacy monkeypatches resolve at the root only."""

    startup: _StartupTracer
    model_factory: ModelFactory
    stdin_is_tty: Callable[[], bool]
    supports_tui: Callable[[], bool]
    terminal_size: Callable[[], os.terminal_size | None]
    interactive_setup: SetupFactory
    discover_models: Callable[[], tuple[str, ...]]
    credential_path: Callable[[], Path]
    fetch_models: Callable[[], tuple[str, ...]]
    runtime: RuntimePorts

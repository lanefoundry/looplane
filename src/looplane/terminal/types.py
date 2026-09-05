"""Terminal request, selection, and lifecycle contracts independent of the App."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from looplane.approvals import ApprovalPolicy
from looplane.cli_config import CliConfig
from looplane.console import EventSink
from looplane.contracts import RunResult

ProviderOption = tuple[str, str]


RuntimeOption = tuple[str, str]


RuntimeModelOption = tuple[str, str | None]


class InteractionState(StrEnum):
    """The UI surface that currently owns keyboard input."""

    APPROVAL = "approval"
    SELECTOR = "selector"
    COMMAND_MENU = "command-menu"
    RUNNING = "running"
    COMPOSER = "composer"
    TRANSCRIPT = "transcript"


class LoadingPhase(StrEnum):
    """Small provider-neutral subset of Claude Code's spinner phases."""

    REQUESTING = "requesting"
    RESPONDING = "responding"
    THINKING = "thinking"
    TOOL_USE = "tool-use"
    VERIFYING = "verifying"


class TuiRunner(Protocol):
    async def run(self) -> RunResult: ...

    def request_cancel(self) -> None: ...


class TuiResource(Protocol):
    async def aclose(self) -> None: ...


@dataclass(frozen=True)
class TuiRunRequest:
    repository: Path
    instruction: str
    runtime: str
    provider: str | None
    model: str | None
    api_url: str | None
    mode: str = "agent"
    context_id: str | None = None
    continuation_run_dir: Path | None = None


RunnerFactory = Callable[
    [TuiRunRequest, ApprovalPolicy, EventSink], tuple[TuiRunner, TuiResource | None]
]


@dataclass(frozen=True)
class TuiConfigurationSelection:
    config: CliConfig
    persist: bool


@dataclass(frozen=True)
class CommandMenuChoice:
    """One keyboard-selectable composer completion."""

    prompt: str
    replacement: str
    execute: bool


@dataclass(frozen=True)
class InlineSelectorOption:
    """One concise choice in a transcript-native command selector."""

    value: str
    label: str
    description: str
    selected: bool = False

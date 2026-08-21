"""Full-screen Textual frontend for the provider-neutral coding-agent harness."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Input, Label, RichLog, Select, Static

from coding_agent.approvals import (
    ApprovalDecision,
    ApprovalPolicy,
    ApprovalRequest,
    ToolEffect,
)
from coding_agent.cli_config import CliConfig, save_cli_config
from coding_agent.console import EventSink, LiveEventProjection
from coding_agent.contracts import RunResult
from coding_agent.events import RunEvent
from coding_agent.loop import AgentRunner
from coding_agent.models import ModelProvider

ProviderOption = tuple[str, str]


@dataclass(frozen=True)
class TuiRunRequest:
    repository: Path
    instruction: str
    provider: str
    model: str
    api_url: str | None


RunnerFactory = Callable[
    [TuiRunRequest, ApprovalPolicy, EventSink], tuple[AgentRunner, ModelProvider]
]


class RunEventMessage(Message):
    """Deliver one immutable harness event to the UI reducer."""

    def __init__(self, event: RunEvent, generation: int) -> None:
        super().__init__()
        self.event = event
        self.generation = generation


class TextualEventSink:
    def __init__(self, app: PcaApp, generation: int) -> None:
        self.app = app
        self.generation = generation

    async def emit(self, event: RunEvent) -> None:
        self.app.post_message(RunEventMessage(event, self.generation))


class TextualApprovalPolicy:
    def __init__(self, app: PcaApp) -> None:
        self.app = app

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        if request.effect == ToolEffect.READ:
            return ApprovalDecision.ALLOW_ONCE
        return await self.app.request_approval(request)


class ApprovalModal(ModalScreen[ApprovalDecision]):
    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]
    DEFAULT_CSS = """
    ApprovalModal { align: center middle; background: $background 70%; }
    ApprovalModal > Vertical {
        width: 78; max-width: 92%; height: auto; max-height: 85%;
        padding: 1 2; border: round $warning; background: $surface;
    }
    ApprovalModal .title { text-style: bold; color: $warning; margin-bottom: 1; }
    ApprovalModal .preview { max-height: 18; overflow-y: auto; margin-bottom: 1; }
    ApprovalModal Horizontal { height: auto; align-horizontal: right; }
    ApprovalModal Button { margin-left: 1; }
    """

    def __init__(self, request: ApprovalRequest) -> None:
        super().__init__()
        self.request = request

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(
                f"Approval required · {self.request.effect.value}", classes="title"
            )
            yield Static(
                self.request.preview or "No preview supplied.",
                classes="preview",
                markup=False,
            )
            with Horizontal():
                yield Button("Once", id="once", variant="primary")
                yield Button("Session", id="session")
                yield Button("Deny", id="deny", variant="warning")
                yield Button("Cancel run", id="cancel", variant="error")

    @on(Button.Pressed)
    def choose(self, event: Button.Pressed) -> None:
        decision = {
            "once": ApprovalDecision.ALLOW_ONCE,
            "session": ApprovalDecision.ALLOW_SESSION,
            "deny": ApprovalDecision.DENY,
            "cancel": ApprovalDecision.CANCEL,
        }[event.button.id or "cancel"]
        self.dismiss(decision)

    def action_cancel(self) -> None:
        self.dismiss(ApprovalDecision.CANCEL)


class OnboardingModal(ModalScreen[CliConfig | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]
    DEFAULT_CSS = """
    OnboardingModal { align: center middle; background: $background 70%; }
    OnboardingModal > Vertical {
        width: 72; max-width: 92%; height: auto; padding: 1 2;
        border: round $accent; background: $surface;
    }
    OnboardingModal .title { text-style: bold; color: $accent; }
    OnboardingModal .hint { color: $text-muted; margin-bottom: 1; }
    OnboardingModal Label.field { margin-top: 1; }
    OnboardingModal Horizontal { height: auto; margin-top: 1; align-horizontal: right; }
    OnboardingModal Button { margin-left: 1; }
    """

    def __init__(
        self,
        *,
        current: CliConfig,
        providers: Iterable[ProviderOption],
        ollama_models: tuple[str, ...],
        locked_provider: str | None = None,
    ) -> None:
        super().__init__()
        self.current = current
        self.providers = tuple(providers)
        self.ollama_models = ollama_models
        self.locked_provider = locked_provider

    def _initial_provider(self) -> str:
        slugs = [slug for slug, _ in self.providers]
        if self.locked_provider:
            return self.locked_provider
        if self.current.provider in slugs:
            return self.current.provider
        return "ollama" if self.ollama_models else "openai-compatible"

    def compose(self) -> ComposeResult:
        provider = self._initial_provider()
        provider_options = list(self.providers)
        if provider not in {slug for slug, _ in provider_options}:
            provider_options.append((provider, provider))
        model_options = tuple((name, name) for name in self.ollama_models)
        model_value: Any = (
            self.current.model
            if self.current.model in self.ollama_models
            else (self.ollama_models[0] if self.ollama_models else Select.NULL)
        )
        with Vertical():
            yield Label("Welcome to PCA", classes="title")
            yield Static(
                "Choose the model for this repository session. Credentials stay outside config.",
                classes="hint",
            )
            yield Label("Provider", classes="field")
            yield Select(
                tuple((label, slug) for slug, label in provider_options),
                value=provider,
                allow_blank=False,
                disabled=self.locked_provider is not None,
                id="provider",
            )
            yield Label("Model", classes="field")
            yield Select(
                model_options,
                value=model_value,
                allow_blank=not bool(model_options),
                id="ollama-model",
            )
            yield Input(
                value=self.current.model or "",
                placeholder="Provider model ID",
                id="model-id",
            )
            yield Static("", id="setup-error", markup=False)
            with Horizontal():
                yield Button("Cancel", id="cancel")
                yield Button("Continue", id="continue", variant="primary")

    def on_mount(self) -> None:
        self._sync_model_control(self._initial_provider())

    def _sync_model_control(self, provider: str) -> None:
        use_list = provider == "ollama" and bool(self.ollama_models)
        self.query_one("#ollama-model", Select).display = use_list
        self.query_one("#model-id", Input).display = not use_list

    @on(Select.Changed, "#provider")
    def provider_changed(self, event: Select.Changed) -> None:
        if event.value is not Select.NULL:
            self._sync_model_control(str(event.value))

    @on(Button.Pressed)
    def choose(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        provider_value = self.query_one("#provider", Select).value
        provider = str(provider_value)
        if provider == "ollama" and self.ollama_models:
            selected = self.query_one("#ollama-model", Select).value
            model = "" if selected is Select.NULL else str(selected).strip()
        else:
            model = self.query_one("#model-id", Input).value.strip()
        if not model or not model.isprintable() or "\x00" in model:
            self.query_one("#setup-error", Static).update("Enter a printable model ID.")
            return
        api_url = self.current.api_url if self.current.provider == provider else None
        self.dismiss(CliConfig(provider=provider, model=model, api_url=api_url))

    def action_cancel(self) -> None:
        self.dismiss(None)


class PcaApp(App[RunResult | None]):
    """One-run full-screen host; durable run state remains owned by AgentRunner."""

    TITLE = "PCA"
    SUB_TITLE = "Python coding agent"
    BINDINGS = [
        Binding("ctrl+c", "stop_or_quit", "Stop / quit", priority=True),
        Binding("ctrl+q", "stop_or_quit", "Stop / quit", priority=True, show=False),
        Binding("q", "quit_when_idle", "Quit"),
    ]
    CSS = """
    Screen { layout: vertical; }
    #context { height: 3; padding: 1 2; background: $boost; text-style: bold; }
    #activity { height: 1fr; margin: 1 2 0 2; border: round $panel; padding: 0 1; }
    #status { height: 3; padding: 1 2; color: $text-muted; }
    #composer { height: auto; padding: 0 2 1 2; }
    #task { width: 1fr; }
    #send, #stop { margin-left: 1; }
    #stop { display: none; }
    """

    def __init__(
        self,
        *,
        repository: Path,
        config: CliConfig,
        runner_factory: RunnerFactory,
        providers: Iterable[ProviderOption],
        ollama_models: tuple[str, ...] = (),
        initial_prompt: str | None = None,
        locked_provider: str | None = None,
    ) -> None:
        super().__init__()
        self.repository = repository
        self.config = config
        self.runner_factory = runner_factory
        self.providers = tuple(providers)
        self.ollama_models = ollama_models
        self.initial_prompt = initial_prompt
        self.locked_provider = locked_provider
        self._runner: AgentRunner | None = None
        self._model: ModelProvider | None = None
        self._result: RunResult | None = None
        self.last_error: str | None = None
        self._agent_running = False
        self._projection = LiveEventProjection()
        self._generation = 0

    def compose(self) -> ComposeResult:
        yield Static("", id="context", markup=False)
        yield RichLog(highlight=True, markup=False, wrap=True, id="activity")
        yield Static("Ready", id="status", markup=False)
        with Horizontal(id="composer"):
            yield Input(
                value=self.initial_prompt or "",
                placeholder="Describe a coding task in this repository…",
                id="task",
            )
            yield Button("Run", id="send", variant="primary")
            yield Button("Stop", id="stop", variant="error")
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_context()
        self.query_one("#activity", RichLog).write(
            "Ready. Describe one bounded coding task; changes stay in an isolated workspace."
        )
        if not self.config.provider or not self.config.model:
            self._run_onboarding()
        elif self.initial_prompt:
            self.call_after_refresh(self._submit_current_task)
        else:
            self.query_one("#task", Input).focus()

    def _refresh_context(self) -> None:
        identity = (
            f"{self.config.provider}/{self.config.model}"
            if self.config.provider and self.config.model
            else "setup required"
        )
        self.query_one("#context", Static).update(
            f"PCA  ·  {identity}  ·  {self.repository}"
        )

    @work
    async def _run_onboarding(self) -> None:
        configured = await self.push_screen_wait(
            OnboardingModal(
                current=self.config,
                providers=self.providers,
                ollama_models=self.ollama_models,
                locked_provider=self.locked_provider,
            )
        )
        if configured is None:
            self.exit(None)
            return
        try:
            await save_cli_config(configured)
        except (OSError, ValueError) as exc:
            self.query_one("#status", Static).update(f"Could not save config: {exc}")
            return
        self.config = configured
        self._refresh_context()
        self.query_one("#status", Static).update(
            f"Saved non-secret defaults · {configured.provider}/{configured.model}"
        )
        if self.initial_prompt:
            self._submit_current_task()
        else:
            self.query_one("#task", Input).focus()

    @on(Input.Submitted, "#task")
    def task_submitted(self, _event: Input.Submitted) -> None:
        self._submit_current_task()

    @on(Button.Pressed, "#send")
    def run_pressed(self, _event: Button.Pressed) -> None:
        self._submit_current_task()

    def _submit_current_task(self) -> None:
        if self._agent_running:
            return
        instruction = self.query_one("#task", Input).value.strip()
        if not instruction:
            self.query_one("#status", Static).update("Describe a coding task first.")
            return
        if not self.config.provider or not self.config.model:
            self._run_onboarding()
            return
        self._run_agent(instruction)

    @work(exclusive=True, group="agent-run")
    async def _run_agent(self, instruction: str) -> None:
        self._set_running(True)
        self._projection = LiveEventProjection()
        self._generation += 1
        generation = self._generation
        self._result = None
        self.last_error = None
        self.query_one("#activity", RichLog).clear()
        self.query_one("#status", Static).update("Starting isolated workspace…")
        request = TuiRunRequest(
            repository=self.repository,
            instruction=instruction,
            provider=self.config.provider or "",
            model=self.config.model or "",
            api_url=self.config.api_url,
        )
        try:
            runner, model = self.runner_factory(
                request,
                TextualApprovalPolicy(self),
                TextualEventSink(self, generation),
            )
            self._runner = runner
            self._model = model
            run_task = asyncio.create_task(runner.run())
            while True:
                try:
                    self._result = await asyncio.shield(run_task)
                    break
                except asyncio.CancelledError:
                    if run_task.done():
                        raise
                    runner.request_cancel()
            if self.query("#status"):
                self.query_one("#status", Static).update(
                    f"{self._result.status.value} · {self._result.terminal_reason} · "
                    f"{len(self._result.changed_files)} changed file(s)"
                )
                if self._result.summary:
                    self.query_one("#activity", RichLog).write(self._result.summary)
                if self._result.changed_files:
                    self.query_one("#activity", RichLog).write(
                        "Changed: " + ", ".join(self._result.changed_files)
                    )
                for outcome in self._result.verification:
                    marker = "passed" if outcome.ok else "failed"
                    self.query_one("#activity", RichLog).write(
                        f"Check {outcome.name}: {marker} (exit {outcome.exit_code})"
                    )
                self.query_one("#activity", RichLog).write(
                    f"Session: {self._result.run_id}"
                )
                if patch_path := self._result.artifacts.get("patch"):
                    self.query_one("#activity", RichLog).write(f"Patch: {patch_path}")
        except Exception as exc:
            self.last_error = f"Could not start run: {exc}"
            if self.query("#status"):
                self.query_one("#status", Static).update(self.last_error)
        finally:
            if self._model is not None:
                try:
                    await self._model.aclose()
                except Exception as exc:
                    self.last_error = f"Provider cleanup failed: {exc}"
                    if self.query("#status"):
                        self.query_one("#status", Static).update(self.last_error)
            self._runner = None
            self._model = None
            self._set_running(False)

    def _set_running(self, running: bool) -> None:
        self._agent_running = running
        if self.query("#task"):
            self.query_one("#task", Input).disabled = running
            self.query_one("#send", Button).display = not running
            self.query_one("#stop", Button).display = running

    @on(RunEventMessage)
    def event_received(self, message: RunEventMessage) -> None:
        if message.generation != self._generation or not self.query("#activity"):
            return
        for line in self._projection.apply(message.event):
            self.query_one("#activity", RichLog).write(line)
        if message.event.event_type == "model.requested":
            self.query_one("#status", Static).update("Thinking…")
        elif message.event.event_type in {"tool.requested", "tool.started"}:
            self.query_one("#status", Static).update(
                f"Using {message.event.data.get('name', 'tool')}…"
            )
        elif message.event.event_type == "verification.started":
            self.query_one("#status", Static).update("Verifying…")

    async def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        return await self.push_screen_wait(ApprovalModal(request))

    @on(Button.Pressed, "#stop")
    def stop_pressed(self, _event: Button.Pressed) -> None:
        self.action_stop_or_quit()

    def action_stop_or_quit(self) -> None:
        if isinstance(self.screen, ApprovalModal):
            self.screen.dismiss(ApprovalDecision.CANCEL)
        if self._runner is not None:
            self._runner.request_cancel()
            self.query_one("#status", Static).update(
                "Stopping safely after the current action…"
            )
        elif not self._agent_running:
            self.exit(self._result)

    def action_quit_when_idle(self) -> None:
        if not self._agent_running:
            self.exit(self._result)

    async def action_quit(self) -> None:
        """Override Textual's inherited priority Ctrl+Q hard quit."""

        self.action_stop_or_quit()

from __future__ import annotations

import asyncio
import stat
import sys
from pathlib import Path
from time import monotonic
from uuid import uuid4

import pytest
from rich.cells import cell_len
from textual.widgets import Button, OptionList, Select, Static

from rivumi.approvals import (
    ApprovalDecision,
    ApprovalReason,
    ApprovalRequest,
    HeadlessApprovalPolicy,
    ToolEffect,
)
from rivumi.backends import ExternalAgentEvent
from rivumi.cli_config import CliConfig, load_cli_config
from rivumi.contracts import (
    ModelCapabilities,
    ModelProtocol,
    ModelTurn,
    RunResult,
    RunStatus,
    TaskContract,
    ToolCall,
    VerificationCommand,
)
from rivumi.conversation import (
    ConversationEvent,
    ConversationEventKind,
    ConversationStore,
)
from rivumi.conversation_runtime import (
    ApprovalRequestedEvent,
    ApprovalResolvedEvent,
    ContextUsageUpdatedEvent,
    RuntimeApprovalKind,
    RuntimeApprovalRequest,
    RuntimeModelUpdatedEvent,
    RuntimeToolKind,
    RuntimeToolStatus,
    RuntimeTurnStatus,
    TextDeltaEvent,
    ToolCompletedEvent,
    ToolOutputDeltaEvent,
    ToolStartedEvent,
    TurnCompletedEvent,
    TurnStartedEvent,
)
from rivumi.events import RunEvent
from rivumi.loop import AgentRunner
from rivumi.models import ScriptedModel
from rivumi.runtime_semantics import ContextTelemetry, PermissionMode, ProcessLocalGrant
from rivumi.tui import (
    ApprovalModal,
    ConversationRuntimeEventMessage,
    InlineApprovalBlock,
    InlineSelectorBlock,
    LoadingPhase,
    MessageBlock,
    MessageComposer,
    OnboardingModal,
    RivumiApp,
    RunEventMessage,
    RuntimeLoadingIndicator,
    RuntimeStatus,
    TextualApprovalPolicy,
    TimelineEntry,
    ToolActionBlock,
    ToolGroupBlock,
)


class FakeModel:
    async def aclose(self) -> None:
        return None


class FailingCloseModel:
    async def aclose(self) -> None:
        raise RuntimeError("close exploded")


class LoopBoundPersistentResource:
    persistent = True

    def __init__(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.closed = False

    async def aclose(self) -> None:
        assert asyncio.get_running_loop() is self.loop
        self.closed = True


class FakeRunner:
    def __init__(self, *, approval_policy=None, event_sink=None) -> None:
        self.approval_policy = approval_policy
        self.event_sink = event_sink
        self.cancelled = False

    def request_cancel(self) -> None:
        self.cancelled = True

    async def run(self) -> RunResult:
        if self.event_sink is not None:
            await self.event_sink.emit(
                RunEvent(
                    event_type="run.created",
                    run_id="tui-run",
                    task_id="tui-task",
                    sequence=0,
                    data={},
                )
            )
        return RunResult(
            run_id="tui-run",
            task_id="tui-task",
            status=RunStatus.COMPLETED,
            summary="Finished in the full-screen UI.",
            terminal_reason="verified",
        )


async def _wait_until(predicate, *, attempts: int = 50) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition did not become true")


async def test_configured_tui_runs_task_and_projects_raw_events(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def factory(request, approval_policy, event_sink):
        captured["request"] = request
        runner = FakeRunner(approval_policy=approval_policy, event_sink=event_sink)
        return runner, FakeModel()

    app = RivumiApp(
        repository=tmp_path,
        config=CliConfig(provider="ollama", model="qwen3:4b"),
        runner_factory=factory,
        providers=(("ollama", "Ollama local"),),
    )

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.click("#task")
        await pilot.press(*"Fix tests")
        await pilot.press("enter")
        await _wait_until(lambda: app._result is not None)
        assert app._result is not None
        assert app._result.status == RunStatus.COMPLETED
        assert captured["request"].instruction == "Fix tests"
        assert app.query_one("#status").content == "completed · verified · 0 changed file(s)"


async def test_persistent_resource_closes_on_textual_event_loop(tmp_path: Path) -> None:
    resources: list[LoopBoundPersistentResource] = []

    def factory(_request, _approval_policy, _event_sink):
        resource = LoopBoundPersistentResource()
        resources.append(resource)
        return FakeRunner(), resource

    app = RivumiApp(
        repository=tmp_path,
        config=CliConfig(runtime="codex-cli"),
        runner_factory=factory,
        runtimes=(("codex-cli", "Codex CLI"),),
        providers=(("ollama", "Ollama"),),
    )
    async with app.run_test(size=(100, 30)) as pilot:
        task = app.query_one("#task", MessageComposer)
        task.load_text("hello")
        await pilot.press("enter")
        await _wait_until(lambda: bool(resources) and not app._agent_running)

    assert resources[0].closed


async def test_onboarding_modal_saves_private_non_secret_config(
    tmp_path: Path, monkeypatch
) -> None:
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("RIVUMI_CONFIG", str(config_path))
    app = RivumiApp(
        repository=tmp_path,
        config=CliConfig(),
        runner_factory=lambda *_: (FakeRunner(), FakeModel()),
        providers=(
            ("ollama", "Ollama local"),
            ("openai-compatible", "OpenAI-compatible API"),
        ),
        ollama_models=("qwen3:4b", "qwen3:0.6b"),
    )

    async with app.run_test(size=(100, 30)) as pilot:
        await _wait_until(lambda: isinstance(app.screen, OnboardingModal))
        await pilot.click("#save")
        await _wait_until(config_path.exists)
        assert load_cli_config(config_path) == CliConfig(
            runtime="rivumi-agent", provider="ollama", model="qwen3:4b"
        )
        assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
        assert "api_key" not in config_path.read_text()


async def test_onboarding_without_local_models_defers_model_until_main_screen(
    tmp_path: Path,
) -> None:
    app = RivumiApp(
        repository=tmp_path,
        config=CliConfig(),
        runner_factory=lambda *_: (FakeRunner(), FakeModel()),
        providers=(
            ("ollama", "Ollama local"),
            ("openai-compatible", "OpenAI-compatible API"),
        ),
        ollama_models=(),
    )

    async with app.run_test(size=(100, 30)) as pilot:
        await _wait_until(lambda: isinstance(app.screen, OnboardingModal))
        assert app.screen.query_one("#model-id").display is False
        assert app.screen.query_one("#automatic-model").display is True
        await pilot.click("#use-once")
        await _wait_until(lambda: not isinstance(app.screen, OnboardingModal))
        assert app.query_one("#send", Button).disabled is True
        assert "model required" in str(app.query_one("#context").content).lower()


@pytest.mark.parametrize("runtime", ["claude-code", "codex-cli"])
async def test_onboarding_external_runtime_uses_automatic_model(
    tmp_path: Path, runtime: str
) -> None:
    app = RivumiApp(
        repository=tmp_path,
        config=CliConfig(),
        runner_factory=lambda *_: (FakeRunner(), None),
        runtimes=((runtime, runtime), ("rivumi-agent", "Rivumi")),
        providers=(("anthropic", "Anthropic API (API key billing)"),),
    )

    async with app.run_test(size=(100, 30)) as pilot:
        await _wait_until(lambda: isinstance(app.screen, OnboardingModal))
        assert app.screen.query_one("#automatic-model").display is True
        assert app.screen.query_one("#model-id").display is False
        await pilot.click("#use-once")
        await _wait_until(lambda: not isinstance(app.screen, OnboardingModal))
        assert app.config.runtime == runtime
        assert app.config.model is None
        assert app.query_one("#send", Button).disabled is False
        assert "Automatic" in str(app.query_one("#context").content)


async def test_external_runtime_model_can_be_changed_after_automatic_default(
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    def factory(request, _approval_policy, _event_sink):
        captured["request"] = request
        return FakeRunner(), None

    app = RivumiApp(
        repository=tmp_path,
        config=CliConfig(runtime="claude-code"),
        runner_factory=factory,
        runtimes=(("claude-code", "Claude Code"),),
        runtime_models={
            "claude-code": (
                ("Automatic (recommended)", None),
                ("Sonnet", "sonnet"),
            )
        },
        providers=(("anthropic", "Anthropic API"),),
    )

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.press("ctrl+l")
        await _wait_until(lambda: bool(app.query(InlineSelectorBlock)))
        selector = app.query_one(InlineSelectorBlock)
        assert selector.kind == "model"
        assert selector.options[0].selected is True
        await pilot.press("down", "enter")
        await _wait_until(lambda: not app.query(InlineSelectorBlock))
        assert app.config.runtime_model == "sonnet"
        assert "sonnet" in str(app.query_one("#context").content)
        app.query_one("#task", MessageComposer).load_text("Fix one bug")
        app._submit_current_task()
        await _wait_until(lambda: app._result is not None)
        assert captured["request"].model == "sonnet"


async def test_api_runtime_can_enter_main_screen_then_choose_model(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def factory(request, _approval_policy, _event_sink):
        captured["request"] = request
        return FakeRunner(), FakeModel()

    app = RivumiApp(
        repository=tmp_path,
        config=CliConfig(runtime="rivumi-agent", provider="anthropic"),
        runner_factory=factory,
        providers=(("anthropic", "Anthropic API (API key billing)"),),
    )

    async with app.run_test(size=(100, 30)) as pilot:
        assert app.query_one("#send", Button).disabled is True
        assert "model required" in str(app.query_one("#context").content).lower()
        await pilot.press("ctrl+l")
        await _wait_until(lambda: isinstance(app.screen, OnboardingModal))
        model_input = app.screen.query_one("#model-id")
        assert model_input.display is True
        model_input.value = "claude-sonnet-test"
        await pilot.click("#use-once")
        await _wait_until(lambda: not isinstance(app.screen, OnboardingModal))
        assert app.query_one("#send", Button).disabled is False
        task = app.query_one("#task")
        task.load_text("Fix one bug")
        app._submit_current_task()
        await _wait_until(lambda: app._result is not None)
        assert captured["request"].runtime == "rivumi-agent"
        assert captured["request"].provider == "anthropic"
        assert captured["request"].model == "claude-sonnet-test"


@pytest.mark.parametrize("runtime", ["claude-code", "codex-cli"])
async def test_external_runtime_submits_without_model_override(
    tmp_path: Path, runtime: str
) -> None:
    captured: dict[str, object] = {}

    def factory(request, _approval_policy, _event_sink):
        captured["request"] = request
        return FakeRunner(), None

    app = RivumiApp(
        repository=tmp_path,
        config=CliConfig(runtime=runtime),
        runner_factory=factory,
        runtimes=((runtime, runtime),),
        providers=(("anthropic", "Anthropic API"),),
        initial_prompt="Fix one bug",
    )

    async with app.run_test(size=(100, 30)):
        await _wait_until(lambda: app._result is not None)
        assert captured["request"].runtime == runtime
        assert captured["request"].provider is None
        assert captured["request"].model is None


async def test_native_conversation_sends_only_each_new_turn_to_the_live_session(
    tmp_path: Path,
) -> None:
    requests = []

    def factory(request, approval_policy, event_sink):
        requests.append(request)
        return FakeRunner(approval_policy=approval_policy, event_sink=event_sink), None

    app = RivumiApp(
        repository=tmp_path,
        config=CliConfig(runtime="codex-cli"),
        runner_factory=factory,
        runtimes=(("codex-cli", "Codex CLI"),),
        providers=(("ollama", "Ollama"),),
    )

    async with app.run_test(size=(100, 30)):
        assert app.query_one("#mode", Select).value == "ask"
        task = app.query_one("#task", MessageComposer)
        task.load_text("hi")
        app._submit_current_task()
        await _wait_until(lambda: len(requests) == 1 and not app._agent_running)
        assert requests[0].instruction == "hi"

        task.load_text("what did I say?")
        app._submit_current_task()
        await _wait_until(lambda: len(requests) == 2 and not app._agent_running)
        assert requests[1].instruction == "what did I say?"
        assert requests[0].context_id == requests[1].context_id


async def test_external_ask_projects_each_turn_once_without_raw_protocol(
    tmp_path: Path,
) -> None:
    class ExternalRunner(FakeRunner):
        async def run(self) -> RunResult:
            for sequence, event_type, text in (
                (0, "system", None),
                (1, "message", "One visible answer."),
                (2, "result", None),
            ):
                await self.event_sink.emit(
                    ExternalAgentEvent(
                        sequence=sequence,
                        event_type=event_type,
                        text=text,
                    )
                )
            return RunResult(
                run_id="ask",
                task_id="ask",
                status=RunStatus.COMPLETED,
                summary="One visible answer.",
                terminal_reason="completed",
            )

    def factory(_request, approval_policy, event_sink):
        return ExternalRunner(approval_policy=approval_policy, event_sink=event_sink), None

    app = RivumiApp(
        repository=tmp_path,
        config=CliConfig(runtime="codex-cli"),
        runner_factory=factory,
        runtimes=(("codex-cli", "Codex CLI"),),
        providers=(("ollama", "Ollama"),),
    )

    async with app.run_test(size=(120, 36)):
        task = app.query_one("#task", MessageComposer)
        for question in ("first", "second"):
            task.load_text(question)
            app._submit_current_task()
            expected = 2 if question == "first" else 4
            await _wait_until(lambda expected=expected: len(app.query(MessageBlock)) == expected)
            await _wait_until(lambda: not app._agent_running)

        blocks = list(app.query(MessageBlock))
        assert [block.role for block in blocks] == [
            "You",
            "Assistant",
            "You",
            "Assistant",
        ]
        assert [block.content for block in blocks].count("One visible answer.") == 2
        assert "External" not in "\n".join(block.content for block in blocks)
        assert app.query_one("#activity").display is False


async def test_conversation_layout_uses_full_timeline_and_reflows_without_draft_loss(
    tmp_path: Path,
) -> None:
    def assert_bottom_stack() -> None:
        screen = app.screen.region
        workspace = app.query_one("#workspace").region
        topbar = app.query_one("#topbar").region
        transcript = app.query_one("#transcript").region
        secondary = app.query_one("#secondary").region
        status_row = app.query_one("#status-row").region
        composer = app.query_one("#composer").region
        assert workspace.bottom == screen.bottom
        assert topbar.bottom == transcript.y
        assert transcript.bottom == secondary.y
        assert status_row.y == secondary.y
        assert status_row.bottom == secondary.bottom
        assert secondary.bottom == composer.y
        assert composer.bottom == workspace.bottom
        assert transcript.height == (
            workspace.height - topbar.height - secondary.height - composer.height
        )
        assert transcript.height >= 4

    app = RivumiApp(
        repository=tmp_path,
        config=CliConfig(runtime="codex-cli"),
        runner_factory=lambda *_args: (FakeRunner(), None),
        runtimes=(("codex-cli", "Codex CLI"),),
        providers=(("ollama", "Ollama"),),
    )

    async with app.run_test(size=(160, 36)) as pilot:
        task = app.query_one("#task", MessageComposer)
        task.load_text("draft survives resize")
        task.focus()
        await pilot.pause()
        assert task.placeholder == ""
        cursor_style = task.get_component_rich_style("text-area--cursor")
        assert cursor_style.bgcolor is not None
        assert cursor_style.bgcolor == task.rich_style.bgcolor
        assert cursor_style.underline is True
        assert app.query_one("#workspace").region.width == 160
        assert app.query_one("#messages").region.width >= 150
        assert app.query_one("#activity").display is False
        assert not app.query("#stop")
        assert not app.query("#activity-toggle")
        assert_bottom_stack()

        await pilot.resize_terminal(60, 22)
        await pilot.pause()
        assert app.has_class("narrow")
        assert task.text == "draft survives resize"
        assert app.focused is task
        assert_bottom_stack()


async def test_sparse_conversation_is_anchored_above_composer(tmp_path: Path) -> None:
    app = RivumiApp(
        repository=tmp_path,
        config=CliConfig(runtime="codex-cli"),
        runner_factory=lambda *_args: (FakeRunner(), None),
        runtimes=(("codex-cli", "Codex CLI"),),
        providers=(("ollama", "Ollama"),),
    )

    async with app.run_test(size=(120, 36)) as pilot:
        app._write_turn("You", "Why does the transcript start at the top?")
        app._write_turn("Assistant", "Sparse conversations now stay near the composer.")
        await pilot.pause()

        transcript = app.query_one("#transcript").region
        messages = app.query_one("#messages").region
        assert messages.bottom >= transcript.bottom - 1
        assert messages.y > transcript.y

        for index in range(24):
            app._write_turn("Assistant", f"Additional conversation row {index}")
        await pilot.pause()

        transcript_widget = app.query_one("#transcript")
        assert transcript_widget.max_scroll_y > 0
        assert transcript_widget.scroll_y == transcript_widget.max_scroll_y

        transcript_widget.scroll_up(animate=False)
        await pilot.pause()
        reading_position = transcript_widget.scroll_y
        previous_maximum = transcript_widget.max_scroll_y
        assert reading_position < previous_maximum

        app._write_turn("Assistant", "A new row must not steal the reader's scroll position.")
        await pilot.pause()
        assert transcript_widget.max_scroll_y > previous_maximum
        assert transcript_widget.scroll_y == reading_position


def test_external_ask_prompt_retains_read_only_prefix_when_history_is_bounded(
    tmp_path: Path,
) -> None:
    app = RivumiApp(
        repository=tmp_path,
        config=CliConfig(runtime="codex-cli"),
        runner_factory=lambda *_args: (FakeRunner(), None),
        runtimes=(("codex-cli", "Codex CLI"),),
        providers=(("ollama", "Ollama"),),
    )
    app._ask_history = [("assistant", "x" * 8_000)] * 12

    prompt = app._ask_prompt("y" * 8_000)

    assert prompt.startswith(
        "You are in read-only Ask mode. Answer without editing files.\nConversation:\n"
    )
    assert len(prompt) <= 48_000
    assert prompt.endswith("y" * 8_000)


async def test_pca_owned_conversation_resumes_after_tui_restart(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path / "conversations", durable=False)
    requests = []

    def factory(request, approval_policy, event_sink):
        requests.append(request)
        return FakeRunner(approval_policy=approval_policy, event_sink=event_sink), None

    first = RivumiApp(
        repository=tmp_path,
        config=CliConfig(runtime="codex-cli"),
        runner_factory=factory,
        runtimes=(("codex-cli", "Codex CLI"),),
        providers=(("ollama", "Ollama"),),
        conversation_store=store,
    )
    async with first.run_test(size=(100, 30)):
        task = first.query_one("#task", MessageComposer)
        task.load_text("remember this")
        first._submit_current_task()
        await _wait_until(lambda: first._result is not None and not first._agent_running)
        conversation_id = first._conversation_id
        assert conversation_id is not None
        persisted = (store.root / conversation_id / "events.jsonl").read_text()
        assert "user.message" in persisted
        assert "turn.completed" in persisted

    second = RivumiApp(
        repository=tmp_path,
        config=CliConfig(runtime="codex-cli"),
        runner_factory=factory,
        runtimes=(("codex-cli", "Codex CLI"),),
        providers=(("ollama", "Ollama"),),
        conversation_store=store,
    )
    async with second.run_test(size=(100, 30)):
        task = second.query_one("#task", MessageComposer)
        task.load_text(f"/resume {conversation_id}")
        second._submit_current_task()
        await _wait_until(lambda: second._conversation_id == conversation_id)
        await _wait_until(lambda: len(second.query(MessageBlock)) == 2)
        blocks = list(second.query(MessageBlock))
        assert [(block.role, block.content) for block in blocks] == [
            ("You", "remember this"),
            ("Assistant", "Finished in the full-screen UI."),
        ]

        task.load_text("what did I ask?")
        second._submit_current_task()
        await _wait_until(lambda: len(requests) == 2 and not second._agent_running)
        assert "User: remember this" in requests[-1].instruction
        assert "Assistant: Finished in the full-screen UI." in requests[-1].instruction


async def test_native_model_switch_retains_conversation_and_replays_completed_turns(
    tmp_path: Path,
) -> None:
    store = ConversationStore(tmp_path / "conversations", durable=False)
    requests = []
    resources: list[LoopBoundPersistentResource] = []

    def factory(request, approval_policy, event_sink):
        requests.append(request)
        resource = LoopBoundPersistentResource()
        resources.append(resource)
        return FakeRunner(approval_policy=approval_policy, event_sink=event_sink), resource

    app = RivumiApp(
        repository=tmp_path,
        config=CliConfig(runtime="codex-cli"),
        runner_factory=factory,
        runtimes=(("codex-cli", "Codex CLI"),),
        runtime_models={
            "codex-cli": (
                ("Automatic", None),
                ("GPT 5.4", "gpt-5.4"),
            )
        },
        providers=(("ollama", "Ollama"),),
        conversation_store=store,
    )

    async with app.run_test(size=(100, 30)) as pilot:
        task = app.query_one("#task", MessageComposer)
        task.load_text("remember alpha")
        app._submit_current_task()
        await _wait_until(lambda: len(requests) == 1 and not app._agent_running)
        conversation_id = app._conversation_id
        first_context_id = requests[0].context_id
        assert conversation_id is not None

        await pilot.press("ctrl+l")
        await _wait_until(lambda: bool(app.query(InlineSelectorBlock)))
        await pilot.press("down", "enter")
        await _wait_until(lambda: not app.query(InlineSelectorBlock))

        assert app._conversation_id == conversation_id
        assert resources[0].closed
        assert [(block.role, block.content) for block in app.query(MessageBlock)] == [
            ("You", "remember alpha"),
            ("Assistant", "Finished in the full-screen UI."),
            ("You", "/model"),
        ]
        assert any(entry.title == "Model switched" for entry in app.query(TimelineEntry))

        task.load_text("what should you remember?")
        app._submit_current_task()
        await _wait_until(lambda: len(requests) == 2 and not app._agent_running)
        assert requests[1].model == "gpt-5.4"
        assert requests[1].context_id != first_context_id
        assert "User: remember alpha" in requests[1].instruction
        assert "Assistant: Finished in the full-screen UI." in requests[1].instruction

        snapshot = await store.load(conversation_id)
        assert snapshot.manifest.runtime == "codex-cli"
        assert snapshot.manifest.model_override == "gpt-5.4"
        assert any(
            event.event_type == ConversationEventKind.CONTEXT_CHANGED for event in snapshot.events
        )


async def test_native_runtime_switch_retains_conversation_but_new_command_clears_it(
    tmp_path: Path,
) -> None:
    store = ConversationStore(tmp_path / "conversations", durable=False)
    requests = []

    def factory(request, approval_policy, event_sink):
        requests.append(request)
        return FakeRunner(approval_policy=approval_policy, event_sink=event_sink), None

    app = RivumiApp(
        repository=tmp_path,
        config=CliConfig(runtime="claude-code"),
        runner_factory=factory,
        runtimes=(("claude-code", "Claude Code"), ("codex-cli", "Codex CLI")),
        providers=(("ollama", "Ollama"),),
        conversation_store=store,
    )

    async with app.run_test(size=(100, 30)) as pilot:
        task = app.query_one("#task", MessageComposer)
        task.load_text("keep this context")
        app._submit_current_task()
        await _wait_until(lambda: len(requests) == 1 and not app._agent_running)
        conversation_id = app._conversation_id
        assert conversation_id is not None

        task.load_text("/runtime")
        await pilot.press("enter")
        await _wait_until(lambda: bool(app.query(InlineSelectorBlock)))
        await pilot.press("down", "enter")
        await _wait_until(lambda: not app.query(InlineSelectorBlock))
        assert app._conversation_id == conversation_id
        assert app.config.runtime == "codex-cli"

        task.load_text("continue after the switch")
        app._submit_current_task()
        await _wait_until(lambda: len(requests) == 2 and not app._agent_running)
        assert requests[1].runtime == "codex-cli"
        assert "User: keep this context" in requests[1].instruction
        assert (await store.load(conversation_id)).manifest.runtime == "codex-cli"

        task.load_text("/new")
        app._submit_current_task()
        await _wait_until(lambda: app._conversation_id is None)
        assert list(app.query(MessageBlock)) == []
        assert app._ask_history == []


async def test_resume_displays_persisted_failure_without_replaying_it(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path / "conversations", durable=False)
    created = await store.create(runtime="codex-cli", title="failed research")
    _, lease = await store.resume(created.manifest.conversation_id)
    turn_id = uuid4().hex
    try:
        await store.append(
            lease,
            ConversationEventKind.USER_MESSAGE,
            turn_id=turn_id,
            text="research this",
        )
        await store.append(
            lease,
            ConversationEventKind.ASSISTANT_CHUNK,
            turn_id=turn_id,
            text="partial answer",
        )
        await store.append(
            lease,
            ConversationEventKind.TURN_FAILED,
            turn_id=turn_id,
            reason="conversation_turn_failed",
            error="Workspace audit failed: reported paths did not match",
        )
    finally:
        lease.release()

    app = RivumiApp(
        repository=tmp_path,
        config=CliConfig(runtime="codex-cli"),
        runner_factory=lambda *_args: (FakeRunner(), None),
        runtimes=(("codex-cli", "Codex CLI"),),
        providers=(("ollama", "Ollama"),),
        conversation_store=store,
    )

    async with app.run_test(size=(100, 30)):
        task = app.query_one("#task", MessageComposer)
        task.load_text(f"/resume {created.manifest.conversation_id}")
        app._submit_current_task()
        await _wait_until(lambda: app._conversation_id == created.manifest.conversation_id)
        await _wait_until(
            lambda: any(entry.title == "Previous run failed" for entry in app.query(TimelineEntry))
        )

        failures = [
            entry for entry in app.query(TimelineEntry) if entry.title == "Previous run failed"
        ]
        assert len(failures) == 1
        assert "Workspace audit failed" in (failures[0].detail or "")
        assert list(app.query(MessageBlock)) == []
        assert app._ask_history == []


async def test_tui_approval_is_attached_inline_and_maps_once_decision(tmp_path: Path) -> None:
    decision: dict[str, ApprovalDecision] = {}

    class ApprovalRunner(FakeRunner):
        async def run(self) -> RunResult:
            request = ApprovalRequest(
                run_id="approval-run",
                action_id="edit-1",
                effect=ToolEffect.MODIFY,
                reason=ApprovalReason.MODEL_TOOL,
                preview="replace src/[example].py x[/bold]",
                tool_call=ToolCall(name="replace_text"),
            )
            decision["value"] = await self.approval_policy.decide(request)
            return await super().run()

    def factory(_request, approval_policy, event_sink):
        return ApprovalRunner(approval_policy=approval_policy, event_sink=event_sink), FakeModel()

    app = RivumiApp(
        repository=tmp_path,
        config=CliConfig(provider="ollama", model="qwen3:4b"),
        runner_factory=factory,
        providers=(("ollama", "Ollama local"),),
        initial_prompt="Edit the example.",
    )

    async with app.run_test(size=(100, 30)) as pilot:
        await _wait_until(lambda: bool(app.query(InlineApprovalBlock)))
        approval = app.query_one(InlineApprovalBlock)
        preview = approval.query_one(".preview", Static)
        assert str(preview.content) == "replace src/[example].py x[/bold]"
        choices = approval.query_one(".approval-choices", OptionList)
        assert [str(option.prompt) for option in choices.options] == [
            "› 1  Allow once",
            "  2  Allow for this session",
            "  3  Deny this action",
            "  4  Cancel run",
        ]
        await pilot.pause()
        assert approval.region.width <= 100
        assert approval.region.height <= 12
        assert choices.region.height == 4

        await pilot.resize_terminal(60, 22)
        await pilot.pause()
        assert approval.region.width <= 60
        assert approval.region.height <= 12
        assert choices.region.height == 4
        assert preview.region.right <= 60
        app.action_approval_choice(0)
        await pilot.pause()
        await _wait_until(lambda: app._result is not None)
        assert decision["value"] == ApprovalDecision.ALLOW_ONCE


async def test_blank_approval_preview_is_actionable_and_defaults_to_deny(
    tmp_path: Path,
) -> None:
    decision: dict[str, ApprovalDecision] = {}

    class ApprovalRunner(FakeRunner):
        async def run(self) -> RunResult:
            request = ApprovalRequest(
                run_id="approval-run",
                action_id="edit-blank",
                effect=ToolEffect.MODIFY,
                reason=ApprovalReason.MODEL_TOOL,
                preview="",
                tool_call=ToolCall(
                    name="external_file_change",
                    arguments={
                        "available_decisions": [
                            ApprovalDecision.ALLOW_ONCE.value,
                            ApprovalDecision.DENY.value,
                        ]
                    },
                ),
            )
            decision["value"] = await self.approval_policy.decide(request)
            return await super().run()

    def factory(_request, approval_policy, event_sink):
        return ApprovalRunner(approval_policy=approval_policy, event_sink=event_sink), FakeModel()

    app = RivumiApp(
        repository=tmp_path,
        config=CliConfig(provider="ollama", model="qwen3:4b"),
        runner_factory=factory,
        providers=(("ollama", "Ollama local"),),
        initial_prompt="Edit the example.",
    )

    async with app.run_test(size=(100, 30)) as pilot:
        await _wait_until(lambda: bool(app.query(InlineApprovalBlock)))
        approval = app.query_one(InlineApprovalBlock)
        await pilot.pause()
        rendered = str(approval.query_one(".preview", Static).content)
        assert "Action: file change" in rendered
        assert "Effect: modify" in rendered
        assert "runtime did not provide" in rendered
        assert "No preview supplied" not in rendered
        choices = approval.query_one(".approval-choices", OptionList)
        assert [str(option.prompt) for option in choices.options] == [
            "  1  Allow once",
            "› 2  Deny this action",
        ]
        assert choices.highlighted == 1
        assert app.screen.focused is choices

        await pilot.press("enter")
        await pilot.pause()
        await _wait_until(lambda: app._result is not None)
        assert decision["value"] == ApprovalDecision.DENY


def test_approval_fallback_names_verification_command() -> None:
    request = ApprovalRequest(
        run_id="approval-run",
        action_id="verify",
        effect=ToolEffect.EXECUTE,
        reason=ApprovalReason.FINAL_VERIFICATION,
        command=VerificationCommand(name="tests", argv=("pytest", "-q")),
    )

    rendered = ApprovalModal._preview_text(request)

    assert "Action: verification command (tests)" in rendered
    assert "Effect: execute" in rendered
    assert "No preview supplied" not in rendered


async def test_allow_session_is_reused_across_bounded_tasks() -> None:
    class ApprovalApp:
        calls = 0

        async def request_approval(self, _request):
            self.calls += 1
            return ApprovalDecision.ALLOW_SESSION

    app = ApprovalApp()
    grants: set[ProcessLocalGrant] = set()
    request = ApprovalRequest(
        run_id="approval-run",
        action_id="external-runtime",
        effect=ToolEffect.MODIFY,
        reason=ApprovalReason.MODEL_TOOL,
        preview="Allow delegated edit",
        tool_call=ToolCall(name="external_agent", arguments={"backend": "codex-cli"}),
    )

    first = await TextualApprovalPolicy(app, grants).decide(request)
    second = await TextualApprovalPolicy(app, grants).decide(request)

    assert first == ApprovalDecision.ALLOW_SESSION
    assert second == ApprovalDecision.ALLOW_ONCE
    assert grants == {ProcessLocalGrant(effect=ToolEffect.MODIFY, scope="external_agent:codex-cli")}
    assert app.calls == 1

    core_request = ApprovalRequest(
        run_id="approval-run",
        action_id="edit-1",
        effect=ToolEffect.MODIFY,
        reason=ApprovalReason.MODEL_TOOL,
        preview="Edit one file",
        tool_call=ToolCall(name="replace_text"),
    )
    await TextualApprovalPolicy(app, grants).decide(core_request)
    assert app.calls == 2


async def test_permission_modes_enforce_read_only_and_accept_edits() -> None:
    class ApprovalApp:
        calls = 0
        _permission_mode = PermissionMode.ACCEPT_EDITS

        async def request_approval(self, _request):
            self.calls += 1
            return ApprovalDecision.ALLOW_ONCE

    app = ApprovalApp()
    request = ApprovalRequest(
        run_id="approval-run",
        action_id="edit-1",
        effect=ToolEffect.MODIFY,
        reason=ApprovalReason.MODEL_TOOL,
        preview="diff",
        tool_call=ToolCall(
            name="external_file_change",
            arguments={"grant_scope": "Edit:src/app.py"},
        ),
    )
    policy = TextualApprovalPolicy(app, set())

    assert await policy.decide(request) == ApprovalDecision.ALLOW_ONCE
    assert app.calls == 0

    app._permission_mode = PermissionMode.READ_ONLY
    assert await policy.decide(request) == ApprovalDecision.DENY
    assert app.calls == 0


async def test_ctrl_c_resolves_open_approval_as_cancel(tmp_path: Path) -> None:
    decision: dict[str, ApprovalDecision] = {}

    class ApprovalRunner(FakeRunner):
        async def run(self) -> RunResult:
            request = ApprovalRequest(
                run_id="approval-cancel-run",
                action_id="edit-2",
                effect=ToolEffect.MODIFY,
                reason=ApprovalReason.MODEL_TOOL,
                preview="unsafe[/bold]preview",
                tool_call=ToolCall(name="replace_text"),
            )
            decision["value"] = await self.approval_policy.decide(request)
            return await super().run()

    def factory(_request, approval_policy, event_sink):
        return ApprovalRunner(approval_policy=approval_policy, event_sink=event_sink), FakeModel()

    app = RivumiApp(
        repository=tmp_path,
        config=CliConfig(provider="ollama", model="qwen3:4b"),
        runner_factory=factory,
        providers=(("ollama", "Ollama local"),),
        initial_prompt="Edit the example.",
    )

    async with app.run_test(size=(100, 30)) as pilot:
        await _wait_until(lambda: bool(app.query(InlineApprovalBlock)))
        app.action_stop_or_quit()
        await _wait_until(lambda: "value" in decision)
        await _wait_until(lambda: app._result is not None)
        await pilot.pause()
        assert decision["value"] == ApprovalDecision.CANCEL


async def test_later_factory_failure_clears_previous_success(tmp_path: Path) -> None:
    calls = 0

    def factory(_request, approval_policy, event_sink):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("second failed")
        return FakeRunner(approval_policy=approval_policy, event_sink=event_sink), FakeModel()

    app = RivumiApp(
        repository=tmp_path,
        config=CliConfig(provider="ollama", model="qwen3:4b"),
        runner_factory=factory,
        providers=(("ollama", "Ollama local"),),
    )

    async with app.run_test(size=(100, 30)) as pilot:
        task = app.query_one("#task")
        task.load_text("First run")
        app._submit_current_task()
        await _wait_until(lambda: app._result is not None and not app._agent_running)
        task.load_text("Second run")
        app._submit_current_task()
        await _wait_until(lambda: app.last_error is not None)
        assert app._result is None
        assert app.last_error == "Run failed: second failed"
        await pilot.pause()


async def test_provider_close_failure_still_cleans_worker_state(tmp_path: Path) -> None:
    app = RivumiApp(
        repository=tmp_path,
        config=CliConfig(provider="ollama", model="qwen3:4b"),
        runner_factory=lambda _request, approval_policy, event_sink: (
            FakeRunner(approval_policy=approval_policy, event_sink=event_sink),
            FailingCloseModel(),
        ),
        providers=(("ollama", "Ollama local"),),
        initial_prompt="Run once.",
    )

    async with app.run_test(size=(100, 30)):
        await _wait_until(lambda: app.last_error is not None)
        assert app.last_error == "Provider cleanup failed: close exploded"
        assert app._runner is None
        assert app._model is None
        assert app._agent_running is False


@pytest.mark.parametrize("shortcut", ["ctrl+q", "escape"])
async def test_keyboard_shortcuts_request_cooperative_stop(tmp_path: Path, shortcut: str) -> None:
    started = asyncio.Event()
    cancel_requested = asyncio.Event()

    class BlockingRunner(FakeRunner):
        def request_cancel(self) -> None:
            cancel_requested.set()

        async def run(self) -> RunResult:
            started.set()
            await cancel_requested.wait()
            return RunResult(
                run_id="ctrl-q-run",
                task_id="ctrl-q-task",
                status=RunStatus.CANCELLED,
                summary="stopped safely",
                terminal_reason="user_cancelled",
            )

    app = RivumiApp(
        repository=tmp_path,
        config=CliConfig(provider="ollama", model="qwen3:4b"),
        runner_factory=lambda _request, approval_policy, event_sink: (
            BlockingRunner(approval_policy=approval_policy, event_sink=event_sink),
            FakeModel(),
        ),
        providers=(("ollama", "Ollama local"),),
        initial_prompt="Wait for stop.",
    )

    async with app.run_test(size=(100, 30)) as pilot:
        await asyncio.wait_for(started.wait(), timeout=1)
        await pilot.press(shortcut)
        await asyncio.wait_for(cancel_requested.wait(), timeout=1)
        await _wait_until(lambda: app._result is not None)
        assert app._result is not None
        assert app._result.status == RunStatus.CANCELLED


@pytest.mark.parametrize("shortcut", ["ctrl+c", "ctrl+d"])
async def test_idle_exit_requires_confirmed_second_press(
    tmp_path: Path, shortcut: str
) -> None:
    exits: list[RunResult | None] = []
    app = RivumiApp(
        repository=tmp_path,
        config=CliConfig(provider="ollama", model="qwen3:4b"),
        runner_factory=lambda *_: (FakeRunner(), None),
        providers=(("ollama", "Ollama local"),),
    )

    async with app.run_test(size=(100, 30)) as pilot:
        app.exit = lambda result=None, *args, **kwargs: exits.append(result)  # type: ignore[method-assign]
        composer = app.query_one("#task", MessageComposer)
        composer.focus()
        await pilot.press(shortcut)
        assert exits == []
        status = str(app.query_one("#status", Static).render())
        label = f"Ctrl-{shortcut.removeprefix('ctrl+').upper()}"
        assert f"Press {label} again to exit" in status

        await pilot.press(shortcut)
        assert exits == [None]


async def test_mixed_exit_keys_do_not_confirm(tmp_path: Path) -> None:
    exits: list[RunResult | None] = []
    app = RivumiApp(
        repository=tmp_path,
        config=CliConfig(provider="ollama", model="qwen3:4b"),
        runner_factory=lambda *_: (FakeRunner(), None),
        providers=(("ollama", "Ollama local"),),
    )

    async with app.run_test(size=(100, 30)) as pilot:
        app.exit = lambda result=None, *args, **kwargs: exits.append(result)  # type: ignore[method-assign]
        app.query_one("#task", MessageComposer).focus()
        await pilot.press("ctrl+c")
        await pilot.press("ctrl+d")
        assert exits == []


async def test_ctrl_c_with_draft_clears_draft_before_exiting(tmp_path: Path) -> None:
    exits: list[RunResult | None] = []
    app = RivumiApp(
        repository=tmp_path,
        config=CliConfig(provider="ollama", model="qwen3:4b"),
        runner_factory=lambda *_: (FakeRunner(), None),
        providers=(("ollama", "Ollama local"),),
    )

    async with app.run_test(size=(100, 30)) as pilot:
        app.exit = lambda result=None, *args, **kwargs: exits.append(result)  # type: ignore[method-assign]
        composer = app.query_one("#task", MessageComposer)
        composer.focus()
        composer.set_text("precious draft")
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert exits == []
        assert composer.text == ""
        assert "Draft cleared" in str(app.query_one("#status", Static).render())

        await pilot.press("ctrl+c")
        await pilot.press("ctrl+c")
        assert exits == [None]


async def test_idle_escape_never_exits_and_is_invisible(tmp_path: Path) -> None:
    exits: list[RunResult | None] = []
    app = RivumiApp(
        repository=tmp_path,
        config=CliConfig(provider="ollama", model="qwen3:4b"),
        runner_factory=lambda *_: (FakeRunner(), None),
        providers=(("ollama", "Ollama local"),),
    )

    async with app.run_test(size=(100, 30)) as pilot:
        app.exit = lambda result=None, *args, **kwargs: exits.append(result)  # type: ignore[method-assign]
        composer = app.query_one("#task", MessageComposer)
        composer.focus()
        composer.set_text("keep me")
        await pilot.press("escape")
        await pilot.pause()
        assert exits == []
        assert composer.text == "keep me"

        composer.load_text("")
        await pilot.press("escape")
        await pilot.press("escape")
        await pilot.pause()
        assert exits == []


async def test_second_escape_after_cancellation_does_not_exit(tmp_path: Path) -> None:
    started = asyncio.Event()
    cancel_requested = asyncio.Event()

    class BlockingRunner(FakeRunner):
        def request_cancel(self) -> None:
            cancel_requested.set()

        async def run(self) -> RunResult:
            started.set()
            await cancel_requested.wait()
            return RunResult(
                run_id="esc-run",
                task_id="esc-task",
                status=RunStatus.CANCELLED,
                summary="stopped",
                terminal_reason="user_cancelled",
            )

    exits: list[RunResult | None] = []
    app = RivumiApp(
        repository=tmp_path,
        config=CliConfig(provider="ollama", model="qwen3:4b"),
        runner_factory=lambda _request, approval_policy, event_sink: (
            BlockingRunner(approval_policy=approval_policy, event_sink=event_sink),
            FakeModel(),
        ),
        providers=(("ollama", "Ollama local"),),
        initial_prompt="Wait for stop.",
    )

    async with app.run_test(size=(100, 30)) as pilot:
        app.exit = lambda result=None, *args, **kwargs: exits.append(result)  # type: ignore[method-assign]
        await asyncio.wait_for(started.wait(), timeout=1)
        await pilot.press("escape")
        await asyncio.wait_for(cancel_requested.wait(), timeout=1)
        await _wait_until(lambda: not app._agent_running)

        assert app._escape_idle_armed_at is None
        await pilot.press("escape")
        await pilot.press("escape")
        await pilot.pause()
        assert exits == []


async def test_active_escape_does_not_arm_idle_rewind(tmp_path: Path) -> None:
    started = asyncio.Event()
    cancel_requested = asyncio.Event()

    class BlockingRunner(FakeRunner):
        def request_cancel(self) -> None:
            cancel_requested.set()

        async def run(self) -> RunResult:
            started.set()
            await cancel_requested.wait()
            return RunResult(
                run_id="arm-run",
                task_id="arm-task",
                status=RunStatus.CANCELLED,
                summary="stopped",
                terminal_reason="user_cancelled",
            )

    app = RivumiApp(
        repository=tmp_path,
        config=CliConfig(provider="ollama", model="qwen3:4b"),
        runner_factory=lambda _request, approval_policy, event_sink: (
            BlockingRunner(approval_policy=approval_policy, event_sink=event_sink),
            FakeModel(),
        ),
        providers=(("ollama", "Ollama local"),),
        initial_prompt="Interrupt me.",
    )

    async with app.run_test(size=(100, 30)) as pilot:
        await asyncio.wait_for(started.wait(), timeout=1)
        await pilot.press("escape")
        await asyncio.wait_for(cancel_requested.wait(), timeout=1)
        assert app._escape_idle_armed_at is None


async def test_escape_on_inline_approval_dismisses_only_the_approval(
    tmp_path: Path,
) -> None:
    decision: dict[str, ApprovalDecision | None] = {"value": None}

    class ApprovalRunner(FakeRunner):
        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)

        async def run(self) -> RunResult:
            request = ApprovalRequest(
                run_id="approval-escape-run",
                action_id="edit-3",
                effect=ToolEffect.MODIFY,
                reason=ApprovalReason.MODEL_TOOL,
                preview="preview",
                tool_call=ToolCall(name="replace_text"),
            )
            decision["value"] = await self.approval_policy.decide(request)
            return await super().run()

    exits: list[RunResult | None] = []
    app = RivumiApp(
        repository=tmp_path,
        config=CliConfig(provider="ollama", model="qwen3:4b"),
        runner_factory=lambda _request, approval_policy, event_sink: (
            ApprovalRunner(approval_policy=approval_policy, event_sink=event_sink),
            FakeModel(),
        ),
        providers=(("ollama", "Ollama local"),),
        initial_prompt="Edit the example.",
    )

    async with app.run_test(size=(100, 30)) as pilot:
        app.exit = lambda result=None, *args, **kwargs: exits.append(result)  # type: ignore[method-assign]
        await _wait_until(lambda: bool(app.query(InlineApprovalBlock)))
        await pilot.press("escape")
        await _wait_until(lambda: decision["value"] == ApprovalDecision.CANCEL)
        await _wait_until(lambda: not app.query(InlineApprovalBlock))
        assert exits == []


async def test_exit_command_stops_active_agent_then_closes(tmp_path: Path) -> None:
    started = asyncio.Event()
    cancel_requested = asyncio.Event()
    exits: list[RunResult | None] = []

    class BlockingRunner(FakeRunner):
        def request_cancel(self) -> None:
            cancel_requested.set()

        async def run(self) -> RunResult:
            started.set()
            await cancel_requested.wait()
            return RunResult(
                run_id="exit-run",
                task_id="exit-task",
                status=RunStatus.CANCELLED,
                summary="stopped for exit",
                terminal_reason="user_cancelled",
            )

    app = RivumiApp(
        repository=tmp_path,
        config=CliConfig(provider="ollama", model="qwen3:4b"),
        runner_factory=lambda _request, approval_policy, event_sink: (
            BlockingRunner(approval_policy=approval_policy, event_sink=event_sink),
            None,
        ),
        providers=(("ollama", "Ollama local"),),
    )

    async with app.run_test(size=(100, 30)):
        app.exit = lambda result=None, *args, **kwargs: exits.append(result)  # type: ignore[method-assign]
        composer = app.query_one("#task", MessageComposer)
        composer.load_text("keep running")
        app._submit_current_task()
        await asyncio.wait_for(started.wait(), timeout=1)

        composer.load_text("/quit")
        app._submit_current_task()
        await asyncio.wait_for(cancel_requested.wait(), timeout=1)
        await _wait_until(lambda: bool(exits))

        assert exits[0] is not None
        assert exits[0].status == RunStatus.CANCELLED


async def test_textual_worker_cancel_is_translated_to_cooperative_stop(tmp_path: Path) -> None:
    started = asyncio.Event()
    cancel_requested = asyncio.Event()

    class BlockingRunner(FakeRunner):
        def request_cancel(self) -> None:
            cancel_requested.set()

        async def run(self) -> RunResult:
            started.set()
            await cancel_requested.wait()
            return RunResult(
                run_id="worker-cancel-run",
                task_id="worker-cancel-task",
                status=RunStatus.CANCELLED,
                summary="stopped safely",
                terminal_reason="user_cancelled",
            )

    app = RivumiApp(
        repository=tmp_path,
        config=CliConfig(provider="ollama", model="qwen3:4b"),
        runner_factory=lambda _request, approval_policy, event_sink: (
            BlockingRunner(approval_policy=approval_policy, event_sink=event_sink),
            FakeModel(),
        ),
        providers=(("ollama", "Ollama local"),),
        initial_prompt="Wait for worker cancellation.",
    )

    async with app.run_test(size=(100, 30)):
        await asyncio.wait_for(started.wait(), timeout=1)
        worker = next(worker for worker in app.workers if worker.name == "_run_agent")
        worker.cancel()
        await asyncio.wait_for(cancel_requested.wait(), timeout=1)
        await _wait_until(lambda: app._result is not None)
        assert app._result is not None
        assert app._result.status == RunStatus.CANCELLED


async def test_delayed_event_from_previous_generation_is_ignored(tmp_path: Path) -> None:
    app = RivumiApp(
        repository=tmp_path,
        config=CliConfig(provider="ollama", model="qwen3:4b"),
        runner_factory=lambda *_: (FakeRunner(), FakeModel()),
        providers=(("ollama", "Ollama local"),),
    )

    async with app.run_test(size=(100, 30)):
        app._generation = 2
        app.event_received(
            RunEventMessage(
                RunEvent(
                    event_type="run.created",
                    run_id="old-run",
                    task_id="old-task",
                    sequence=0,
                    data={},
                ),
                generation=1,
            )
        )
        assert app._projection.run_id is None
        app.event_received(
            RunEventMessage(
                RunEvent(
                    event_type="run.created",
                    run_id="new-run",
                    task_id="new-task",
                    sequence=0,
                    data={},
                ),
                generation=2,
            )
        )
        assert app._projection.run_id == "new-run"


async def test_tool_lifecycle_updates_one_inline_action_in_place(tmp_path: Path) -> None:
    app = RivumiApp(
        repository=tmp_path,
        config=CliConfig(provider="ollama", model="qwen3:4b"),
        runner_factory=lambda *_: (FakeRunner(), FakeModel()),
        providers=(("ollama", "Ollama local"),),
    )

    async with app.run_test(size=(100, 30)) as pilot:
        app._generation = 1
        events = (
            (
                "tool.requested",
                {
                    "tool_call_id": "call-1",
                    "name": "read_file",
                    "arguments": {"path": "src/app.py"},
                },
            ),
            ("tool.started", {"tool_call_id": "call-1", "name": "read_file"}),
            (
                "tool.completed",
                {"tool_call_id": "call-1", "name": "read_file", "ok": True, "preview": "84 lines"},
            ),
        )
        for sequence, (event_type, data) in enumerate(events):
            app.event_received(
                RunEventMessage(
                    RunEvent(
                        event_type=event_type,
                        run_id="run",
                        task_id="task",
                        sequence=sequence,
                        data=data,
                    ),
                    generation=1,
                )
            )
            await pilot.pause()

        actions = list(app.query(ToolActionBlock))
        assert len(actions) == 1
        assert actions[0].title == "Read src/app.py"
        assert actions[0].status == "completed"
        assert actions[0].detail == "84 lines"


async def test_runtime_stream_projects_one_assistant_and_correlated_tool(
    tmp_path: Path,
) -> None:
    app = RivumiApp(
        repository=tmp_path,
        config=CliConfig(runtime="codex-cli"),
        runner_factory=lambda *_: (FakeRunner(), None),
        runtimes=(("codex-cli", "Codex CLI"),),
        providers=(("ollama", "Ollama"),),
    )

    async with app.run_test(size=(100, 30)) as pilot:
        app._generation = 1
        events = (
            TextDeltaEvent(sequence=0, turn_id="turn", text="I will inspect. "),
            TextDeltaEvent(sequence=1, turn_id="turn", text="Found it."),
            ToolStartedEvent(
                sequence=2,
                turn_id="turn",
                action_id="action",
                kind=RuntimeToolKind.COMMAND,
                tool_name="Run",
                effect=ToolEffect.EXECUTE,
                summary="pytest -q",
            ),
            ToolOutputDeltaEvent(
                sequence=3,
                turn_id="turn",
                action_id="action",
                text="1 passed",
            ),
            ToolCompletedEvent(
                sequence=4,
                turn_id="turn",
                action_id="action",
                status=RuntimeToolStatus.COMPLETED,
                output="1 passed",
            ),
        )
        for event in events:
            app.conversation_runtime_event_received(
                ConversationRuntimeEventMessage(event, generation=1)
            )
            await pilot.pause()

        messages = list(app.query(MessageBlock))
        actions = list(app.query(ToolActionBlock))
        assert len(messages) == 1
        assert messages[0].content == "I will inspect. Found it."
        assert len(actions) == 1
        assert actions[0].status == "completed"
        assert actions[0].detail == "1 passed"


async def test_context_command_uses_runtime_token_telemetry_not_character_count(
    tmp_path: Path,
) -> None:
    app = RivumiApp(
        repository=tmp_path,
        config=CliConfig(runtime="codex-cli"),
        runner_factory=lambda *_: (FakeRunner(), None),
        runtimes=(("codex-cli", "Codex CLI"),),
        providers=(("ollama", "Ollama"),),
    )

    async with app.run_test(size=(100, 30)) as pilot:
        app._generation = 1
        app.conversation_runtime_event_received(
            ConversationRuntimeEventMessage(
                ContextUsageUpdatedEvent(
                    sequence=0,
                    turn_id="turn",
                    telemetry=ContextTelemetry(
                        accuracy="exact",
                        input_tokens=900,
                        cached_input_tokens=400,
                        output_tokens=100,
                        total_tokens=1_000,
                        context_window=10_000,
                    ),
                ),
                generation=1,
            )
        )
        app._dispatch_command("/context")
        await pilot.pause()

        entry = [item for item in app.query(TimelineEntry) if item.title == "Context"][-1]
        assert "1,000 tokens (exact)" in (entry.detail or "")
        assert "10.0% of 10,000" in (entry.detail or "")
        assert "characters" not in (entry.detail or "")


async def test_runtime_reported_model_updates_header_without_becoming_override(
    tmp_path: Path,
) -> None:
    app = RivumiApp(
        repository=tmp_path,
        config=CliConfig(runtime="claude-code"),
        runner_factory=lambda *_: (FakeRunner(), None),
        runtimes=(("claude-code", "Claude Code"),),
        runtime_models={
            "claude-code": (
                ("Automatic", None),
                ("Sonnet", "sonnet"),
            )
        },
        providers=(("anthropic", "Anthropic"),),
    )

    async with app.run_test(size=(120, 30)) as pilot:
        app._generation = 1
        app.conversation_runtime_event_received(
            ConversationRuntimeEventMessage(
                RuntimeModelUpdatedEvent(
                    sequence=0,
                    turn_id="turn",
                    model="claude-opus-4-5-20251101",
                ),
                generation=1,
            )
        )
        await pilot.pause()

        assert app.config.runtime_model is None
        assert "claude-opus-4-5-20251101" in str(app.query_one("#context").content)
        app._dispatch_command("/status")
        status = [item for item in app.query(TimelineEntry) if item.title == "Status"][-1]
        assert "claude-opus-4-5-20251101" in (status.detail or "")

        composer = app.query_one("#task", MessageComposer)
        composer.load_text("/model")
        await pilot.press("enter")
        await _wait_until(lambda: bool(app.query(InlineSelectorBlock)))
        selector = app.query_one(InlineSelectorBlock)
        assert "claude-opus-4-5-20251101" in selector.description


async def test_multiline_composer_and_slash_palette_are_keyboard_driven(tmp_path: Path) -> None:
    app = RivumiApp(
        repository=tmp_path,
        config=CliConfig(runtime="codex-cli"),
        runner_factory=lambda *_: (FakeRunner(), None),
        runtimes=(("codex-cli", "Codex CLI"),),
        runtime_models={
            "codex-cli": (
                ("Automatic", None),
                ("Sonnet", "sonnet"),
                ("Opus", "opus"),
            )
        },
        providers=(("ollama", "Ollama"),),
    )

    async with app.run_test(size=(100, 30)) as pilot:
        composer = app.query_one("#task", MessageComposer)
        composer.focus()
        composer.set_text("first line")
        await pilot.press("shift+enter")
        await pilot.press("s", "e", "c", "o", "n", "d")
        assert composer.text == "first line\nsecond"

        composer.load_text("")
        await pilot.press("/", "m", "o", "d", "e", "l")
        await pilot.pause()
        menu = app.query_one("#command-menu", OptionList)
        assert composer.text == "/model"
        assert menu.display is True
        assert menu.option_count == 1
        await pilot.press("enter")
        await _wait_until(lambda: bool(app.query(InlineSelectorBlock)))
        selector = app.query_one(InlineSelectorBlock)
        assert [option.value for option in selector.options] == [
            "__automatic__",
            "sonnet",
            "opus",
        ]
        await pilot.press("escape")
        await _wait_until(lambda: not app.query(InlineSelectorBlock))

        composer.load_text("/model ")
        await pilot.pause()
        assert menu.display is True
        assert menu.option_count == 3

        composer.load_text("/model son")
        await pilot.pause()
        assert [str(option.prompt) for option in menu.options] == ["Sonnet  sonnet"]

        composer.load_text("/modelsonnet")
        await pilot.pause()
        assert menu.display is False

        composer.load_text("/")
        await pilot.pause()
        assert menu.display is True
        assert menu.option_count >= 10

        composer.load_text("/runtime ")
        await pilot.pause()
        assert menu.display is True
        assert [str(option.prompt) for option in menu.options] == ["Codex CLI  codex-cli"]
        await pilot.press("tab")
        assert composer.text == "/runtime codex-cli"
        assert menu.display is False

        composer.load_text("/permissions")
        await pilot.press("enter")
        await _wait_until(lambda: bool(app.query(InlineSelectorBlock)))
        await pilot.press("down", "enter")
        await pilot.pause()
        assert app._permission_mode == PermissionMode.ACCEPT_EDITS
        assert composer.text == ""

        composer.load_text("/compact keep build failures")
        await pilot.pause()
        assert menu.display is False
        assert composer.text == "/compact keep build failures"

        composer.load_text("/compact\tkeep build failures")
        await pilot.pause()
        assert menu.display is False
        assert composer.text == "/compact\tkeep build failures"


async def test_inline_selector_escape_is_non_mutating_and_restores_focus(tmp_path: Path) -> None:
    app = RivumiApp(
        repository=tmp_path,
        config=CliConfig(runtime="codex-cli"),
        runner_factory=lambda *_: (FakeRunner(), None),
        runtimes=(("codex-cli", "Codex CLI"),),
        runtime_models={
            "codex-cli": (
                ("Automatic", None),
                ("A deliberately long model label", "long-model-id"),
            )
        },
        providers=(("ollama", "Ollama"),),
    )

    async with app.run_test(size=(52, 18)) as pilot:
        composer = app.query_one("#task", MessageComposer)
        composer.load_text("/model")
        await pilot.press("enter")
        await _wait_until(lambda: bool(app.query(InlineSelectorBlock)))

        selector = app.query_one(InlineSelectorBlock)
        assert composer.disabled is True
        assert app.query_one("#composer").display is False
        assert selector.size.width <= app.size.width
        assert selector.query_one(OptionList).has_focus is True

        await pilot.press("escape")
        await _wait_until(lambda: not app.query(InlineSelectorBlock))
        assert app.config.runtime_model is None
        assert composer.disabled is False
        assert app.query_one("#composer").display is True
        assert composer.has_focus is True

        composer.load_text("/permissions")
        await pilot.press("enter")
        await _wait_until(lambda: bool(app.query(InlineSelectorBlock)))
        await pilot.press("ctrl+c")
        await _wait_until(lambda: not app.query(InlineSelectorBlock))
        assert composer.has_focus is True


async def test_follow_up_queue_runs_in_fifo_order(tmp_path: Path) -> None:
    first_gate = asyncio.Event()
    requests = []

    class QueueRunner(FakeRunner):
        def __init__(self, request, **kwargs) -> None:
            super().__init__(**kwargs)
            self.request = request

        async def run(self) -> RunResult:
            if self.request.instruction == "first":
                await first_gate.wait()
            return RunResult(
                run_id=self.request.instruction,
                task_id=self.request.instruction,
                status=RunStatus.COMPLETED,
                summary=f"done {self.request.instruction}",
                terminal_reason="completed",
            )

    def factory(request, approval_policy, event_sink):
        requests.append(request)
        return QueueRunner(
            request,
            approval_policy=approval_policy,
            event_sink=event_sink,
        ), None

    app = RivumiApp(
        repository=tmp_path,
        config=CliConfig(provider="ollama", model="qwen3:4b"),
        runner_factory=factory,
        providers=(("ollama", "Ollama local"),),
    )

    async with app.run_test(size=(100, 30)) as pilot:
        composer = app.query_one("#task", MessageComposer)
        for prompt in ("first", "second", "third"):
            composer.load_text(prompt)
            app._submit_current_task()
            await pilot.pause()
        assert [request.instruction for request in requests] == ["first"]
        assert list(app._queued_prompts) == ["second", "third"]

        first_gate.set()
        await _wait_until(lambda: len(requests) == 3 and not app._agent_running, attempts=150)
        assert [request.instruction for request in requests] == ["first", "second", "third"]
        assert not app.query("#stop")


async def test_active_turn_keeps_idle_only_slash_command_in_composer(tmp_path: Path) -> None:
    gate = asyncio.Event()

    class BlockingRunner(FakeRunner):
        async def run(self) -> RunResult:
            await gate.wait()
            return await super().run()

    app = RivumiApp(
        repository=tmp_path,
        config=CliConfig(provider="ollama", model="qwen3:4b"),
        runner_factory=lambda _request, approval_policy, event_sink: (
            BlockingRunner(approval_policy=approval_policy, event_sink=event_sink),
            None,
        ),
        providers=(("ollama", "Ollama local"),),
    )

    async with app.run_test(size=(100, 30)) as pilot:
        composer = app.query_one("#task", MessageComposer)
        composer.load_text("first")
        app._submit_current_task()
        await _wait_until(lambda: app._agent_running)

        composer.load_text("/compact keep build failures")
        app._submit_current_task()
        await pilot.pause()
        assert composer.text == "/compact keep build failures"
        assert "kept in composer" in str(app.query_one("#status", Static).render())

        gate.set()
        await _wait_until(lambda: not app._agent_running)
        assert composer.text == "/compact keep build failures"


async def test_escape_cancels_native_startup_before_runner_exists(tmp_path: Path) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    factory_calls = 0

    class SlowStartApp(RivumiApp):
        async def _begin_conversation_turn(self, instruction: str) -> None:
            started.set()
            await release.wait()

    def factory(*_args):
        nonlocal factory_calls
        factory_calls += 1
        return FakeRunner(), None

    app = SlowStartApp(
        repository=tmp_path,
        config=CliConfig(runtime="codex-cli"),
        runner_factory=factory,
        runtimes=(("codex-cli", "Codex CLI"),),
        providers=(("ollama", "Ollama local"),),
        initial_prompt="wait during startup",
    )

    async with app.run_test(size=(100, 30)) as pilot:
        await asyncio.wait_for(started.wait(), timeout=1)
        await pilot.press("escape")
        release.set()
        await _wait_until(lambda: app._result is not None and not app._agent_running)
        assert app._result is not None
        assert app._result.status == RunStatus.CANCELLED
        assert factory_calls == 0


async def test_read_search_actions_collapse_into_one_keyboard_group(tmp_path: Path) -> None:
    app = RivumiApp(
        repository=tmp_path,
        config=CliConfig(provider="ollama", model="qwen3:4b"),
        runner_factory=lambda *_: (FakeRunner(), None),
        providers=(("ollama", "Ollama local"),),
    )

    async with app.run_test(size=(100, 30)) as pilot:
        first = app._ensure_tool_action("read", "Read src/app.py", detail_kind="read")
        first.set_state("completed", detail="84 lines")
        second = app._ensure_tool_action("search", 'Search "runner"', detail_kind="search")
        second.set_state("completed", detail="6 matches")
        await pilot.pause()

        groups = list(app.query(ToolGroupBlock))
        assert len(groups) == 1
        assert len(groups[0].actions) == 2
        assert groups[0].collapsed is True
        groups[0].query_one("CollapsibleTitle").focus()
        await pilot.press("enter")
        assert groups[0].collapsed is False


async def test_scrolled_transcript_reports_deduplicated_new_items(tmp_path: Path) -> None:
    app = RivumiApp(
        repository=tmp_path,
        config=CliConfig(provider="ollama", model="qwen3:4b"),
        runner_factory=lambda *_: (FakeRunner(), None),
        providers=(("ollama", "Ollama local"),),
    )

    async with app.run_test(size=(80, 20)) as pilot:
        for index in range(30):
            app._write_turn("Assistant", f"history {index}\nmore detail")
        await pilot.pause()
        transcript = app.query_one("#transcript")
        transcript.release_anchor()
        transcript.scroll_home(animate=False)
        await pilot.pause()

        app._write_timeline("Fresh result", "one")
        await pilot.pause()
        new_items = app.query_one("#new-items", Button)
        assert new_items.display is True
        assert "1 new" in str(new_items.label)

        await pilot.click("#new-items")
        await pilot.pause()
        assert new_items.display is False


def test_runtime_loading_uses_fixed_width_swimming_otter_frames() -> None:
    assert [cell_len(frame) for frame in RuntimeLoadingIndicator._FRAMES] == [8] * 6
    assert cell_len(RuntimeLoadingIndicator._STATIC_FRAME) == 8
    assert RuntimeLoadingIndicator._FRAMES == (
        "[🦦≋≋≋] ",
        "[≋🦦≋≋] ",
        "[≋≋🦦≋] ",
        "[≋≋≋🦦] ",
        "[≋≋🦦≋] ",
        "[≋🦦≋≋] ",
    )
    assert pytest.approx(0.20) == RuntimeLoadingIndicator._CADENCE


async def test_runtime_loading_follows_runtime_attention_states(tmp_path: Path) -> None:
    app = RivumiApp(
        repository=tmp_path,
        config=CliConfig(runtime="codex-cli"),
        runner_factory=lambda *_: (FakeRunner(), None),
        runtimes=(("codex-cli", "Codex CLI"),),
        providers=(("ollama", "Ollama"),),
    )

    async with app.run_test(size=(100, 30)) as pilot:
        app._generation = 1
        indicator = app.query_one("#loading-indicator", RuntimeLoadingIndicator)
        status = app.query_one("#status", RuntimeStatus)

        app.conversation_runtime_event_received(
            ConversationRuntimeEventMessage(
                TurnStartedEvent(sequence=0, turn_id="turn"), generation=1
            )
        )
        await pilot.pause()
        assert indicator.display is True
        assert indicator.phase == LoadingPhase.REQUESTING
        assert indicator.auto_refresh == pytest.approx(0.20)
        assert status.loading_label == "Thinking…"

        app.conversation_runtime_event_received(
            ConversationRuntimeEventMessage(
                TextDeltaEvent(sequence=1, turn_id="turn", text="Partial response"),
                generation=1,
            )
        )
        await pilot.pause()
        assert indicator.display is False
        assert indicator.phase is None
        assert indicator.auto_refresh is None
        assert status.loading_label == "Responding…"
        assert list(app.query(MessageBlock))[0].content == "Partial response"

        app.conversation_runtime_event_received(
            ConversationRuntimeEventMessage(
                TextDeltaEvent(
                    sequence=2,
                    turn_id="turn",
                    text=" continues\nunfinished",
                ),
                generation=1,
            )
        )
        await pilot.pause()
        assert indicator.display is False
        assert indicator.phase is None
        assert indicator.auto_refresh is None
        assert status.loading_label == "Responding…"
        assert list(app.query(MessageBlock))[0].content == (
            "Partial response continues\nunfinished"
        )

        app.conversation_runtime_event_received(
            ConversationRuntimeEventMessage(
                ToolStartedEvent(
                    sequence=3,
                    turn_id="turn",
                    action_id="action",
                    kind=RuntimeToolKind.COMMAND,
                    tool_name="shell",
                    effect=ToolEffect.EXECUTE,
                    summary="pytest -q",
                ),
                generation=1,
            )
        )
        await pilot.pause()
        assert indicator.display is True
        assert indicator.phase == LoadingPhase.TOOL_USE
        assert indicator.auto_refresh == pytest.approx(0.20)
        assert status.loading_label == "Using shell…"
        assert list(app.query(MessageBlock))[0].content == (
            "Partial response continues\nunfinished"
        )

        approval = RuntimeApprovalRequest(
            request_id="approval",
            turn_id="turn",
            action_id="action",
            kind=RuntimeApprovalKind.COMMAND,
            effect=ToolEffect.EXECUTE,
            preview="pytest -q",
            available_decisions=(ApprovalDecision.ALLOW_ONCE, ApprovalDecision.DENY),
        )
        app.conversation_runtime_event_received(
            ConversationRuntimeEventMessage(
                ApprovalRequestedEvent(sequence=4, turn_id="turn", approval=approval),
                generation=1,
            )
        )
        await pilot.pause()
        assert indicator.display is False
        assert status.loading_label is None
        assert str(status.content) == "Waiting for permission…"

        app.conversation_runtime_event_received(
            ConversationRuntimeEventMessage(
                ApprovalResolvedEvent(
                    sequence=5,
                    turn_id="turn",
                    request_id="approval",
                    decision=ApprovalDecision.ALLOW_ONCE,
                ),
                generation=1,
            )
        )
        await pilot.pause()
        assert indicator.display is True
        assert indicator.phase == LoadingPhase.TOOL_USE
        assert status.loading_label == "Using shell…"

        app.conversation_runtime_event_received(
            ConversationRuntimeEventMessage(
                ToolCompletedEvent(
                    sequence=6,
                    turn_id="turn",
                    action_id="action",
                    status=RuntimeToolStatus.COMPLETED,
                ),
                generation=1,
            )
        )
        app.conversation_runtime_event_received(
            ConversationRuntimeEventMessage(
                TurnCompletedEvent(
                    sequence=7,
                    turn_id="turn",
                    status=RuntimeTurnStatus.COMPLETED,
                ),
                generation=1,
            )
        )
        await pilot.pause()
        assert indicator.display is False
        assert indicator.auto_refresh is None
        assert status.loading_label is None


async def test_runtime_loading_status_glimmers_then_reveals_elapsed_time(
    tmp_path: Path,
) -> None:
    app = RivumiApp(
        repository=tmp_path,
        config=CliConfig(runtime="codex-cli"),
        runner_factory=lambda *_: (FakeRunner(), None),
        runtimes=(("codex-cli", "Codex CLI"),),
        providers=(("ollama", "Ollama"),),
    )

    async with app.run_test(size=(100, 30)):
        status = app.query_one("#status", RuntimeStatus)
        status.set_loading("Thinking…", LoadingPhase.THINKING)
        status._loading_started_at = monotonic() - 18.6

        rendered = status.render()
        assert rendered.plain == "Thinking… (18s)"
        assert any(span.style == "not dim bold" for span in rendered.spans)

        app.animation_level = "none"
        status.set_loading("Thinking…", LoadingPhase.THINKING)
        assert status.auto_refresh is None
        reduced_motion = status.render()
        assert reduced_motion.plain == "Thinking… (18s)"
        assert not any(span.style == "not dim bold" for span in reduced_motion.spans)


async def test_runtime_loading_respects_reduced_motion(tmp_path: Path) -> None:
    app = RivumiApp(
        repository=tmp_path,
        config=CliConfig(runtime="codex-cli"),
        runner_factory=lambda *_: (FakeRunner(), None),
        runtimes=(("codex-cli", "Codex CLI"),),
        providers=(("ollama", "Ollama"),),
    )

    async with app.run_test(size=(100, 30)):
        app._generation = 1
        app.animation_level = "none"
        app.conversation_runtime_event_received(
            ConversationRuntimeEventMessage(
                TurnStartedEvent(sequence=0, turn_id="turn"), generation=1
            )
        )
        indicator = app.query_one("#loading-indicator", RuntimeLoadingIndicator)

        assert indicator.display is True
        assert indicator.auto_refresh is None
        assert indicator.render().plain == "[≋🦦≋≋] "

        app.conversation_runtime_event_received(
            ConversationRuntimeEventMessage(
                TextDeltaEvent(sequence=1, turn_id="turn", text="Complete line\n"),
                generation=1,
            )
        )
        assert indicator.display is True
        assert list(app.query(MessageBlock)) == []

        app.conversation_runtime_event_received(
            ConversationRuntimeEventMessage(
                TurnCompletedEvent(
                    sequence=2,
                    turn_id="turn",
                    status=RuntimeTurnStatus.COMPLETED,
                ),
                generation=1,
            )
        )
        assert indicator.display is False
        assert list(app.query(MessageBlock))[0].content == "Complete line\n"


async def test_failed_native_turn_shows_exact_error_and_partial_changes(tmp_path: Path) -> None:
    exact_error = "Workspace audit failed: reported paths did not match"

    class FailedNativeRunner(FakeRunner):
        async def run(self) -> RunResult:
            events = (
                TurnStartedEvent(sequence=0, turn_id="turn"),
                TextDeltaEvent(sequence=1, turn_id="turn", text="Useful partial answer."),
                ToolStartedEvent(
                    sequence=2,
                    turn_id="turn",
                    action_id="change",
                    kind=RuntimeToolKind.FILE_CHANGE,
                    tool_name="file_change",
                    effect=ToolEffect.MODIFY,
                    path=".codex-task.md",
                ),
                ToolCompletedEvent(
                    sequence=3,
                    turn_id="turn",
                    action_id="change",
                    status=RuntimeToolStatus.COMPLETED,
                    diff="+ task notes",
                ),
                TurnCompletedEvent(
                    sequence=4,
                    turn_id="turn",
                    status=RuntimeTurnStatus.FAILED,
                    error=exact_error,
                ),
            )
            for event in events:
                await self.event_sink.emit(event)
            return RunResult(
                run_id="turn",
                task_id="turn",
                status=RunStatus.FAILED,
                summary="Useful partial answer.",
                changed_files=(".codex-task.md",),
                terminal_reason="conversation_turn_failed",
                error=exact_error,
            )

    def factory(_request, approval_policy, event_sink):
        return FailedNativeRunner(
            approval_policy=approval_policy,
            event_sink=event_sink,
        ), FakeModel()

    app = RivumiApp(
        repository=tmp_path,
        config=CliConfig(runtime="codex-cli"),
        runner_factory=factory,
        runtimes=(("codex-cli", "Codex CLI"),),
        providers=(("ollama", "Ollama"),),
        initial_prompt="Research it",
    )

    async with app.run_test(size=(100, 35)) as pilot:
        await _wait_until(lambda: app._result is not None)
        await pilot.pause()

        messages = list(app.query(MessageBlock))
        assert [message.content for message in messages] == [
            "Research it",
            "Useful partial answer.",
        ]
        failures = [entry for entry in app.query(TimelineEntry) if entry.title == "Run failed"]
        assert len(failures) == 1
        assert exact_error in (failures[0].detail or "")
        assert ".codex-task.md" in (failures[0].detail or "")
        assert not any(entry.title == "Edited" for entry in app.query(TimelineEntry))
        assert str(app.query_one("#status", Static).content) == (
            f"Failed · {exact_error} · 1 file changed before failure"
        )


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[RunEvent] = []
        self.tool_started = asyncio.Event()

    async def emit(self, event: RunEvent) -> None:
        self.events.append(event)
        if event.event_type == "tool.started":
            self.tool_started.set()


async def test_cooperative_stop_waits_for_started_tool_completion(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    check = VerificationCommand(
        name="slow-check",
        argv=(sys.executable, "-c", "import time; time.sleep(0.4); print('done')"),
        timeout_seconds=2,
    )
    model = ScriptedModel(
        [ModelTurn(tool_calls=(ToolCall(name="run_check", arguments={"name": "slow-check"}),))]
    )
    sink = RecordingSink()
    runner = AgentRunner(
        TaskContract(
            repository=tiny_bug_repo,
            instruction="Run the bounded check.",
            allowed_paths=("**",),
            verification=(check,),
        ),
        model,
        tmp_path / "runs",
        allow_unsafe_local_exec=True,
        approval_policy=HeadlessApprovalPolicy(allow_modify=True, allow_execute=True),
        event_sink=sink,
    )

    run_task = asyncio.create_task(runner.run())
    await asyncio.wait_for(sink.tool_started.wait(), timeout=2)
    runner.request_cancel()
    result = await asyncio.wait_for(run_task, timeout=3)

    assert result.status == RunStatus.CANCELLED
    event_types = [event.event_type for event in sink.events]
    assert event_types.index("tool.completed") < event_types.index("run.cancelled")
    assert result.terminal_reason == "user_cancelled"


async def test_hard_task_cancel_is_deferred_until_started_tool_completes(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    check = VerificationCommand(
        name="slow-check",
        argv=(sys.executable, "-c", "import time; time.sleep(0.4); print('done')"),
        timeout_seconds=2,
    )
    model = ScriptedModel(
        [ModelTurn(tool_calls=(ToolCall(name="run_check", arguments={"name": "slow-check"}),))]
    )
    sink = RecordingSink()
    runner = AgentRunner(
        TaskContract(
            repository=tiny_bug_repo,
            instruction="Run the bounded check.",
            allowed_paths=("**",),
            verification=(check,),
        ),
        model,
        tmp_path / "runs",
        allow_unsafe_local_exec=True,
        approval_policy=HeadlessApprovalPolicy(allow_modify=True, allow_execute=True),
        event_sink=sink,
    )

    run_task = asyncio.create_task(runner.run())
    await asyncio.wait_for(sink.tool_started.wait(), timeout=2)
    await asyncio.sleep(0.1)
    run_task.cancel()
    result = await asyncio.wait_for(run_task, timeout=3)

    assert result.status == RunStatus.CANCELLED
    event_types = [event.event_type for event in sink.events]
    assert event_types.index("tool.completed") < event_types.index("run.cancelled")


async def test_outer_cancel_cleans_inflight_model_task(tiny_bug_repo: Path, tmp_path: Path) -> None:
    started = asyncio.Event()
    model_cancelled = asyncio.Event()

    class WaitingModel:
        provider_name = "waiting"
        model_id = "waiting"
        protocol = ModelProtocol.SCRIPTED
        capabilities = ModelCapabilities(
            tool_calling=True,
            streaming=False,
            structured_output=False,
        )

        async def complete(self, _messages, _tools):
            started.set()
            try:
                await asyncio.Future()
            finally:
                model_cancelled.set()

        async def aclose(self) -> None:
            return None

    runner = AgentRunner(
        TaskContract(
            repository=tiny_bug_repo,
            instruction="Wait for the model.",
            allowed_paths=("**",),
            verification=(VerificationCommand(name="diff", argv=("git", "diff", "--check")),),
        ),
        WaitingModel(),
        tmp_path / "runs",
        allow_unsafe_local_exec=True,
        approval_policy=HeadlessApprovalPolicy(allow_modify=True, allow_execute=True),
    )

    run_task = asyncio.create_task(runner.run())
    await asyncio.wait_for(started.wait(), timeout=2)
    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task
    await asyncio.wait_for(model_cancelled.wait(), timeout=1)


async def test_slash_rewind_forks_and_restores_prompt_without_submitting(
    tmp_path: Path,
) -> None:
    store = ConversationStore(tmp_path / "conversations", durable=False)
    requests = []

    def factory(request, approval_policy, event_sink):
        requests.append(request)
        return FakeRunner(approval_policy=approval_policy, event_sink=event_sink), None

    app = RivumiApp(
        repository=tmp_path,
        config=CliConfig(runtime="codex-cli"),
        runner_factory=factory,
        runtimes=(("codex-cli", "Codex CLI"),),
        providers=(("ollama", "Ollama"),),
        conversation_store=store,
    )

    async with app.run_test(size=(100, 30)) as pilot:
        task = app.query_one("#task", MessageComposer)
        task.load_text("remember alpha")
        app._submit_current_task()
        await _wait_until(lambda: len(requests) == 1 and not app._agent_running)
        parent_id = app._conversation_id
        assert parent_id is not None

        task.load_text("/rewind")
        await pilot.press("enter")
        await _wait_until(lambda: bool(app.query(InlineSelectorBlock)))
        selector = app.query_one(InlineSelectorBlock)
        assert selector.kind == "rewind"
        assert [option.value for option in selector.options] != []
        await pilot.press("enter")

        await _wait_until(lambda: not app.query(InlineSelectorBlock))
        assert app._conversation_id != parent_id
        assert task.text == "remember alpha"
        assert len(requests) == 1  # restored prompt must not auto-submit
        status = str(app.query_one("#status", Static).render())
        assert "Rewound" in status
        assert "prompt restored" in status

        branch_id = app._conversation_id
        persisted = (store.root / str(branch_id) / "events.jsonl").read_text()
        assert "user.message" not in persisted  # branch excludes selected turn
        parent_persisted = (store.root / str(parent_id) / "events.jsonl").read_text()
        assert "user.message" in parent_persisted

        await pilot.press("enter")
        await _wait_until(lambda: len(requests) == 2 and not app._agent_running)
        assert requests[1].instruction == "remember alpha"


async def test_idle_double_escape_opens_rewind_selector(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path / "conversations", durable=False)

    def factory(request, approval_policy, event_sink):
        return FakeRunner(approval_policy=approval_policy, event_sink=event_sink), None

    app = RivumiApp(
        repository=tmp_path,
        config=CliConfig(runtime="codex-cli"),
        runner_factory=factory,
        runtimes=(("codex-cli", "Codex CLI"),),
        providers=(("ollama", "Ollama"),),
        conversation_store=store,
    )

    async with app.run_test(size=(100, 30)) as pilot:
        task = app.query_one("#task", MessageComposer)
        task.load_text("first prompt")
        app._submit_current_task()
        await _wait_until(lambda: app._result is not None and not app._agent_running)

        task.load_text("")
        await pilot.press("escape")
        await pilot.press("escape")
        await _wait_until(lambda: bool(app.query(InlineSelectorBlock)))
        assert app.query_one(InlineSelectorBlock).kind == "rewind"


async def test_double_escape_without_conversation_shows_nothing_to_rewind(
    tmp_path: Path,
) -> None:
    def factory(request, approval_policy, event_sink):
        return FakeRunner(approval_policy=approval_policy, event_sink=event_sink), None

    app = RivumiApp(
        repository=tmp_path,
        config=CliConfig(provider="ollama", model="qwen3:4b"),
        runner_factory=factory,
        providers=(("ollama", "Ollama"),),
    )

    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one("#task", MessageComposer).focus()
        await pilot.press("escape")
        await pilot.press("escape")
        await pilot.pause()
        assert not app.query(InlineSelectorBlock)
        status = str(app.query_one("#status", Static).render())
        assert "Nothing to rewind" in status or "start a conversation first" in status


async def test_rewindable_prompts_ignore_active_and_failed_only_turns(
    tmp_path: Path,
) -> None:
    from rivumi.tui import _rewindable_prompts_from_events

    events = []
    # A completed turn plus a still-active turn without a terminal event.
    for kind, turn, text in (
        (ConversationEventKind.USER_MESSAGE, "a" * 32, "done prompt"),
        (ConversationEventKind.TURN_COMPLETED, "a" * 32, None),
        (ConversationEventKind.USER_MESSAGE, "b" * 32, "in flight"),
    ):
        events.append(
            ConversationEvent(
                conversation_id="0" * 32,
                sequence=len(events),
                event_type=kind,
                turn_id=turn,
                text=text,
            )
        )
    prompts = _rewindable_prompts_from_events(events)
    assert [label for _turn, label in prompts] == ["done prompt"]


async def test_final_transcript_exports_semantic_history(tmp_path: Path) -> None:
    def factory(request, approval_policy, event_sink):
        return FakeRunner(approval_policy=approval_policy, event_sink=event_sink), None

    app = RivumiApp(
        repository=tmp_path,
        config=CliConfig(provider="ollama", model="qwen3:4b"),
        runner_factory=factory,
        providers=(("ollama", "Ollama"),),
    )

    async with app.run_test(size=(100, 30)):
        task = app.query_one("#task", MessageComposer)
        task.load_text("export me")
        app._submit_current_task()
        await _wait_until(lambda: app._result is not None and not app._agent_running)

        exported = app.export_final_transcript()
        assert "You › export me" in exported
        assert "Assistant › Finished in the full-screen UI." in exported


async def test_cancelled_turn_leaves_useful_transcript(tmp_path: Path) -> None:
    started = asyncio.Event()
    cancel_requested = asyncio.Event()

    class BlockingRunner(FakeRunner):
        def request_cancel(self) -> None:
            cancel_requested.set()

        async def run(self) -> RunResult:
            started.set()
            await cancel_requested.wait()
            return RunResult(
                run_id="export-run",
                task_id="export-task",
                status=RunStatus.CANCELLED,
                summary="stopped safely",
                terminal_reason="user_cancelled",
            )

    app = RivumiApp(
        repository=tmp_path,
        config=CliConfig(provider="ollama", model="qwen3:4b"),
        runner_factory=lambda _request, approval_policy, event_sink: (
            BlockingRunner(approval_policy=approval_policy, event_sink=event_sink),
            FakeModel(),
        ),
        providers=(("ollama", "Ollama"),),
        initial_prompt="cancel me",
    )

    async with app.run_test(size=(100, 30)) as pilot:
        await asyncio.wait_for(started.wait(), timeout=1)
        await pilot.press("escape")
        await _wait_until(lambda: not app._agent_running)

        exported = app.export_final_transcript()
        assert "You › cancel me" in exported
        assert "Turn cancelled" in exported

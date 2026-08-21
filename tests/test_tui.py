from __future__ import annotations

import asyncio
import stat
import sys
from pathlib import Path

import pytest
from textual.widgets import Button

from coding_agent.approvals import (
    ApprovalDecision,
    ApprovalReason,
    ApprovalRequest,
    HeadlessApprovalPolicy,
    ToolEffect,
)
from coding_agent.cli_config import CliConfig, load_cli_config
from coding_agent.contracts import (
    ModelCapabilities,
    ModelProtocol,
    ModelTurn,
    RunResult,
    RunStatus,
    TaskContract,
    ToolCall,
    VerificationCommand,
)
from coding_agent.events import RunEvent
from coding_agent.loop import AgentRunner
from coding_agent.models import ScriptedModel
from coding_agent.tui import OnboardingModal, PcaApp, RunEventMessage


class FakeModel:
    async def aclose(self) -> None:
        return None


class FailingCloseModel:
    async def aclose(self) -> None:
        raise RuntimeError("close exploded")


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

    app = PcaApp(
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


async def test_onboarding_modal_saves_private_non_secret_config(
    tmp_path: Path, monkeypatch
) -> None:
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("PCA_CONFIG", str(config_path))
    app = PcaApp(
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
        await pilot.click("#continue")
        await _wait_until(config_path.exists)
        assert load_cli_config(config_path) == CliConfig(
            provider="ollama", model="qwen3:4b"
        )
        assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
        assert "api_key" not in config_path.read_text()


async def test_onboarding_without_local_models_uses_model_id_input(tmp_path: Path) -> None:
    app = PcaApp(
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
        assert app.screen.query_one("#model-id").display is True
        await pilot.press("escape")


async def test_tui_approval_modal_maps_once_decision(tmp_path: Path) -> None:
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

    app = PcaApp(
        repository=tmp_path,
        config=CliConfig(provider="ollama", model="qwen3:4b"),
        runner_factory=factory,
        providers=(("ollama", "Ollama local"),),
        initial_prompt="Edit the example.",
    )

    async with app.run_test(size=(100, 30)) as pilot:
        await _wait_until(lambda: bool(app.screen.query("#once")))
        button = app.screen.query_one("#once", Button)
        app.screen.choose(Button.Pressed(button))
        await pilot.pause()
        await _wait_until(lambda: app._result is not None)
        assert decision["value"] == ApprovalDecision.ALLOW_ONCE


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

    app = PcaApp(
        repository=tmp_path,
        config=CliConfig(provider="ollama", model="qwen3:4b"),
        runner_factory=factory,
        providers=(("ollama", "Ollama local"),),
        initial_prompt="Edit the example.",
    )

    async with app.run_test(size=(100, 30)) as pilot:
        await _wait_until(lambda: bool(app.screen.query("#cancel")))
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

    app = PcaApp(
        repository=tmp_path,
        config=CliConfig(provider="ollama", model="qwen3:4b"),
        runner_factory=factory,
        providers=(("ollama", "Ollama local"),),
    )

    async with app.run_test(size=(100, 30)) as pilot:
        task = app.query_one("#task")
        task.value = "First run"
        app._submit_current_task()
        await _wait_until(lambda: app._result is not None and not app._agent_running)
        task.value = "Second run"
        app._submit_current_task()
        await _wait_until(lambda: app.last_error is not None)
        assert app._result is None
        assert app.last_error == "Could not start run: second failed"
        await pilot.pause()


async def test_provider_close_failure_still_cleans_worker_state(tmp_path: Path) -> None:
    app = PcaApp(
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


async def test_ctrl_q_requests_cooperative_stop_instead_of_hard_quit(tmp_path: Path) -> None:
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

    app = PcaApp(
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
        await pilot.press("ctrl+q")
        await asyncio.wait_for(cancel_requested.wait(), timeout=1)
        await _wait_until(lambda: app._result is not None)
        assert app._result is not None
        assert app._result.status == RunStatus.CANCELLED


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

    app = PcaApp(
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
    app = PcaApp(
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
        [
            ModelTurn(
                tool_calls=(
                    ToolCall(name="run_check", arguments={"name": "slow-check"}),
                )
            )
        ]
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
        [
            ModelTurn(
                tool_calls=(
                    ToolCall(name="run_check", arguments={"name": "slow-check"}),
                )
            )
        ]
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


async def test_outer_cancel_cleans_inflight_model_task(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
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
            verification=(
                VerificationCommand(name="diff", argv=("git", "diff", "--check")),
            ),
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

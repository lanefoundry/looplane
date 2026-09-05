"""Deterministic service contracts for projection, attachment fences, and compatibility."""

from __future__ import annotations

import asyncio
import dataclasses
import subprocess
import sys
from pathlib import Path

import pytest
from textual.widgets import Static

from looplane.cli_config import CliConfig
from looplane.contracts import RunResult, RunStatus, Usage
from looplane.conversation import ConversationEventKind, ConversationStore
from looplane.conversation_runtime import (
    RuntimeTurnStatus,
    TextDeltaEvent,
    TurnCompletedEvent,
    TurnStartedEvent,
)
from looplane.events import RunEvent
from looplane.terminal.app import looplaneApp
from looplane.terminal.conversation_binding import ConversationBinding, TextualEventSink
from looplane.terminal.events import RunEventMessage
from looplane.terminal.projection import (
    AliasTool,
    MessageView,
    ProjectionContext,
    StreamAppend,
    TerminalProjection,
    ToolView,
)


@pytest.mark.parametrize("module", ["app", "projection", "conversation_binding"])
def test_canonical_import_has_no_facade_dependency(module: str) -> None:
    code = f"""
import importlib, sys
importlib.import_module('looplane.terminal.{module}')
for name in ('looplane.tui', 'looplane.cli', 'looplane.backends', 'looplane.loop'):
    assert name not in sys.modules, name
if '{module}' == 'projection':
    assert 'textual' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stderr


def test_stream_projection_flushes_incrementally_and_exports_once() -> None:
    now = 1.0
    projection = TerminalProjection(clock=lambda: now)
    context = ProjectionContext(mode="ask")
    projection.project(TurnStartedEvent(sequence=0, turn_id="runtime-turn"), context)
    commands = projection.project(
        TextDeltaEvent(sequence=1, turn_id="runtime-turn", text="first"), context
    )
    assert [c.content for c in commands if isinstance(c, MessageView)] == [""]
    assert [c.text for c in commands if isinstance(c, StreamAppend)] == ["first"]
    assert len(projection.reducer) == 0
    assert not any(
        isinstance(c, StreamAppend)
        for c in projection.project(
            TextDeltaEvent(sequence=2, turn_id="runtime-turn", text=" pending"), context
        )
    )
    now += 0.081
    commands = projection.project(
        TextDeltaEvent(sequence=3, turn_id="runtime-turn", text=" tail"), context
    )
    assert [c.text for c in commands if isinstance(c, StreamAppend)] == [" pending tail"]
    projection.project(
        TurnCompletedEvent(sequence=4, turn_id="runtime-turn", status=RuntimeTurnStatus.COMPLETED),
        context,
    )
    assert len(projection.reducer) == 1
    exported = projection.reducer.render(conversation_id=None, resume_command=None)
    assert exported.count("first pending tail") == 1


def test_tool_view_commands_are_immutable_and_reuse_one_action() -> None:
    projection = TerminalProjection()
    context = ProjectionContext()
    reused = RunEvent(
        event_type="verification.reused",
        run_id="run",
        task_id="task",
        sequence=0,
        data={
            "name": "lint",
            "tool_call_id": "manual",
            "ok": True,
            "exit_code": 0,
            "argv": ["ruff", "check"],
        },
    )
    projection.project(reused, context)
    commands = projection.project(
        RunEvent(
            event_type="tool.requested",
            run_id="run",
            task_id="task",
            sequence=1,
            data={"tool_call_id": "manual", "name": "run_check"},
        ),
        context,
    )
    assert AliasTool("verification:lint", "manual") in commands
    final = [c for c in commands if isinstance(c, ToolView)][-1]
    assert final.action_id == "manual"
    assert final.status == "completed"
    assert final.title == "Run ruff check"
    with pytest.raises(dataclasses.FrozenInstanceError):
        final.title = "mutated"


def test_result_fallback_observes_received_output_before_ui_delivery() -> None:
    projection = TerminalProjection()
    result = RunResult(
        run_id="run",
        task_id="task",
        status=RunStatus.COMPLETED,
        terminal_reason="completed",
        summary="already delivered",
        usage=Usage(),
    )
    commands = projection.finish_result(
        result, ProjectionContext(mode="ask", result=result, received_message=True)
    )
    assert not any(isinstance(c, MessageView) for c in commands)


class BarrierStore(ConversationStore):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.entered = asyncio.Event()
        self.release_append = asyncio.Event()

    async def append(self, lease, kind, **kwargs):
        if kind is ConversationEventKind.ASSISTANT_CHUNK:
            self.entered.set()
            await self.release_append.wait()
        return await super().append(lease, kind, **kwargs)


async def test_admitted_old_append_drains_before_lease_release_without_new_ui_write(
    tmp_path,
) -> None:
    store = BarrierStore(tmp_path)
    old = await store.create(runtime="codex-cli")
    _, old_lease = await store.resume(old.manifest.conversation_id)
    posted = []
    binding = ConversationBinding(store, lambda message: posted.append(message) or True)
    binding.conversation_id = old.manifest.conversation_id
    binding.lease = old_lease
    binding.turn_id = "a" * 32
    await binding.append(binding.write_target(), ConversationEventKind.USER_MESSAGE, text="hello")
    sink = TextualEventSink(binding, binding.generation)
    pending = asyncio.create_task(
        sink.emit(TextDeltaEvent(sequence=0, turn_id="different-runtime-turn", text="old text"))
    )
    await asyncio.wait_for(store.entered.wait(), 2)
    binding.release_conversation()
    assert old_lease.active  # admitted writer still owns its lease
    new = await store.create(runtime="codex-cli")
    _, new_lease = await store.resume(new.manifest.conversation_id)
    binding.conversation_id = new.manifest.conversation_id
    binding.lease = new_lease
    binding.turn_id = "b" * 32
    store.release_append.set()
    await pending
    assert not old_lease.active
    assert new_lease.active
    assert binding.has_chunk is False
    assert binding.received_messages == set()
    assert posted == []
    old_snapshot = await store.load(old.manifest.conversation_id)
    assert old_snapshot.events[-1].text == "old text"
    assert old_snapshot.events[-1].turn_id == "a" * 32
    new_snapshot = await store.load(new.manifest.conversation_id)
    assert not any(e.text == "old text" for e in new_snapshot.events)
    await binding.close()


async def test_old_sink_and_queued_envelope_are_rejected_after_attachment_change() -> None:
    posted = []
    binding = ConversationBinding(None, lambda message: posted.append(message) or True)
    sink = TextualEventSink(binding, 0)
    event = RunEvent(event_type="run.created", run_id="run", task_id="task", sequence=0)
    await sink.emit(event)
    assert binding.accepts(posted[0])
    binding.invalidate()
    assert not binding.accepts(posted[0])
    await sink.emit(event)
    assert len(posted) == 1
    # Original public constructors remain source-compatible for manually posted events.
    assert binding.accepts(RunEventMessage(event, 0))


async def test_final_event_remains_deliverable_until_next_turn() -> None:
    posted = []
    binding = ConversationBinding(None, lambda message: posted.append(message) or True)
    sink = TextualEventSink(binding, 0)
    await sink.emit(TextDeltaEvent(sequence=0, turn_id="turn", text="received"))
    assert binding.received_messages == {0}
    assert binding.accepts(posted[0])
    binding.generation += 1
    assert not binding.accepts(posted[0])


async def test_resource_close_retries_failures_and_keeps_new_attachment() -> None:
    entered = asyncio.Event()
    resume = asyncio.Event()
    order = []

    class Resource:
        def __init__(self, name, fail=False):
            self.name = name
            self.fail = fail

        async def aclose(self):
            order.append(self.name)
            if self.name == "slow":
                entered.set()
                await resume.wait()
            if self.fail:
                raise RuntimeError("close failed")

    binding = ConversationBinding(None, lambda _: True)
    failed = Resource("failed", fail=True)
    slow = Resource("slow")
    new = Resource("new")
    binding.resources.extend([failed, slow])
    closing = asyncio.create_task(binding.close_resources())
    await asyncio.wait_for(entered.wait(), 2)
    binding.remember_resource(new)
    resume.set()
    with pytest.raises(RuntimeError, match="close failed"):
        await closing
    assert order == ["slow", "failed"]
    assert binding.resources == [failed, new]
    failed.fail = False
    await binding.close_resources()
    assert binding.resources == []


def make_app(tmp_path: Path) -> looplaneApp:
    return looplaneApp(
        repository=tmp_path,
        config=CliConfig(provider="ollama", model="test"),
        runner_factory=lambda *_: (None, None),
        providers=(("ollama", "Ollama"),),
    )


async def test_deferred_callback_is_fenced_on_context_replacement(tmp_path) -> None:
    app = make_app(tmp_path)
    called = []
    async with app.run_test() as pilot:
        app._after_current_refresh(lambda: called.append("old"))
        app.conversation_binding.invalidate()
        await pilot.pause()
        assert called == []


async def test_statusline_out_of_order_same_turn_results_do_not_overwrite(tmp_path, monkeypatch):
    from looplane.terminal import app as app_module

    outputs = [asyncio.Future(), asyncio.Future()]
    launched = asyncio.Queue()

    class Process:
        def __init__(self, result):
            self.result = result

        async def communicate(self, _payload):
            return await self.result, b""

    async def spawn(*_args, **_kwargs):
        index = launched.qsize()
        process = Process(outputs[index])
        launched.put_nowait(process)
        return process

    monkeypatch.setattr(app_module.asyncio, "create_subprocess_shell", spawn)
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        app.config = app.config.model_copy(update={"statusline_command": "fake"})
        app._refresh_statusline()
        await pilot.pause()
        app._refresh_statusline()
        await pilot.pause()
        outputs[1].set_result(b"newer")
        await pilot.pause()
        outputs[0].set_result(b"older")
        await pilot.pause()
        assert str(app.query_one("#statusline", Static).content) == "newer"


async def test_facade_late_bound_copy_and_formatter_after_app_construction(tmp_path, monkeypatch):
    from looplane import tui

    app = tui.looplaneApp(
        repository=tmp_path,
        config=CliConfig(provider="ollama", model="test"),
        runner_factory=lambda *_: (None, None),
        providers=(("ollama", "Ollama"),),
    )
    copied = []
    monkeypatch.setattr(tui, "copy_with_native_command", lambda text: copied.append(text) or True)
    monkeypatch.setattr(tui, "format_token_count", lambda count: f"patched:{count}")
    async with app.run_test() as pilot:
        composer = app.query_one("#task", tui.MessageComposer)
        composer.set_text("copy me")
        composer.select_all()
        assert app._copy_selected_text()
        assert copied == ["copy me"]
        metrics = app.query_one("#metrics", tui.RuntimeMetrics)
        metrics.set_metrics(input_tokens=1234)
        assert "patched:1234" in str(metrics.content)
        await pilot.pause()


async def test_checkpoint_cancellation_drains_write_before_releasing_retired_lease(tmp_path):
    store = ConversationStore(tmp_path)
    snapshot = await store.create(runtime="codex-cli")
    _, lease = await store.resume(snapshot.manifest.conversation_id)
    binding = ConversationBinding(store, lambda _: True)
    binding.lease = lease
    entered = asyncio.Event()
    finish = asyncio.Event()
    written = []

    async def operation(captured_lease):
        entered.set()
        await finish.wait()
        assert captured_lease.active
        written.append(captured_lease.token)

    pending = asyncio.create_task(binding.checkpoint(binding.write_target(), operation))
    await asyncio.wait_for(entered.wait(), 2)
    pending.cancel()
    binding.release_conversation()
    await asyncio.sleep(0)
    assert lease.active
    assert not pending.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert written == [lease.token]
    assert not lease.active


async def test_binding_detach_resolves_pending_ui_approval_without_runtime_policy():
    from looplane.approvals import ApprovalDecision

    binding = ConversationBinding(None, lambda _: True)
    decision = asyncio.get_running_loop().create_future()
    binding.watch_approval(decision)
    binding.invalidate()
    assert decision.result() is ApprovalDecision.CANCEL
    binding.forget_approval(decision)
    await binding.close()

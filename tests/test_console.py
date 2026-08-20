from __future__ import annotations

from io import StringIO

import pytest

from coding_agent.console import CompositeEventSink, ConsoleEventSink, LiveEventProjection
from coding_agent.events import RunEvent


def event(sequence: int, event_type: str, **data: object) -> RunEvent:
    return RunEvent(
        run_id="run",
        task_id="task",
        sequence=sequence,
        event_type=event_type,
        data=data,
    )


@pytest.mark.asyncio
async def test_console_projection_renders_live_tool_trace() -> None:
    stream = StringIO()
    sink = ConsoleEventSink(stream)
    await sink.emit(event(0, "tool.started", name="read_file"))
    await sink.emit(event(1, "tool.completed", name="read_file", ok=True))
    assert stream.getvalue().splitlines() == [
        "[0] tool read_file",
        "[1] tool read_file: ok",
    ]


def test_projection_rejects_gaps_and_mixed_runs() -> None:
    projection = LiveEventProjection()
    projection.apply(event(0, "run.created"))
    with pytest.raises(ValueError, match="contiguous"):
        projection.apply(event(2, "run.completed"))


class RecordingSink:
    def __init__(self, *, fail: bool = False) -> None:
        self.events: list[RunEvent] = []
        self.fail = fail

    async def emit(self, value: RunEvent) -> None:
        self.events.append(value)
        if self.fail:
            raise OSError("display broke")


@pytest.mark.asyncio
async def test_composite_requires_durable_first_but_tolerates_display_failure() -> None:
    durable = RecordingSink()
    display = RecordingSink(fail=True)
    sink = CompositeEventSink((durable, display))
    value = event(0, "run.created")
    await sink.emit(value)
    assert durable.events == [value]
    assert display.events == [value]
    assert len(sink.secondary_errors) == 1


@pytest.mark.asyncio
async def test_composite_propagates_authoritative_sink_failure() -> None:
    sink = CompositeEventSink((RecordingSink(fail=True), RecordingSink()))
    with pytest.raises(OSError, match="display broke"):
        await sink.emit(event(0, "run.created"))

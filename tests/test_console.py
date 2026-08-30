from __future__ import annotations

from io import StringIO

import pytest

from looplane.console import CompositeEventSink, ConsoleEventSink, LiveEventProjection
from looplane.events import RunEvent


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


def test_projection_silently_skips_redelivered_event() -> None:
    """Same event_id replayed must not crash and must not double-project."""
    projection = LiveEventProjection()
    first = event(0, "tool.started", name="read_file")
    second = event(0, "tool.started", name="read_file")  # same seq, fresh event_id
    assert projection.apply(first) == ("[0] tool read_file",)
    # Different event_id, same sequence: treated as out-of-order, skip.
    assert projection.apply(second) == ()
    assert projection.out_of_order == 1

    # Now real redelivery (same event_id, same sequence): dedupe skip.
    third = RunEvent(
        run_id="run",
        task_id="task",
        sequence=0,
        event_type="tool.started",
        data={"name": "read_file"},
        event_id=first.event_id,
    )
    assert projection.apply(third) == ()
    assert projection.dropped_duplicates == 1


def test_projection_tolerates_out_of_order_replay() -> None:
    """Resume replay catching up to live must not raise; only the late ones are skipped."""
    projection = LiveEventProjection()
    projection.apply(event(0, "run.created"))
    projection.apply(event(1, "model.requested", step=1))
    # Replay arrives with sequence 0 (already seen): skip silently.
    replay = event(0, "tool.started", name="read_file")
    assert projection.apply(replay) == ()
    assert projection.out_of_order == 1
    # Live continues from 2.
    assert projection.apply(event(2, "run.completed")) == ("[2] run.completed",)
    assert projection.last_sequence == 2


def test_projection_still_rejects_run_id_change() -> None:
    projection = LiveEventProjection()
    projection.apply(
        RunEvent(run_id="run-a", task_id="t", sequence=0, event_type="run.created", data={})
    )
    with pytest.raises(ValueError, match="event stream changed run_id"):
        projection.apply(
            RunEvent(run_id="run-b", task_id="t", sequence=1, event_type="run.created", data={})
        )


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

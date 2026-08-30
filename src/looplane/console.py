"""Live event delivery and compact terminal projection."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol, TextIO, runtime_checkable

from looplane.events import EventWriter, RunEvent
from looplane.runtime import bounded_text


@runtime_checkable
class EventSink(Protocol):
    async def emit(self, event: RunEvent) -> None: ...


class JsonlEventSink:
    """Durable sink backed by the existing append-only event writer."""

    def __init__(self, writer: EventWriter) -> None:
        self.writer = writer

    async def emit(self, event: RunEvent) -> None:
        await self.writer.append(event)


class CompositeEventSink:
    """Fan out after the authoritative first sink succeeds.

    Secondary display failures are retained for diagnostics but never invalidate an event that
    was already persisted by the first sink.
    """

    def __init__(self, sinks: Iterable[EventSink]) -> None:
        self.sinks = tuple(sinks)
        if not self.sinks:
            raise ValueError("CompositeEventSink requires at least one sink")
        self.secondary_errors: list[Exception] = []

    async def emit(self, event: RunEvent) -> None:
        await self.sinks[0].emit(event)
        for sink in self.sinks[1:]:
            try:
                await sink.emit(event)
            except Exception as exc:  # display/telemetry must not corrupt durable state
                self.secondary_errors.append(exc)


class LiveEventProjection:
    """Validate event order and turn audit events into short human-readable lines."""

    def __init__(
        self,
        *,
        max_preview_bytes: int = 2_000,
        run_id: str | None = None,
        last_sequence: int = -1,
    ) -> None:
        self.max_preview_bytes = max_preview_bytes
        self.run_id = run_id
        self.last_sequence = last_sequence

    def apply(self, event: RunEvent) -> tuple[str, ...]:
        if self.run_id is None:
            self.run_id = event.run_id
        if event.run_id != self.run_id:
            raise ValueError("event stream changed run_id")
        if event.sequence != self.last_sequence + 1:
            raise ValueError(
                f"event sequence is not contiguous: expected {self.last_sequence + 1}, "
                f"got {event.sequence}"
            )
        self.last_sequence = event.sequence

        data = event.data
        kind = event.event_type
        if kind == "model.requested":
            return (f"[{event.sequence}] model step {data.get('step', '?')}",)
        if kind in {"tool.started", "tool.requested"}:
            return (f"[{event.sequence}] tool {data.get('name', '?')}",)
        if kind == "tool.completed":
            marker = "ok" if data.get("ok") else "failed"
            detail = data.get("error") or data.get("preview")
            lines = [f"[{event.sequence}] tool {data.get('name', '?')}: {marker}"]
            if detail:
                lines.append(bounded_text(str(detail), self.max_preview_bytes))
            return tuple(lines)
        if kind == "approval.requested":
            return (
                f"[{event.sequence}] approval: {data.get('effect', '?')} "
                f"({data.get('reason', '?')})",
            )
        if kind == "approval.resolved":
            return (f"[{event.sequence}] approval: {data.get('decision', '?')}",)
        if kind == "verification.completed":
            marker = "passed" if data.get("ok") else "failed"
            return (f"[{event.sequence}] check {data.get('name', '?')}: {marker}",)
        if kind.startswith("run.") or kind.startswith("session."):
            return (f"[{event.sequence}] {kind}",)
        return ()


class ConsoleEventSink:
    def __init__(self, stream: TextIO, projection: LiveEventProjection | None = None) -> None:
        self.stream = stream
        self.projection = projection or LiveEventProjection()

    async def emit(self, event: RunEvent) -> None:
        for line in self.projection.apply(event):
            self.stream.write(f"{line}\n")
        self.stream.flush()

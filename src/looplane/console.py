"""Live event delivery and compact terminal projection."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TextIO

from looplane.events import EventSink, EventWriter, RunEvent
from looplane.runtime import bounded_text


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
    """Project audit events into short human-readable lines for the TUI.

    The display layer is best-effort and must never terminate the UI on a
    benign redelivery (Textual replays the last event after widget rebind) or
    on out-of-order arrival (session resume replays history ahead of the live
    producer). The authoritative sequence is enforced by `EventWriter` and
    `JsonlEventSink`; this projection only renders lines and must stay alive
    even when the live stream hiccups.
    """

    def __init__(
        self,
        *,
        max_preview_bytes: int = 2_000,
        run_id: str | None = None,
        last_sequence: int = -1,
        seen_event_ids: set[str] | None = None,
    ) -> None:
        self.max_preview_bytes = max_preview_bytes
        self.run_id = run_id
        self.last_sequence = last_sequence
        self.seen_event_ids: set[str] = seen_event_ids or set()
        self.dropped_duplicates = 0
        self.out_of_order = 0

    def apply(self, event: RunEvent) -> tuple[str, ...]:
        if self.run_id is None:
            self.run_id = event.run_id
        if event.run_id != self.run_id:
            raise ValueError("event stream changed run_id")
        # Redelivery of the same event (Textual rebind, session resume replay)
        # is a benign replay; silently skip the duplicate projection.
        if event.event_id in self.seen_event_ids:
            self.dropped_duplicates += 1
            return ()
        self.seen_event_ids.add(event.event_id)
        # Out-of-order arrival (e.g. resume replay catches up to live) is
        # best-effort: skip the projection rather than corrupt the running
        # `last_sequence` cursor. The authoritative append-only log still has
        # the events in order; only the TUI line list drops the late one.
        if event.sequence <= self.last_sequence:
            self.out_of_order += 1
            return ()
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

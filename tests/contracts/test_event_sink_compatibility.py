"""A single native event-sink contract across stable public entry points."""

from typing import get_type_hints

from looplane import console, events, sdk
from looplane.conversation_controller import BackendTurnLimiter, ConversationEventSink, TurnLimiter
from looplane.external_agents import ExternalEventSink


def test_native_event_sink_has_one_owner_and_keeps_legacy_identity() -> None:
    assert console.EventSink is sdk.EventSink is events.EventSink
    assert get_type_hints(events.EventSink.emit)["event"] is events.RunEvent
    assert events.EventSink is not ConversationEventSink
    assert events.EventSink is not ExternalEventSink


def test_native_event_sink_remains_runtime_checkable() -> None:
    class Sink:
        async def emit(self, event: events.RunEvent) -> None:
            pass

    assert isinstance(Sink(), console.EventSink)
    assert isinstance(Sink(), sdk.EventSink)


def test_turn_limiter_retains_legacy_sdk_class_identity() -> None:
    assert sdk.TurnLimiter is sdk.BackendTurnLimiter is BackendTurnLimiter is TurnLimiter

"""Textual message envelopes for canonical runtime events."""

from __future__ import annotations

from textual.message import Message

from looplane.conversation_runtime import ConversationRuntimeEvent
from looplane.events import RunEvent
from looplane.external_agents import ExternalAgentEvent


class RunEventMessage(Message):
    """Deliver one immutable harness event to the UI reducer."""

    def __init__(self, event: RunEvent, generation: int) -> None:
        super().__init__()
        self.event = event
        self.generation = generation


class ExternalRunEventMessage(Message):
    """Deliver one bounded external-runtime event without pretending it is a core event."""

    def __init__(self, event: ExternalAgentEvent, generation: int) -> None:
        super().__init__()
        self.event = event
        self.generation = generation


class ConversationRuntimeEventMessage(Message):
    """Deliver one typed live-session event to the transcript reducer."""

    def __init__(self, event: ConversationRuntimeEvent, generation: int) -> None:
        super().__init__()
        self.event = event
        self.generation = generation

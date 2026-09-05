"""Compatibility exports for the former external-backend contract module."""

from looplane.external_agents import (
    ExternalAgentBackend,
    ExternalAgentEvent,
    ExternalAgentResult,
    ExternalAgentRunner,
    ExternalAgentTask,
    ExternalEventSink,
    ExternalRunStatus,
)

__all__ = [
    "ExternalAgentBackend",
    "ExternalAgentEvent",
    "ExternalAgentResult",
    "ExternalAgentRunner",
    "ExternalAgentTask",
    "ExternalEventSink",
    "ExternalRunStatus",
]

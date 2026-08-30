"""Public typed API for the looplane coding-agent harness.

Heavy, route-specific modules (provider SDKs, vendor backends, the loop, OAuth)
are intentionally NOT imported here so that loading any ``looplane.*`` submodule
(including ``looplane.cli`` for ``looplane --help``) never eagerly pulls in the
OpenAI/Anthropic SDKs or uvicorn. Import those directly from their submodules
when needed. See docs/startup-performance-playbook.md.
"""

from looplane.contracts import (
    Checkpoint,
    ConversationItem,
    CostBreakdown,
    Limits,
    Message,
    ModelCapabilities,
    ModelProtocol,
    ModelTurn,
    ModelUsageRecord,
    RunResult,
    RunStatus,
    TaskContract,
    ToolCall,
    ToolDefinition,
    ToolObservation,
    Usage,
    VerificationCommand,
    VerificationOutcome,
)
from looplane.events import EventWriter, RunEvent, atomic_write_json, write_json_atomic

__all__ = [
    "Checkpoint",
    "ConversationItem",
    "EventWriter",
    "Limits",
    "Message",
    "ModelCapabilities",
    "ModelProtocol",
    "ModelTurn",
    "ModelUsageRecord",
    "RunEvent",
    "RunResult",
    "RunStatus",
    "TaskContract",
    "ToolCall",
    "ToolDefinition",
    "ToolObservation",
    "Usage",
    "CostBreakdown",
    "VerificationCommand",
    "VerificationOutcome",
    "atomic_write_json",
    "write_json_atomic",
]

"""Provider-neutral approval contracts and policies for agent side effects."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Protocol, TextIO, runtime_checkable
from uuid import uuid4

from pydantic import Field, model_validator

from coding_agent.contracts import ContractModel, ToolCall, VerificationCommand


class ToolEffect(StrEnum):
    """The externally relevant effect of an agent action."""

    READ = "read"
    MODIFY = "modify"
    EXECUTE = "execute"


class ApprovalReason(StrEnum):
    MODEL_TOOL = "model_tool"
    FINAL_VERIFICATION = "final_verification"


class ApprovalDecision(StrEnum):
    ALLOW_ONCE = "allow_once"
    ALLOW_SESSION = "allow_session"
    DENY = "deny"
    CANCEL = "cancel"


class ApprovalRequest(ContractModel):
    """A bounded, auditable request made before one side effect."""

    request_id: str = Field(default_factory=lambda: uuid4().hex, min_length=1)
    run_id: str = Field(min_length=1)
    action_id: str = Field(min_length=1)
    effect: ToolEffect
    reason: ApprovalReason
    preview: str = Field(default="", max_length=16_000)
    tool_call: ToolCall | None = None
    command: VerificationCommand | None = None

    @model_validator(mode="after")
    def require_exactly_one_action(self) -> ApprovalRequest:
        if (self.tool_call is None) == (self.command is None):
            raise ValueError("approval request requires exactly one tool_call or command")
        if self.reason == ApprovalReason.FINAL_VERIFICATION and self.command is None:
            raise ValueError("final verification approval requires a command")
        return self


@runtime_checkable
class ApprovalPolicy(Protocol):
    async def decide(self, request: ApprovalRequest) -> ApprovalDecision: ...


ApprovalCallback = Callable[
    [ApprovalRequest], ApprovalDecision | Awaitable[ApprovalDecision]
]


class CallbackApprovalPolicy:
    """Adapt a sync or async application callback into an approval policy."""

    def __init__(self, callback: ApprovalCallback) -> None:
        self._callback = callback

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        value = self._callback(request)
        if inspect.isawaitable(value):
            value = await value
        return ApprovalDecision(value)


class HeadlessApprovalPolicy:
    """A deterministic policy that never reads stdin and therefore cannot hang CI."""

    def __init__(self, *, allow_modify: bool = True, allow_execute: bool = False) -> None:
        self.allow_modify = allow_modify
        self.allow_execute = allow_execute

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        allowed = {
            ToolEffect.READ: True,
            ToolEffect.MODIFY: self.allow_modify,
            ToolEffect.EXECUTE: self.allow_execute,
        }[request.effect]
        return ApprovalDecision.ALLOW_ONCE if allowed else ApprovalDecision.DENY


class TTYApprovalPolicy:
    """Small terminal prompt adapter with session-scoped effect grants."""

    def __init__(self, input_stream: TextIO, output_stream: TextIO) -> None:
        self._input = input_stream
        self._output = output_stream
        self._grants: set[ToolEffect] = set()

    @property
    def grants(self) -> frozenset[ToolEffect]:
        return frozenset(self._grants)

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        if request.effect == ToolEffect.READ or request.effect in self._grants:
            return ApprovalDecision.ALLOW_ONCE
        if not self._input.isatty():
            return ApprovalDecision.DENY

        self._output.write(
            f"\nApproval required: {request.effect.value} ({request.reason.value})\n"
        )
        if request.preview:
            self._output.write(f"{request.preview}\n")
        self._output.write("Allow? [y] once / [a] session / [n] deny / [c] cancel: ")
        self._output.flush()
        answer = self._input.readline().strip().lower()
        decision = {
            "y": ApprovalDecision.ALLOW_ONCE,
            "yes": ApprovalDecision.ALLOW_ONCE,
            "a": ApprovalDecision.ALLOW_SESSION,
            "always": ApprovalDecision.ALLOW_SESSION,
            "c": ApprovalDecision.CANCEL,
            "cancel": ApprovalDecision.CANCEL,
        }.get(answer, ApprovalDecision.DENY)
        if decision == ApprovalDecision.ALLOW_SESSION:
            self._grants.add(request.effect)
        return decision


TOOL_EFFECTS: dict[str, ToolEffect] = {
    "list_files": ToolEffect.READ,
    "read_file": ToolEffect.READ,
    "search_text": ToolEffect.READ,
    "git_diff": ToolEffect.READ,
    "apply_patch": ToolEffect.MODIFY,
    "run_check": ToolEffect.EXECUTE,
}


def effect_for_tool(tool_name: str) -> ToolEffect:
    """Fail closed when a newly added tool has no explicit effect classification."""

    try:
        return TOOL_EFFECTS[tool_name]
    except KeyError as exc:
        raise ValueError(f"tool has no approval effect classification: {tool_name!r}") from exc

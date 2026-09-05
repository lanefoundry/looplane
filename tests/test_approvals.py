from __future__ import annotations

from io import StringIO

import pytest
from pydantic import ValidationError

from looplane.approvals import (
    ApprovalDecision,
    ApprovalReason,
    ApprovalRequest,
    CallbackApprovalPolicy,
    HeadlessApprovalPolicy,
    ToolEffect,
    TTYApprovalPolicy,
    effect_for_tool,
)
from looplane.contracts import ToolCall, VerificationCommand


def request(effect: ToolEffect) -> ApprovalRequest:
    return ApprovalRequest(
        run_id="run",
        action_id="action",
        effect=effect,
        reason=ApprovalReason.MODEL_TOOL,
        tool_call=ToolCall(name="apply_patch"),
        preview="small diff",
    )


@pytest.mark.asyncio
async def test_headless_policy_is_deterministic_and_fail_closed_for_execution() -> None:
    policy = HeadlessApprovalPolicy()
    assert await policy.decide(request(ToolEffect.READ)) == ApprovalDecision.ALLOW_ONCE
    assert await policy.decide(request(ToolEffect.MODIFY)) == ApprovalDecision.ALLOW_ONCE
    assert await policy.decide(request(ToolEffect.MODIFY_EXECUTE)) == ApprovalDecision.DENY
    assert await policy.decide(request(ToolEffect.EXECUTE)) == ApprovalDecision.DENY


@pytest.mark.asyncio
async def test_callback_policy_accepts_async_callback() -> None:
    async def decide(_: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision.CANCEL

    assert await CallbackApprovalPolicy(decide).decide(request(ToolEffect.MODIFY)) == "cancel"


class TTYInput(StringIO):
    def isatty(self) -> bool:
        return True


@pytest.mark.asyncio
async def test_tty_session_grant_avoids_second_prompt() -> None:
    input_stream = TTYInput("a\n")
    output = StringIO()
    policy = TTYApprovalPolicy(input_stream, output)
    assert await policy.decide(request(ToolEffect.MODIFY)) == ApprovalDecision.ALLOW_SESSION
    assert await policy.decide(request(ToolEffect.MODIFY)) == ApprovalDecision.ALLOW_ONCE
    assert input_stream.tell() == 2
    assert policy.grants == {ToolEffect.MODIFY}


@pytest.mark.asyncio
async def test_tty_prompt_includes_policy_reason() -> None:
    input_stream = TTYInput("n\n")
    output = StringIO()
    request_with_reason = request(ToolEffect.EXECUTE).model_copy(
        update={"policy_reason": "suspicious command shape: compound shell command"}
    )

    assert (
        await TTYApprovalPolicy(input_stream, output).decide(request_with_reason)
        == ApprovalDecision.DENY
    )

    assert "Policy: suspicious command shape: compound shell command" in output.getvalue()


@pytest.mark.asyncio
async def test_non_tty_never_prompts() -> None:
    stream = StringIO("y\n")
    assert (
        await TTYApprovalPolicy(stream, StringIO()).decide(request(ToolEffect.EXECUTE))
        == ApprovalDecision.DENY
    )
    assert stream.tell() == 0


def test_request_requires_exactly_one_action() -> None:
    with pytest.raises(ValidationError):
        ApprovalRequest(
            run_id="run",
            action_id="action",
            effect=ToolEffect.EXECUTE,
            reason=ApprovalReason.FINAL_VERIFICATION,
            tool_call=ToolCall(name="run_check"),
            command=VerificationCommand(name="tests", argv=("pytest",)),
        )


def test_tool_effects_are_explicit_and_unknown_tools_fail_closed() -> None:
    assert effect_for_tool("create_file") == ToolEffect.MODIFY
    assert effect_for_tool("replace_text") == ToolEffect.MODIFY
    assert effect_for_tool("apply_patch") == ToolEffect.MODIFY
    assert effect_for_tool("tool_transaction") == ToolEffect.MODIFY_EXECUTE
    assert effect_for_tool("run_check") == ToolEffect.EXECUTE
    assert effect_for_tool("tool_program") == ToolEffect.READ
    with pytest.raises(ValueError, match="no approval effect"):
        effect_for_tool("future_network_tool")


def test_every_registered_tool_has_an_approval_effect_classification() -> None:
    from looplane.tools import ToolExecutor

    for definition in ToolExecutor._tool_definitions():
        effect_for_tool(definition.name)  # must not raise

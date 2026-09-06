"""Rule-based auto-thinking classifier for per-turn thinking level selection."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from looplane.contracts import ConversationItem, Message, ToolObservation

THINKING_BUDGETS: dict[str, int] = {
    "minimal": 1024,
    "low": 2048,
    "medium": 8192,
    "high": 16384,
    "xhigh": 32768,
    "max": 32768,
}

_HIGH_SIGNAL_PATTERN = re.compile(
    r"\b(fix|debug|why|explain|investigate|diagnose|analyze|analyse|root\s*cause)\b",
    re.IGNORECASE,
)

_CODE_FENCE_PATTERN = re.compile(r"```")

SHORT_PROMPT_THRESHOLD = 50
DEEP_LOOP_THRESHOLD = 10


@dataclass(frozen=True)
class ThinkingClassification:
    level: str
    reason: str
    budget_tokens: int | None


def classify_thinking(
    messages: Sequence[ConversationItem],
    *,
    step: int,
    turn_start_step: int,
) -> ThinkingClassification:
    """Select a thinking level for the current model call.

    Pure function: inspects the conversation state and returns a classification.
    """
    steps_in_turn = step - turn_start_step

    if steps_in_turn >= DEEP_LOOP_THRESHOLD:
        return ThinkingClassification("low", "deep loop", THINKING_BUDGETS["low"])

    if _is_tool_result_dominated(messages):
        return ThinkingClassification("low", "tool results", THINKING_BUDGETS["low"])

    user_content = _last_user_content(messages)

    if user_content is not None and len(user_content) < SHORT_PROMPT_THRESHOLD:
        if _HIGH_SIGNAL_PATTERN.search(user_content):
            return ThinkingClassification("high", "reasoning keyword", THINKING_BUDGETS["high"])
        return ThinkingClassification("low", "short prompt", THINKING_BUDGETS["low"])

    if user_content is not None and _HIGH_SIGNAL_PATTERN.search(user_content):
        return ThinkingClassification("high", "reasoning keyword", THINKING_BUDGETS["high"])

    if user_content is not None and (
        len(user_content) > 200 or _CODE_FENCE_PATTERN.search(user_content)
    ):
        return ThinkingClassification("medium", "complex prompt", THINKING_BUDGETS["medium"])

    return ThinkingClassification("medium", "default", THINKING_BUDGETS["medium"])


def _last_user_content(messages: Sequence[ConversationItem]) -> str | None:
    for item in reversed(messages):
        if isinstance(item, Message) and item.role == "user" and item.content:
            return item.content
    return None


def _is_tool_result_dominated(messages: Sequence[ConversationItem]) -> bool:
    """Check if the most recent sequence of items is predominantly tool results."""
    recent_total = 0
    recent_tool_results = 0
    for item in reversed(messages):
        if isinstance(item, Message) and item.role == "user":
            break
        recent_total += 1
        if isinstance(item, ToolObservation):
            recent_tool_results += 1
        if recent_total >= 8:
            break
    return recent_total >= 2 and recent_tool_results > recent_total // 2

from __future__ import annotations

import pytest

from looplane.agent.thinking_classifier import (
    THINKING_BUDGETS,
    classify_thinking,
)
from looplane.contracts import Message, ToolObservation


def _user(content: str) -> Message:
    return Message(role="user", content=content)


def _assistant(content: str) -> Message:
    return Message(role="assistant", content=content)


def _tool_result(name: str = "read_file") -> ToolObservation:
    return ToolObservation(tool_call_id="tc_1", name=name, ok=True, content="ok")


class TestClassifyThinking:
    def test_short_prompt_gets_low(self):
        result = classify_thinking(
            [_user("list files")],
            step=1,
            turn_start_step=0,
        )
        assert result.level == "low"
        assert result.reason == "short prompt"
        assert result.budget_tokens == THINKING_BUDGETS["low"]

    def test_short_prompt_with_reasoning_keyword_gets_high(self):
        result = classify_thinking(
            [_user("why is this broken")],
            step=1,
            turn_start_step=0,
        )
        assert result.level == "high"
        assert result.reason == "reasoning keyword"
        assert result.budget_tokens == THINKING_BUDGETS["high"]

    def test_long_prompt_with_fix_gets_high(self):
        result = classify_thinking(
            [_user("Please fix this function that handles authentication: " + "x" * 200)],
            step=1,
            turn_start_step=0,
        )
        assert result.level == "high"
        assert result.reason == "reasoning keyword"

    def test_long_prompt_with_code_gets_medium(self):
        result = classify_thinking(
            [_user("Here is some code:\n```python\ndef foo():\n    pass\n```\nRefactor it.")],
            step=1,
            turn_start_step=0,
        )
        assert result.level == "medium"
        assert result.reason == "complex prompt"
        assert result.budget_tokens == THINKING_BUDGETS["medium"]

    def test_deep_loop_gets_low(self):
        result = classify_thinking(
            [_user("Please implement a complex feature with many detailed requirements")],
            step=15,
            turn_start_step=0,
        )
        assert result.level == "low"
        assert result.reason == "deep loop"

    def test_tool_results_dominated_gets_low(self):
        messages = [
            _user("read these files"),
            _assistant(content="I'll read them."),
            _tool_result("read_file"),
            _tool_result("read_file"),
            _tool_result("read_file"),
        ]
        result = classify_thinking(messages, step=2, turn_start_step=0)
        assert result.level == "low"
        assert result.reason == "tool results"

    def test_default_medium(self):
        prompt = "a medium length prompt that has enough words to pass the threshold"
        result = classify_thinking(
            [_user(prompt)],
            step=1,
            turn_start_step=0,
        )
        assert result.level == "medium"
        assert result.reason == "default"

    def test_empty_messages_defaults_medium(self):
        result = classify_thinking([], step=1, turn_start_step=0)
        assert result.level == "medium"
        assert result.reason == "default"

    def test_debug_keyword_case_insensitive(self):
        result = classify_thinking(
            [_user("DEBUG this issue with the parser")],
            step=1,
            turn_start_step=0,
        )
        assert result.level == "high"
        assert result.reason == "reasoning keyword"

    def test_investigate_triggers_high(self):
        result = classify_thinking(
            [_user("investigate the memory leak in the worker pool")],
            step=1,
            turn_start_step=0,
        )
        assert result.level == "high"

    def test_deep_loop_overrides_reasoning_keyword(self):
        result = classify_thinking(
            [_user("debug this complex issue")],
            step=20,
            turn_start_step=5,
        )
        assert result.level == "low"
        assert result.reason == "deep loop"

    def test_classification_is_frozen(self):
        result = classify_thinking([_user("hello")], step=1, turn_start_step=0)
        with pytest.raises(AttributeError):
            result.level = "high"  # type: ignore[misc]

    def test_budget_tokens_match_sidecar_values(self):
        assert THINKING_BUDGETS == {
            "minimal": 1024,
            "low": 2048,
            "medium": 8192,
            "high": 16384,
            "xhigh": 32768,
            "max": 32768,
        }

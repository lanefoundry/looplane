"""Tests for leaked-thinking healing stream filter."""

from __future__ import annotations

import pytest

from looplane.agent.thinking_healing import (
    ThinkingStreamFilter,
    contains_thinking_fences,
    heal_thinking,
)


class TestThinkingStreamFilter:
    def test_no_fences_passthrough(self):
        f = ThinkingStreamFilter()
        assert f.process("hello world") == [("text", "hello world")]
        assert f.flush() == []

    def test_basic_think_tag(self):
        f = ThinkingStreamFilter()
        result = f.process("before<think>reasoning</think>after")
        assert result == [
            ("text", "before"),
            ("thinking", "reasoning"),
            ("text", "after"),
        ]

    def test_basic_thinking_tag(self):
        f = ThinkingStreamFilter()
        result = f.process("before<thinking>reasoning</thinking>after")
        assert result == [
            ("text", "before"),
            ("thinking", "reasoning"),
            ("text", "after"),
        ]

    def test_case_insensitive(self):
        f = ThinkingStreamFilter()
        result = f.process("a<THINK>reason</THINK>b")
        assert result == [("text", "a"), ("thinking", "reason"), ("text", "b")]

    def test_multiple_blocks(self):
        f = ThinkingStreamFilter()
        result = f.process("hello<think>thought1</think> middle <think>thought2</think>end")
        assert result == [
            ("text", "hello"),
            ("thinking", "thought1"),
            ("text", " middle "),
            ("thinking", "thought2"),
            ("text", "end"),
        ]

    def test_partial_open_tag_across_chunks(self):
        f = ThinkingStreamFilter()
        r1 = f.process("hello<thi")
        assert r1 == [("text", "hello")]
        r2 = f.process("nk>reasoning</think>done")
        assert r2 == [("thinking", "reasoning"), ("text", "done")]

    def test_partial_close_tag_across_chunks(self):
        f = ThinkingStreamFilter()
        r1 = f.process("<think>reasoning</thi")
        assert r1 == [("thinking", "reasoning")]
        r2 = f.process("nk>done")
        assert r2 == [("text", "done")]

    def test_partial_thinking_tag_across_chunks(self):
        f = ThinkingStreamFilter()
        r1 = f.process("text<think")
        assert r1 == [("text", "text")]
        r2 = f.process("ing>deep thought</thinking>end")
        assert r2 == [("thinking", "deep thought"), ("text", "end")]

    def test_unclosed_fence_flushed_as_thinking(self):
        f = ThinkingStreamFilter()
        r1 = f.process("<think>unclosed reasoning")
        assert r1 == []
        r2 = f.flush()
        assert r2 == [("thinking", "unclosed reasoning")]

    def test_flush_partial_tag_as_text(self):
        f = ThinkingStreamFilter()
        r1 = f.process("trailing<thi")
        assert r1 == [("text", "trailing")]
        r2 = f.flush()
        assert r2 == [("text", "<thi")]

    def test_empty_thinking_block(self):
        f = ThinkingStreamFilter()
        result = f.process("before<think></think>after")
        assert result == [("text", "before"), ("text", "after")]

    def test_thinking_only_no_visible_text(self):
        f = ThinkingStreamFilter()
        result = f.process("<think>all reasoning</think>")
        assert result == [("thinking", "all reasoning")]

    def test_char_by_char_streaming(self):
        f = ThinkingStreamFilter()
        text = "hi<think>r</think>x"
        all_segments = []
        for ch in text:
            all_segments.extend(f.process(ch))
        all_segments.extend(f.flush())
        combined_text = ""
        combined_thinking = ""
        for kind, content in all_segments:
            if kind == "text":
                combined_text += content
            else:
                combined_thinking += content
        assert combined_text == "hix"
        assert combined_thinking == "r"


class TestHealThinking:
    def test_no_fences(self):
        visible, thinking = heal_thinking("just normal text")
        assert visible == "just normal text"
        assert thinking == ""

    def test_single_fence(self):
        visible, thinking = heal_thinking("answer<think>let me reason about this</think> is 42")
        assert visible == "answer is 42"
        assert thinking == "let me reason about this"

    def test_multiple_fences(self):
        visible, thinking = heal_thinking("<think>first</think>visible<thinking>second</thinking>")
        assert visible == "visible"
        assert thinking == "firstsecond"

    def test_whitespace_stripped(self):
        visible, thinking = heal_thinking(" <think> reasoning </think> answer ")
        assert visible == "answer"
        assert thinking == "reasoning"

    def test_empty_content(self):
        visible, thinking = heal_thinking("")
        assert visible == ""
        assert thinking == ""


class TestContainsThinkingFences:
    def test_has_think(self):
        assert contains_thinking_fences("foo<think>bar</think>baz")

    def test_has_thinking(self):
        assert contains_thinking_fences("foo<thinking>bar</thinking>baz")

    def test_no_fences(self):
        assert not contains_thinking_fences("just plain text")

    @pytest.mark.parametrize("tag", ["<THINK>", "<Think>", "<THINKING>"])
    def test_case_insensitive(self, tag):
        assert contains_thinking_fences(f"foo{tag}bar")

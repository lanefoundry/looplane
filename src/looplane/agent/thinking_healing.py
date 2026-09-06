"""Detect and extract leaked thinking from non-Anthropic provider text output.

Some providers (DeepSeek, Qwen, local models) embed reasoning inside
``<think>…</think>`` or ``<thinking>…</thinking>`` fences in the visible
text stream instead of returning structured thinking blocks.  This module
provides a streaming-safe splitter that separates thinking from visible
text, so the runner can emit proper thinking events.
"""

from __future__ import annotations

import re
from typing import Literal

_OPEN_RE = re.compile(r"<(think|thinking)>", re.IGNORECASE)
_CLOSE_RE = re.compile(r"</(think|thinking)>", re.IGNORECASE)

Segment = tuple[Literal["text", "thinking"], str]


class ThinkingStreamFilter:
    """State-machine filter that splits leaked thinking fences from text.

    Call :meth:`process` with each incoming text chunk.  It returns a list
    of ``("text", ...)`` / ``("thinking", ...)`` segments.  At the end of
    the stream, call :meth:`flush` to drain any buffered partial tag or
    unclosed thinking block.
    """

    def __init__(self) -> None:
        self._in_thinking = False
        self._buffer = ""

    def process(self, text: str) -> list[Segment]:
        self._buffer += text
        return self._drain()

    def flush(self) -> list[Segment]:
        """Drain remaining buffer — unclosed fences are emitted as text."""
        result: list[Segment] = []
        if self._buffer:
            kind: Literal["text", "thinking"] = "thinking" if self._in_thinking else "text"
            result.append((kind, self._buffer))
            self._buffer = ""
            self._in_thinking = False
        return result

    def _drain(self) -> list[Segment]:
        result: list[Segment] = []
        while self._buffer:
            if self._in_thinking:
                match = _CLOSE_RE.search(self._buffer)
                if match:
                    thinking_text = self._buffer[: match.start()]
                    if thinking_text:
                        result.append(("thinking", thinking_text))
                    self._buffer = self._buffer[match.end() :]
                    self._in_thinking = False
                else:
                    split = self._split_partial_close()
                    if split > 0:
                        result.append(("thinking", self._buffer[:split]))
                        self._buffer = self._buffer[split:]
                    break
            else:
                match = _OPEN_RE.search(self._buffer)
                if match:
                    before = self._buffer[: match.start()]
                    if before:
                        result.append(("text", before))
                    self._buffer = self._buffer[match.end() :]
                    self._in_thinking = True
                else:
                    split = self._split_partial_open()
                    if split >= 0:
                        if split > 0:
                            result.append(("text", self._buffer[:split]))
                            self._buffer = self._buffer[split:]
                    else:
                        result.append(("text", self._buffer))
                        self._buffer = ""
                    break
        return result

    def _split_partial_open(self) -> int:
        """Return the start index of a trailing partial open tag, or -1."""
        return _partial_tag_start(self._buffer, "<think>", "<thinking>")

    def _split_partial_close(self) -> int:
        """Return the start index of a trailing partial close tag, or -1."""
        return _partial_tag_start(self._buffer, "</think>", "</thinking>")


def _partial_tag_start(text: str, *tags: str) -> int:
    """Index where a trailing partial match of one of *tags* begins, or -1."""
    best = -1
    for tag in tags:
        tag_lower = tag.lower()
        for length in range(1, len(tag)):
            suffix = tag_lower[:length]
            if text[-length:].lower() == suffix:
                pos = len(text) - length
                if best == -1 or pos < best:
                    best = pos
    return best


def heal_thinking(content: str) -> tuple[str, str]:
    """Extract leaked thinking from a complete model response.

    Returns ``(visible_text, thinking_text)`` where ``thinking_text`` is
    the concatenation of all thinking fence contents.  If no fences are
    found, ``thinking_text`` is empty and ``visible_text`` is the original
    content unchanged.
    """
    f = ThinkingStreamFilter()
    segments = f.process(content)
    segments.extend(f.flush())
    visible_parts: list[str] = []
    thinking_parts: list[str] = []
    for kind, text in segments:
        if kind == "thinking":
            thinking_parts.append(text)
        else:
            visible_parts.append(text)
    return "".join(visible_parts).strip(), "".join(thinking_parts).strip()


def contains_thinking_fences(content: str) -> bool:
    """Quick check whether content has any thinking fence tags."""
    return bool(_OPEN_RE.search(content))

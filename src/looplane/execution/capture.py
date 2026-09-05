from __future__ import annotations

import os
import select
import threading
from collections.abc import Callable, Iterator
from contextlib import suppress


def bounded_text(value: str, max_chars: int) -> str:
    """Bound UTF-8 output bytes while retaining useful content from both ends."""

    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    encoded = value.encode("utf-8")
    if len(encoded) <= max_chars:
        return value
    marker = f"\n... output truncated ({len(encoded) - max_chars} bytes omitted) ...\n"
    marker_bytes = marker.encode("utf-8")
    if len(marker_bytes) >= max_chars:
        return marker_bytes[:max_chars].decode("utf-8", errors="ignore")
    available = max_chars - len(marker_bytes)
    head = available // 2
    tail = available - head
    prefix = encoded[:head].decode("utf-8", errors="ignore")
    suffix = encoded[-tail:].decode("utf-8", errors="ignore") if tail else ""
    return f"{prefix}{marker}{suffix}"


class _BoundedCapture:
    """Drain a pipe fully while retaining only bounded head and tail bytes."""

    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        self._head_limit = max_bytes // 2
        self._tail_limit = max_bytes - self._head_limit
        self._head = bytearray()
        self._tail = bytearray()
        self.total_bytes = 0

    def add(self, chunk: bytes) -> None:
        self.total_bytes += len(chunk)
        remaining_head = self._head_limit - len(self._head)
        if remaining_head > 0:
            self._head.extend(chunk[:remaining_head])
            chunk = chunk[remaining_head:]
        if chunk and self._tail_limit:
            self._tail.extend(chunk)
            if len(self._tail) > self._tail_limit:
                del self._tail[: len(self._tail) - self._tail_limit]

    @property
    def truncated(self) -> bool:
        if self.total_bytes > self.max_bytes:
            return True
        # Malformed input expands when rendered as UTF-8 replacement characters.
        return len(bytes(self._head + self._tail).decode("utf-8", errors="replace").encode()) > (
            self.max_bytes
        )

    def text(self) -> str:
        if self.total_bytes <= self.max_bytes:
            return bounded_text(
                bytes(self._head + self._tail).decode("utf-8", errors="replace"), self.max_bytes
            )
        marker = f"\n... output truncated ({self.total_bytes - self.max_bytes} bytes omitted) ...\n"
        marker_bytes = marker.encode("utf-8")
        available = max(0, self.max_bytes - len(marker_bytes))
        head_size = available // 2
        tail_size = available - head_size
        if len(marker_bytes) >= self.max_bytes:
            return marker_bytes[: self.max_bytes].decode("ascii")
        head = _decode_utf8_bound(bytes(self._head[:head_size]), head_size)
        tail = (
            _decode_utf8_bound(bytes(self._tail[-tail_size:]), tail_size, tail=True)
            if tail_size
            else ""
        )
        return head + marker + tail


def _decode_utf8_bound(payload: bytes, limit: int, *, tail: bool = False) -> str:
    """Replace malformed input, then clip only on rendered UTF-8 boundaries."""

    if limit <= 0:
        return ""
    encoded = payload.decode("utf-8", errors="replace").encode("utf-8")
    clipped = encoded[-limit:] if tail else encoded[:limit]
    return clipped.decode("utf-8", errors="ignore")


def _pipe_chunks(pipe: object, stop_event: threading.Event | None) -> Iterator[bytes]:
    descriptor = pipe.fileno()  # type: ignore[attr-defined]
    if os.name == "posix":
        os.set_blocking(descriptor, False)
    while stop_event is None or not stop_event.is_set():
        if os.name == "posix":
            if not select.select([descriptor], [], [], 0.05)[0]:
                continue
            try:
                chunk = os.read(descriptor, 64 * 1024)
            except BlockingIOError:
                continue
        else:
            # Windows pipe cancellation needs native evidence; do not close a
            # BufferedReader from another thread while its read lock is held.
            chunk = pipe.read(64 * 1024)  # type: ignore[attr-defined]
        if not chunk or (stop_event is not None and stop_event.is_set()):
            return
        yield chunk


def _drain_pipe(
    pipe: object,
    capture: _BoundedCapture,
    *,
    stop_event: threading.Event | None = None,
) -> None:
    try:
        for chunk in _pipe_chunks(pipe, stop_event):
            capture.add(chunk)
    finally:
        pipe.close()  # type: ignore[attr-defined]


def _drain_stdout_lines(
    pipe: object,
    capture: _BoundedCapture,
    callback: Callable[[str, bool], None],
    max_line_bytes: int,
    *,
    stop_event: threading.Event | None = None,
) -> None:
    """Drain stdout while delivering complete, independently bounded lines.

    Callback exceptions are isolated. A slow callback applies pipe backpressure;
    the runner's deadline/cancellation ends the operation even if it never returns.
    After shutdown, queued bytes do not cause another callback invocation.
    """

    retained = bytearray()
    line_truncated = False

    def append(segment: bytes) -> None:
        nonlocal line_truncated
        remaining = max_line_bytes - len(retained)
        if remaining > 0:
            retained.extend(segment[:remaining])
        if len(segment) > remaining:
            line_truncated = True

    def emit() -> None:
        nonlocal line_truncated
        payload = bytes(retained)
        if payload.endswith(b"\r"):
            payload = payload[:-1]
        decoded_bytes = len(payload.decode("utf-8", errors="replace").encode("utf-8"))
        text = _decode_utf8_bound(payload, max_line_bytes)
        with suppress(Exception):
            if stop_event is None or not stop_event.is_set():
                callback(text, line_truncated or decoded_bytes > max_line_bytes)
        retained.clear()
        line_truncated = False

    try:
        for chunk in _pipe_chunks(pipe, stop_event):
            capture.add(chunk)
            start = 0
            while True:
                if stop_event is not None and stop_event.is_set():
                    return
                newline = chunk.find(b"\n", start)
                if newline < 0:
                    append(chunk[start:])
                    break
                append(chunk[start:newline])
                emit()
                start = newline + 1
        if retained or line_truncated:
            emit()
    finally:
        pipe.close()  # type: ignore[attr-defined]


def _stdin_chunks(value: str) -> Iterator[bytes]:
    for start in range(0, len(value), 16 * 1024):
        yield value[start : start + 16 * 1024].encode("utf-8")


def _validate_stdin(value: str | None, max_bytes: int) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        raise TypeError("stdin must be a string or None")
    if len(value) > max_bytes:
        raise ValueError(f"stdin exceeds {max_bytes} UTF-8 bytes")
    total = 0
    for chunk in _stdin_chunks(value):
        total += len(chunk)
        if total > max_bytes:
            raise ValueError(f"stdin exceeds {max_bytes} UTF-8 bytes")


def _write_stdin(
    pipe: object,
    value: str,
    *,
    stop_event: threading.Event | None = None,
) -> None:
    try:
        descriptor = pipe.fileno()  # type: ignore[attr-defined]
        if os.name == "posix":
            os.set_blocking(descriptor, False)
        for chunk in _stdin_chunks(value):
            pending = memoryview(chunk)
            while pending:
                if stop_event is not None and stop_event.is_set():
                    return
                if os.name == "posix":
                    if not select.select([], [descriptor], [], 0.05)[1]:
                        continue
                    try:
                        written = os.write(descriptor, pending)
                    except BlockingIOError:
                        continue
                else:
                    written = pipe.write(pending)  # type: ignore[attr-defined]
                pending = pending[written:]
        pipe.flush()  # type: ignore[attr-defined]
    except (BrokenPipeError, OSError):
        pass
    finally:
        pipe.close()  # type: ignore[attr-defined]

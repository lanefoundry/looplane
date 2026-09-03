"""Startup telemetry shared by CLI and conversation orchestration.

Enable with ``LOOPLANE_STARTUP_LOG=<path>``: the value is an explicit file
path to append spans to. Telemetry is disabled when unset, empty, or one of
``0``/``false``/``no``.

The tracer uses only the standard library, so importing it never pulls in the
heavy provider SDKs or vendor runtimes that the startup-performance work keeps
lazy.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from collections.abc import Iterator
from pathlib import Path

_T0 = time.perf_counter()
_T0_WALL = time.time()

_STARTUP_LOG_TARGET = os.environ.get("LOOPLANE_STARTUP_LOG")

_MAX_EVENTS = 32


class _StartupTracer:
    """Emit per-step startup timings when ``LOOPLANE_STARTUP_LOG`` is set.

    Cheap no-op when disabled: every call is a flag check plus a perf_counter
    read. Output is one JSON object per line appended to the target file.
    """

    def __init__(self, target: str | None) -> None:
        self.enabled = bool(target) and target not in {"0", "false", "no"}
        self._target = target
        self._event_count = 0
        if self.enabled:
            if not self._validate_target(target):
                self.enabled = False
                return
            self._emit_record(
                {"event": "process_entry", "elapsed_ms": 0.0, "t": _T0_WALL}
            )

    @contextlib.contextmanager
    def span(self, name: str) -> Iterator[None]:
        if not self.enabled:
            yield
            return
        start = time.perf_counter()
        try:
            yield
        finally:
            elapsed = (time.perf_counter() - _T0) * 1000.0
            duration = (time.perf_counter() - start) * 1000.0
            self._emit_record(
                {
                    "step": name,
                    "ms": round(duration, 2),
                    "elapsed_ms": round(elapsed, 2),
                    "t": time.time(),
                }
            )

    def mark(self, name: str) -> None:
        if not self.enabled:
            return
        elapsed = (time.perf_counter() - _T0) * 1000.0
        self._emit_record(
            {"event": name, "elapsed_ms": round(elapsed, 2), "t": time.time()}
        )

    @staticmethod
    def _validate_target(target: str | None) -> bool:
        if not target:
            return False
        try:
            p = Path(target)
            if p.is_symlink():
                return False
        except (OSError, ValueError):
            return False
        return True

    def _emit_record(self, record: dict[str, object]) -> None:
        if self._event_count >= _MAX_EVENTS:
            return
        self._event_count += 1
        line = json.dumps(record, separators=(",", ":"))
        try:
            fd = os.open(
                self._target,  # type: ignore[arg-type]
                os.O_WRONLY | os.O_APPEND | os.O_CREAT,
                0o600,
            )
            try:
                os.write(fd, (line + "\n").encode("utf-8"))
            finally:
                os.close(fd)
        except OSError:
            pass


_STARTUP = _StartupTracer(_STARTUP_LOG_TARGET)

"""Startup telemetry shared by CLI and conversation orchestration.

Enable with ``RIVUMI_STARTUP_LOG``: any truthy value except ``0``/``false``/``no``
turns tracing on. A target of ``1``/``stdout``/``-`` writes one JSON object per
span to stderr; any other value is treated as a file path to append spans to.

The tracer uses only the standard library, so importing it never pulls in the
heavy provider SDKs or vendor runtimes that the startup-performance work keeps
lazy.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from collections.abc import Iterator

_STARTUP_LOG_TARGET = os.environ.get("RIVUMI_STARTUP_LOG")


class _StartupTracer:
    """Emit per-step startup timings when ``RIVUMI_STARTUP_LOG`` is set.

    Cheap no-op when disabled: every span is a flag check plus a perf_counter
    read. Output is one JSON object per span, either to stderr (target
    ``"1"``/``"stdout"``/``"-"``) or appended to the file path given.
    """

    def __init__(self, target: str | None) -> None:
        self.enabled = bool(target) and target not in {"0", "false", "no"}
        self._target = target

    @contextlib.contextmanager
    def span(self, name: str) -> Iterator[None]:
        if not self.enabled:
            yield
            return
        start = time.perf_counter()
        try:
            yield
        finally:
            self._emit(name, (time.perf_counter() - start) * 1000.0)

    def _emit(self, name: str, ms: float) -> None:
        record = json.dumps(
            {"step": name, "ms": round(ms, 2), "t": time.time()},
            separators=(",", ":"),
        )
        target = self._target
        if target in {"1", "stdout", "-"}:
            print(record, file=sys.stderr, flush=True)
        else:
            try:
                with open(target, "a", encoding="utf-8") as handle:
                    handle.write(record + "\n")
            except OSError:
                pass


_STARTUP = _StartupTracer(_STARTUP_LOG_TARGET)

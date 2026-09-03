"""Tests for the startup telemetry tracer."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from looplane.startup_trace import _StartupTracer


def test_tracer_is_noop_when_disabled(tmp_path: Path) -> None:
    log = tmp_path / "startup.jsonl"
    tracer = _StartupTracer(None)
    assert tracer.enabled is False
    with tracer.span("config.load"):
        pass
    tracer.mark("test")
    assert not log.exists()


def test_tracer_is_noop_for_explicit_disable_values(tmp_path: Path) -> None:
    for value in ("0", "false", "no", ""):
        tracer = _StartupTracer(value)
        assert tracer.enabled is False


def test_tracer_emits_process_entry_first(tmp_path: Path) -> None:
    log = tmp_path / "startup.jsonl"
    tracer = _StartupTracer(str(log))
    assert tracer.enabled is True
    lines = log.read_text().splitlines()
    assert len(lines) == 1
    first = json.loads(lines[0])
    assert first["event"] == "process_entry"
    assert first["elapsed_ms"] == 0.0
    assert "t" in first


def test_tracer_mark_records_elapsed(tmp_path: Path) -> None:
    log = tmp_path / "startup.jsonl"
    tracer = _StartupTracer(str(log))
    tracer.mark("imports_done")
    tracer.mark("cli_routed")
    lines = log.read_text().splitlines()
    assert len(lines) == 3
    events = [json.loads(line) for line in lines]
    assert events[0]["event"] == "process_entry"
    assert events[1]["event"] == "imports_done"
    assert events[2]["event"] == "cli_routed"
    assert events[1]["elapsed_ms"] >= 0.0
    assert events[2]["elapsed_ms"] >= events[1]["elapsed_ms"]


def test_tracer_span_records_duration_and_elapsed(tmp_path: Path) -> None:
    log = tmp_path / "startup.jsonl"
    tracer = _StartupTracer(str(log))
    with tracer.span("config.load"):
        pass
    lines = log.read_text().splitlines()
    assert len(lines) == 2
    span = json.loads(lines[1])
    assert span["step"] == "config.load"
    assert "ms" in span
    assert "elapsed_ms" in span
    assert span["ms"] >= 0.0


def test_tracer_event_ordering_is_monotonic(tmp_path: Path) -> None:
    log = tmp_path / "startup.jsonl"
    tracer = _StartupTracer(str(log))
    for i in range(10):
        tracer.mark(f"step_{i}")
    lines = log.read_text().splitlines()
    events = [json.loads(line) for line in lines]
    elapsed_values = [e["elapsed_ms"] for e in events]
    assert elapsed_values == sorted(elapsed_values)


def test_tracer_bounded_output(tmp_path: Path) -> None:
    log = tmp_path / "startup.jsonl"
    tracer = _StartupTracer(str(log))
    for i in range(50):
        tracer.mark(f"step_{i}")
    lines = [line for line in log.read_text().splitlines() if line.strip()]
    from looplane.startup_trace import _MAX_EVENTS

    assert len(lines) == _MAX_EVENTS


def test_tracer_privacy_no_secrets(tmp_path: Path) -> None:
    log = tmp_path / "startup.jsonl"
    tracer = _StartupTracer(str(log))
    tracer.mark("test_step")
    with tracer.span("test_span"):
        pass
    content = log.read_text()
    for line in content.splitlines():
        record = json.loads(line)
        keys = set(record.keys())
        assert keys <= {"event", "step", "ms", "elapsed_ms", "t"}


def test_tracer_rejects_symlink_target(tmp_path: Path) -> None:
    real_file = tmp_path / "real.jsonl"
    real_file.touch()
    link = tmp_path / "link.jsonl"
    link.symlink_to(real_file)
    tracer = _StartupTracer(str(link))
    assert tracer.enabled is False


def test_tracer_private_file_permissions(tmp_path: Path) -> None:
    log = tmp_path / "startup.jsonl"
    tracer = _StartupTracer(str(log))
    assert tracer.enabled is True
    mode = stat.S_IMODE(os.stat(log).st_mode)
    assert mode == 0o600


def test_tracer_write_failure_does_not_raise(tmp_path: Path) -> None:
    bad_path = str(tmp_path / "nonexistent_dir" / "startup.jsonl")
    tracer = _StartupTracer(bad_path)
    tracer.mark("should_not_raise")


def test_tracer_file_path_only(tmp_path: Path) -> None:
    for special in ("1", "stdout", "-"):
        target = tmp_path / special
        tracer = _StartupTracer(str(target))
        assert tracer.enabled is True
        assert target.exists()

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rivumi.otel_export import export_run, run_to_otel_payload


def _write_run(run_dir: Path) -> None:
    run_dir.mkdir(parents=True)
    (run_dir / "session.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": "abc123",
                "task_id": "task-1",
                "provider_name": "anthropic",
                "model_id": "claude-test",
                "protocol": "anthropic-messages",
                "prompt_version": "m3-exact-edit-v3",
                "usage": {
                    "input_tokens": 1000,
                    "output_tokens": 200,
                    "cached_input_tokens": 400,
                    "reasoning_tokens": 50,
                },
                "active_wall_time_seconds": 12.0,
            }
        )
    )
    (run_dir / "result.json").write_text(
        json.dumps(
            {
                "run_id": "abc123",
                "task_id": "task-1",
                "status": "completed",
                "terminal_reason": "verified",
                "usage": {
                    "input_tokens": 1000,
                    "output_tokens": 200,
                },
            }
        )
    )


def test_run_to_otel_payload_maps_genai_attributes(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "abc123"
    _write_run(run_dir)

    payload = run_to_otel_payload(run_dir)
    span = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    attributes = {attr["key"]: attr["value"] for attr in span["attributes"]}

    assert attributes["gen_ai.system"] == {"stringValue": "anthropic"}
    assert attributes["gen_ai.request.model"] == {"stringValue": "claude-test"}
    assert attributes["gen_ai.usage.input_tokens"] == {"intValue": 1000}
    assert attributes["gen_ai.usage.output_tokens"] == {"intValue": 200}
    assert attributes["gen_ai.usage.cache_read_input_tokens"] == {"intValue": 400}
    assert attributes["gen_ai.usage.reasoning_tokens"] == {"intValue": 50}
    assert attributes["rivumi.status"] == {"stringValue": "completed"}
    assert span["status"]["code"] == "STATUS_CODE_OK"


def test_run_to_otel_payload_marks_failed_runs(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "abc123"
    _write_run(run_dir)
    result = json.loads((run_dir / "result.json").read_text())
    result["status"] = "failed"
    result["error"] = "Token budget exceeded."
    (run_dir / "result.json").write_text(json.dumps(result))

    span = run_to_otel_payload(run_dir)["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    attributes = {attr["key"]: attr["value"] for attr in span["attributes"]}
    assert span["status"]["code"] == "STATUS_CODE_ERROR"
    assert attributes["error.message"] == {"stringValue": "Token budget exceeded."}


def test_export_run_missing_artifacts_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        run_to_otel_payload(tmp_path)


def test_export_run_writes_file(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "abc123"
    _write_run(run_dir)
    output = tmp_path / "export.json"
    export_run(run_dir, output)
    assert json.loads(output.read_text())["resourceSpans"]

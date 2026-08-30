"""Export looplane run artifacts as OpenTelemetry GenAI-compatible OTLP-JSON.

Follows the GenAI semantic conventions for spans and token usage attributes so
exports drop into any OTLP-compatible collector (Grafana, Prometheus, vendor
endpoints). See docs/research/2026-08-25-agent-usage-packages.md for the
ecosystem rationale.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any

from looplane.secret_scan import redact_secrets, scan_text_for_secrets

_OTEL_VERSION = 1


def _attr(key: str, value: Any) -> dict[str, Any]:
    """One OTLP attribute, typed the way OTLP-JSON expects."""
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": value}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": str(value)}}


def _usage_attributes(usage: dict[str, Any]) -> list[dict[str, Any]]:
    attributes: list[dict[str, Any]] = []
    input_tokens = usage.get("input_tokens") or 0
    output_tokens = usage.get("output_tokens") or 0
    if input_tokens:
        attributes.append(_attr("gen_ai.usage.input_tokens", int(input_tokens)))
    if output_tokens:
        attributes.append(_attr("gen_ai.usage.output_tokens", int(output_tokens)))
    cached = usage.get("cached_input_tokens") or 0
    if cached:
        attributes.append(_attr("gen_ai.usage.cache_read_input_tokens", int(cached)))
    reasoning = usage.get("reasoning_tokens") or 0
    if reasoning:
        attributes.append(_attr("gen_ai.usage.reasoning_tokens", int(reasoning)))
    return attributes


def run_to_otel_payload(run_dir: str | Path) -> dict[str, Any]:
    """Build one OTLP-JSON ExportTraceServiceRequest for a single run."""

    directory = Path(run_dir)
    manifest: dict[str, Any] = {}
    result: dict[str, Any] = {}
    with contextlib.suppress(OSError, ValueError):
        manifest = json.loads((directory / "session.json").read_text())
    with contextlib.suppress(OSError, ValueError):
        result = json.loads((directory / "result.json").read_text())
    if not manifest and not result:
        raise FileNotFoundError(f"no session.json or result.json under {directory}")

    run_id = str(manifest.get("run_id") or result.get("run_id") or directory.name)
    model = str(manifest.get("model_id") or result.get("model") or "unknown")
    provider = str(manifest.get("provider_name") or "unknown")
    usage = manifest.get("usage") or result.get("usage") or {}
    wall_time = manifest.get("active_wall_time_seconds")

    attributes = [
        _attr("gen_ai.system", provider),
        _attr("gen_ai.request.model", model),
        _attr("gen_ai.operation.name", "chat"),
        _attr("looplane.run_id", run_id),
        _attr("looplane.prompt_version", manifest.get("prompt_version", "unknown")),
        *_usage_attributes(usage),
    ]
    status = str(result.get("status") or manifest.get("phase") or "unknown")
    attributes.append(_attr("looplane.status", status))
    if isinstance(wall_time, (int, float)):
        attributes.append(_attr("looplane.active_wall_time_seconds", float(wall_time)))
    error = result.get("error")
    if error:
        attributes.append(_attr("error.message", str(error)[:512]))

    span: dict[str, Any] = {
        "traceId": run_id,
        "spanId": run_id[-16:],
        "name": f"looplane.run {status}",
        "kind": "SPAN_KIND_INTERNAL",
        "startTimeUnixNano": 0,
        "endTimeUnixNano": 0,
        "attributes": attributes,
        "status": {"code": "STATUS_CODE_OK"},
    }
    if status == "failed":
        span["status"] = {"code": "STATUS_CODE_ERROR"}

    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        _attr("service.name", "looplane"),
                        _attr("telemetry.sdk.name", "looplane-otel-export"),
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "looplane.run"},
                        "spans": [span],
                    }
                ],
            }
        ]
    }


def export_run(run_dir: str | Path, output: Path | None = None) -> str:
    """Export one run as OTLP-JSON; returns the JSON string (writes to output if given)."""

    payload = json.dumps(run_to_otel_payload(run_dir), ensure_ascii=False, indent=2)
    payload = redact_secrets(payload)
    findings = scan_text_for_secrets(str(output), path="otel-export-path") if output else ()
    if findings:
        raise ValueError("OTel export output path looks like it contains a secret")
    if output is not None:
        output.write_text(payload + "\n")
    return payload

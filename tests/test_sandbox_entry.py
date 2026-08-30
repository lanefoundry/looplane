from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from looplane.approvals import ApprovalDecision, ApprovalReason, ApprovalRequest, ToolEffect
from looplane.contracts import ModelTurn, ToolCall
from looplane.events import RunEvent
from looplane.models import ScriptedModel
from looplane.sandbox_entry import (
    SandboxControlPlaneApprovalPolicy,
    SandboxControlPlaneEventSink,
    SandboxEntrypointError,
    _main,
    _read_and_remove_approval_token,
    _read_and_remove_event_token,
    _read_and_remove_run_token,
    run_sandbox_request,
)

PATCH = """\
diff --git a/src/example/calculator.py b/src/example/calculator.py
--- a/src/example/calculator.py
+++ b/src/example/calculator.py
@@ -1,2 +1,2 @@
 def add(left, right):
-    return left - right
+    return left + right
"""


def _workspace(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "workspace"
    source = root / "source"
    (source / "src/example").mkdir(parents=True)
    (source / "src/example/calculator.py").write_text(
        "def add(left, right):\n    return left - right\n",
        encoding="utf-8",
    )
    (source / "check.py").write_text(
        "from src.example.calculator import add\nassert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    request = root / "request.json"
    request.write_text(
        json.dumps(
            {
                "task_id": "sandbox-fixture",
                "instruction": "Fix addition.",
                "allowed_paths": ["src/**"],
                "verification": [
                    {
                        "name": "tests",
                        "argv": ["python3", "check.py"],
                        "timeout_seconds": 30,
                    }
                ],
                "limits": {"max_steps": 4, "wall_time_seconds": 60},
            }
        ),
        encoding="utf-8",
    )
    return root, request


@pytest.mark.asyncio
async def test_sandbox_entry_runs_existing_agent_and_bundles_artifacts(
    tmp_path: Path,
) -> None:
    root, request = _workspace(tmp_path)
    model = ScriptedModel(
        [
            ModelTurn(tool_calls=(ToolCall(name="apply_patch", arguments={"patch": PATCH}),)),
            ModelTurn(content="fixed and verified"),
        ]
    )

    response = await run_sandbox_request(request, workspace_root=root, model=model)

    assert response["ok"] is True, response
    assert response["result"]["terminal_reason"] == "verified"
    assert response["result"]["changed_files"] == ["src/example/calculator.py"]
    assert "return left + right" in response["artifacts"]["patch"]
    assert json.loads(response["artifacts"]["result"])["status"] == "completed"
    assert (
        not (root / "source/src/example/calculator.py")
        .read_text()
        .endswith("return left + right\n")
    )


@pytest.mark.asyncio
async def test_sandbox_entry_rejects_uploaded_git_metadata(tmp_path: Path) -> None:
    root, request = _workspace(tmp_path)
    (root / "source/.git").mkdir()

    with pytest.raises(SandboxEntrypointError, match="Git metadata"):
        await run_sandbox_request(
            request,
            workspace_root=root,
            model=ScriptedModel([ModelTurn(content="unused")]),
        )


@pytest.mark.asyncio
async def test_sandbox_entry_rejects_reused_run_directory(tmp_path: Path) -> None:
    root, request = _workspace(tmp_path)
    (root / "runs").mkdir()

    with pytest.raises(SandboxEntrypointError, match="already exists"):
        await run_sandbox_request(
            request,
            workspace_root=root,
            model=ScriptedModel([ModelTurn(content="unused")]),
        )


@pytest.mark.asyncio
async def test_sandbox_entry_rejects_oversized_request(tmp_path: Path) -> None:
    root, request = _workspace(tmp_path)
    request.write_bytes(b"{" + (b" " * 256_000) + b"}")

    with pytest.raises(SandboxEntrypointError, match="request exceeds"):
        await run_sandbox_request(
            request,
            workspace_root=root,
            model=ScriptedModel([ModelTurn(content="unused")]),
        )


def test_run_capability_is_owner_only_and_consumed_once(tmp_path: Path) -> None:
    token_path = tmp_path / ".looplane-run-token"
    token_path.write_text("signed-run-capability", encoding="utf-8")
    token_path.chmod(0o600)

    assert _read_and_remove_run_token(tmp_path) == "signed-run-capability"
    assert not token_path.exists()
    with pytest.raises(SandboxEntrypointError, match="unavailable"):
        _read_and_remove_run_token(tmp_path)


def test_event_capability_is_owner_only_and_consumed_once(tmp_path: Path) -> None:
    token_path = tmp_path / ".looplane-event-token"
    token_path.write_text("signed-event-capability", encoding="utf-8")
    token_path.chmod(0o600)

    assert _read_and_remove_event_token(tmp_path) == "signed-event-capability"
    assert not token_path.exists()
    with pytest.raises(SandboxEntrypointError, match="unavailable"):
        _read_and_remove_event_token(tmp_path)


def test_approval_capability_is_owner_only_and_consumed_once(tmp_path: Path) -> None:
    token_path = tmp_path / ".looplane-approval-token"
    token_path.write_text("signed-approval-capability", encoding="utf-8")
    token_path.chmod(0o600)

    assert _read_and_remove_approval_token(tmp_path) == "signed-approval-capability"
    assert not token_path.exists()
    with pytest.raises(SandboxEntrypointError, match="unavailable"):
        _read_and_remove_approval_token(tmp_path)


def test_run_capability_rejects_loose_permissions(tmp_path: Path) -> None:
    token_path = tmp_path / ".looplane-run-token"
    token_path.write_text("signed-run-capability", encoding="utf-8")
    token_path.chmod(0o644)

    with pytest.raises(SandboxEntrypointError, match="unsafe metadata"):
        _read_and_remove_run_token(tmp_path)
    assert token_path.read_text(encoding="utf-8") == "signed-run-capability"


def test_event_capability_rejects_loose_permissions(tmp_path: Path) -> None:
    token_path = tmp_path / ".looplane-event-token"
    token_path.write_text("signed-event-capability", encoding="utf-8")
    token_path.chmod(0o644)

    with pytest.raises(SandboxEntrypointError, match="unsafe metadata"):
        _read_and_remove_event_token(tmp_path)
    assert token_path.read_text(encoding="utf-8") == "signed-event-capability"


@pytest.mark.asyncio
async def test_control_plane_event_sink_posts_jsonl_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    class FakeResponse:
        status_code = 200

    class FakeAsyncClient:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc_info) -> None:
            return None

        async def post(self, url: str, *, headers: dict[str, str], json: dict[str, object]):
            calls.append({"url": url, "headers": headers, "json": json, "timeout": self.timeout})
            return FakeResponse()

    monkeypatch.setattr("looplane.sandbox_entry.httpx.AsyncClient", FakeAsyncClient)
    sink = SandboxControlPlaneEventSink(
        base_url="https://control.example/internal/v1",
        run_id="task-1",
        token="run-token",
    )

    await sink.emit(
        RunEvent(event_type="run.created", run_id="agent-run", task_id="task-1", sequence=0)
    )

    assert len(calls) == 1
    assert calls[0]["url"] == "https://control.example/internal/v1/runs/task-1/events"
    assert calls[0]["headers"] == {
        "authorization": "Bearer run-token",
        "content-type": "application/json",
    }
    assert calls[0]["timeout"] == 10.0
    body = calls[0]["json"]
    assert isinstance(body, dict)
    lines = body["lines"]
    assert isinstance(lines, list)
    line = lines[0]
    assert isinstance(line, str)
    assert line.endswith("\n")
    event = json.loads(line)
    assert event["task_id"] == "task-1"
    assert event["run_id"] == "agent-run"


@pytest.mark.asyncio
async def test_control_plane_approval_policy_polls_until_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    class FakeAsyncClient:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout
            self.responses = [
                httpx.Response(202, json={"status": "pending", "requestId": "approval-1"}),
                httpx.Response(
                    200,
                    json={
                        "status": "decided",
                        "requestId": "approval-1",
                        "decision": "allow_once",
                    },
                ),
            ]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc_info) -> None:
            return None

        async def get(self, url: str, *, headers: dict[str, str]):
            calls.append({"url": url, "headers": headers, "timeout": self.timeout})
            return self.responses.pop(0)

    monkeypatch.setattr("looplane.sandbox_entry.asyncio.sleep", fake_sleep)
    monkeypatch.setattr("looplane.sandbox_entry.httpx.AsyncClient", FakeAsyncClient)
    policy = SandboxControlPlaneApprovalPolicy(
        base_url="https://control.example/internal/v1",
        run_id="task-1",
        token="approval-token",
        timeout_seconds=5.0,
        poll_interval_seconds=0.5,
    )

    decision = await policy.decide(
        ApprovalRequest(
            request_id="approval-1",
            run_id="agent-run",
            action_id="action-1",
            effect=ToolEffect.EXECUTE,
            reason=ApprovalReason.MODEL_TOOL,
            command={"name": "tests", "argv": ("python3", "-m", "pytest", "-q")},
        )
    )

    assert decision is ApprovalDecision.ALLOW_ONCE
    assert sleeps == [0.5]
    assert calls == [
        {
            "url": "https://control.example/internal/v1/runs/task-1/approvals/approval-1",
            "headers": {"authorization": "Bearer approval-token"},
            "timeout": 10.0,
        },
        {
            "url": "https://control.example/internal/v1/runs/task-1/approvals/approval-1",
            "headers": {"authorization": "Bearer approval-token"},
            "timeout": 10.0,
        },
    ]


@pytest.mark.asyncio
async def test_main_returns_only_a_bounded_entrypoint_failure_code(tmp_path: Path) -> None:
    response_path = tmp_path / "response.json"

    async def fail_entrypoint(_request: str) -> dict[str, object]:
        raise SandboxEntrypointError("sensitive internal detail")

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            "looplane.sandbox_entry.Path",
            lambda value: response_path if value == "/workspace/response.json" else Path(value),
        )
        monkeypatch.setattr("looplane.sandbox_entry.run_sandbox_request", fail_entrypoint)
        exit_code = await _main(["sandbox_entry", "request.json"])

    assert exit_code == 1
    assert json.loads(response_path.read_text(encoding="utf-8")) == {
        "ok": False,
        "error": "sandbox_entrypoint_failed",
    }

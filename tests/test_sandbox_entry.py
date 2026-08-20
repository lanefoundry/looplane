from __future__ import annotations

import json
from pathlib import Path

import pytest

from coding_agent.contracts import ModelTurn, ToolCall
from coding_agent.models import ScriptedModel
from coding_agent.sandbox_entry import (
    SandboxEntrypointError,
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
            ModelTurn(
                tool_calls=(ToolCall(name="apply_patch", arguments={"patch": PATCH}),)
            ),
            ModelTurn(content="fixed and verified"),
        ]
    )

    response = await run_sandbox_request(request, workspace_root=root, model=model)

    assert response["ok"] is True, response
    assert response["result"]["terminal_reason"] == "verified"
    assert response["result"]["changed_files"] == ["src/example/calculator.py"]
    assert "return left + right" in response["artifacts"]["patch"]
    assert json.loads(response["artifacts"]["result"])["status"] == "completed"
    assert not (root / "source/src/example/calculator.py").read_text().endswith(
        "return left + right\n"
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
    token_path = tmp_path / ".pca-run-token"
    token_path.write_text("signed-run-capability", encoding="utf-8")
    token_path.chmod(0o600)

    assert _read_and_remove_run_token(tmp_path) == "signed-run-capability"
    assert not token_path.exists()
    with pytest.raises(SandboxEntrypointError, match="unavailable"):
        _read_and_remove_run_token(tmp_path)


def test_run_capability_rejects_loose_permissions(tmp_path: Path) -> None:
    token_path = tmp_path / ".pca-run-token"
    token_path.write_text("signed-run-capability", encoding="utf-8")
    token_path.chmod(0o644)

    with pytest.raises(SandboxEntrypointError, match="unsafe metadata"):
        _read_and_remove_run_token(tmp_path)
    assert token_path.read_text(encoding="utf-8") == "signed-run-capability"

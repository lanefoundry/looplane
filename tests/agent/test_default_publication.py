"""Exercise normal-session publication with a deterministic sandbox launcher spy.

Git operations are real and use a local bare remote. The launcher spy only
records sandbox requests; it does not provide or assert OS-level isolation.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from looplane.agent.runner import AgentRunner
from looplane.approvals import HeadlessApprovalPolicy
from looplane.contracts import ModelTurn, TaskContract, ToolCall, VerificationCommand
from looplane.execution import local_process
from looplane.models import ScriptedModel
from looplane.sandbox.policy import CommandSandbox


@pytest.mark.asyncio
async def test_default_session_publishes_and_preserves_verification_sandbox(
    tiny_bug_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from conftest import run_git

    launches: list[tuple[tuple[str, ...], Path, CommandSandbox | None]] = []

    def record_launcher(argv, *, cwd, sandbox):
        launches.append((tuple(argv), Path(cwd), sandbox))
        return tuple(argv)

    monkeypatch.setattr(local_process, "sandboxed_command_argv", record_launcher)
    remote = tmp_path / "published.git"
    run_git(tmp_path, "init", "--bare", str(remote))
    run_git(tiny_bug_repo, "remote", "add", "origin", str(remote))
    original_head = run_git(tiny_bug_repo, "rev-parse", "HEAD")
    relative_path = "src/tiny_python_bug/calculator.py"
    original_content = (tiny_bug_repo / relative_path).read_text()
    extra_read_root = tmp_path / "runtime"
    extra_read_root.mkdir()
    check_script = (
        "from pathlib import Path; assert 'return left + right' in Path("
        + repr(relative_path)
        + ").read_text()"
    )
    task = TaskContract(
        repository=tiny_bug_repo,
        instruction="Fix add and push to review/default-session",
        allowed_paths=("src/**",),
        verification=(
            VerificationCommand(name="source", argv=(sys.executable, "-c", check_script)),
        ),
    )
    model = ScriptedModel(
        [
            ModelTurn(tool_calls=(ToolCall(name="read_file", arguments={"path": relative_path}),)),
            ModelTurn(
                tool_calls=(
                    ToolCall(
                        name="replace_text",
                        arguments={
                            "path": relative_path,
                            "old_text": "return left - right",
                            "new_text": "return left + right",
                        },
                    ),
                )
            ),
            ModelTurn(tool_calls=(ToolCall(name="run_check", arguments={"name": "source"}),)),
            ModelTurn(
                tool_calls=(
                    ToolCall(
                        name="publish_changes", arguments={"branch": "review/default-session"}
                    ),
                )
            ),
            ModelTurn(content="Published the fix."),
        ]
    )
    runner = AgentRunner(
        task,
        model,
        tmp_path / "runs",
        allow_unsafe_local_exec=False,
        sandbox_checks=True,
        sandbox_backend="landlock",
        sandbox_read_roots=(extra_read_root,),
        # A supplied policy exercises the normal interactive approval path.
        approval_policy=HeadlessApprovalPolicy(allow_execute=True),
    )
    result = await runner.run()
    events = [
        json.loads(line) for line in Path(result.artifacts["events"]).read_text().splitlines()
    ]
    publication = next(
        event
        for event in events
        if event["event_type"] == "tool.completed" and event["data"]["name"] == "publish_changes"
    )
    assert publication["data"]["ok"], publication
    published = json.loads(publication["data"]["preview"])
    assert published["checks"] == ["source"]
    assert run_git(remote, "rev-parse", "refs/heads/review/default-session") == published["head"]
    assert run_git(remote, "show", f"review/default-session:{relative_path}") == (
        original_content.replace("return left - right", "return left + right").strip()
    )
    assert run_git(tiny_bug_repo, "rev-parse", "HEAD") == original_head
    assert (tiny_bug_repo / relative_path).read_text() == original_content
    assert run_git(tiny_bug_repo, "status", "--porcelain") == ""

    # Both the requested workspace check and publication verification must ask
    # the process boundary for a sandbox, including the configured read roots.
    checks = [
        (cwd, sandbox) for argv, cwd, sandbox in launches if argv[-2:] == ("-c", check_script)
    ]
    assert len(checks) >= 2
    publication_checks = [(cwd, sandbox) for cwd, sandbox in checks if cwd.name == "checkout"]
    workspace_checks = [(cwd, sandbox) for cwd, sandbox in checks if cwd.name != "checkout"]
    assert publication_checks
    assert workspace_checks
    for cwd, sandbox in checks:
        assert sandbox is not None, checks
        assert sandbox.profile == "verification"
        assert sandbox.backend == "landlock"
        assert extra_read_root.resolve() in sandbox.read_roots
        assert cwd.resolve() in sandbox.read_roots
    pushes = [(argv, sandbox) for argv, _, sandbox in launches if "push" in argv]
    assert len(pushes) == 1
    assert pushes[0][1] is None

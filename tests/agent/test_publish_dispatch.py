from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from looplane.agent import publish_dispatch, tool_scheduler
from looplane.agent.ports import PreparedToolCall
from looplane.agent.runner import AgentRunner
from looplane.approvals import ApprovalDecision, ToolEffect, effect_for_tool_definition
from looplane.contracts import TaskContract, ToolCall, ToolObservation, VerificationCommand
from looplane.tooling.types import ReviewablePatch


def test_publication_is_never_read_only() -> None:
    definition = publish_dispatch.publish_changes_definition()
    call = ToolCall(name="publish_changes", arguments={"branch": "review"})
    assert not definition.read_only
    assert not definition.concurrency_safe
    misleading = definition.model_copy(update={"read_only": True, "concurrency_safe": True})
    assert effect_for_tool_definition(call.name, misleading) is ToolEffect.EXECUTE
    assert not tool_scheduler.can_execute_concurrently(call, (misleading,))


@pytest.mark.parametrize(
    "changes",
    [
        {"_enable_host_publication": False},
        {"_forked_messages": ()},
    ],
)
def test_publication_hidden_outside_host_top_level(changes: dict[str, object]) -> None:
    runner = object.__new__(AgentRunner)
    runner.allow_unsafe_local_exec = True
    runner._sandbox_checks = False
    runner._enable_host_publication = True
    runner._enable_subagent_dispatch = True
    runner._forked_messages = None
    runner._executor = SimpleNamespace(definitions=())
    runner._skills = ()
    for name, value in changes.items():
        setattr(runner, name, value)
    assert "publish_changes" not in {d.name for d in runner._provider_tool_definitions()}


def test_normal_sandboxed_session_exposes_publication_without_extra_flags() -> None:
    runner = object.__new__(AgentRunner)
    runner.allow_unsafe_local_exec = False
    runner._sandbox_checks = True
    runner._enable_host_publication = True
    runner._enable_subagent_dispatch = False
    runner._forked_messages = None
    runner._executor = SimpleNamespace(definitions=())
    runner._skills = ()
    assert "publish_changes" in {d.name for d in runner._provider_tool_definitions()}


def test_dispatch_uses_host_context_and_rejects_model_paths(monkeypatch, tmp_path: Path) -> None:
    @dataclass
    class Result:
        head: str = "abc"
        branch: str = "review"
        remote: str = "origin"
        checks: tuple[str, ...] = ()

    publish = Mock(return_value=Result())
    monkeypatch.setattr(publish_dispatch, "publish_changes", publish)
    context = dict(
        source_repo=tmp_path / "source",
        workspace=tmp_path / "workspace",
        base_sha="base",
        verification=(),
        patch="trusted patch",
        timeout_seconds=12.0,
        sandbox_checks=True,
        sandbox_profile="verification",
        sandbox_backend="landlock",
        sandbox_read_roots=(tmp_path / "runtime",),
    )
    call = ToolCall(name="publish_changes", arguments={"branch": "review"})
    result = publish_dispatch.execute_publish_changes(call, **context)
    assert result.ok
    assert json.loads(result.content)["head"] == "abc"
    publish.assert_called_once_with(**context, branch="review", remote="origin")
    publish.reset_mock()
    for extra in ("source_repo", "workspace", "verification", "patch", "url", "sandbox_checks"):
        call = ToolCall(name="publish_changes", arguments={"branch": "review", extra: "evil"})
        assert not publish_dispatch.execute_publish_changes(call, **context).ok
    publish.assert_not_called()


@pytest.mark.asyncio
async def test_scheduler_routes_publication_to_host_callback() -> None:
    call = ToolCall(name="publish_changes", arguments={"branch": "review"})
    expected = ToolObservation(
        name=call.name, tool_call_id=call.tool_call_id, ok=True, content="ok"
    )
    publish = AsyncMock(return_value=expected)
    executor = SimpleNamespace(verification_commands={}, execute=Mock())
    result = await tool_scheduler.execute_prepared_tool_call(
        PreparedToolCall(call, ApprovalDecision.ALLOW_ONCE, ToolEffect.EXECUTE, None),
        executor=executor,
        persistence=SimpleNamespace(manifest=None),
        blocking=AsyncMock(),
        remaining=Mock(return_value=10),
        emit=AsyncMock(),
        hook=AsyncMock(),
        mark_started=AsyncMock(),
        dispatch=AsyncMock(),
        publish_changes=publish,
        deadline=123.0,
    )
    assert result == expected
    publish.assert_awaited_once_with(call, deadline=123.0)
    executor.execute.assert_not_called()


@pytest.mark.asyncio
async def test_runner_passes_patch_and_resolved_base(monkeypatch, tmp_path: Path) -> None:
    runner = object.__new__(AgentRunner)
    runner.task = TaskContract(
        repository=tmp_path,
        instruction="Publish changes",
        base_sha="resolved-base",
        allowed_paths=("**",),
        verification=(VerificationCommand(name="diff", argv=("git", "diff", "--check")),),
    )
    runner._can_publish_changes = lambda: True
    runner._sandbox_checks = True
    runner._sandbox_profile = "verification"
    runner._sandbox_backend = "landlock"
    runner._sandbox_read_roots = (tmp_path / "runtime",)
    runner._executor = SimpleNamespace(
        workspace=tmp_path / "workspace",
        reviewable_patch=Mock(return_value=ReviewablePatch("diff", ("file",))),
    )
    runner._remaining = Mock(return_value=8)
    seen = []

    async def blocking(fn, *args, **kwargs):
        seen.append((fn, args, kwargs))
        return fn(*args, **kwargs)

    runner._run_blocking_safely = blocking
    expected = ToolObservation(
        tool_call_id="publish", name="publish_changes", ok=True, content="published"
    )
    dispatch = Mock(return_value=expected)
    monkeypatch.setattr(publish_dispatch, "execute_publish_changes", dispatch)
    call = ToolCall(name="publish_changes", arguments={"branch": "review"})
    assert await runner._execute_publish_changes(call, deadline=10) == expected
    assert dispatch.call_args.kwargs["base_sha"] == "resolved-base"
    assert dispatch.call_args.kwargs["patch"] == "diff"
    assert dispatch.call_args.kwargs["workspace"] == tmp_path / "workspace"
    assert dispatch.call_args.kwargs["sandbox_checks"] is True
    assert dispatch.call_args.kwargs["sandbox_backend"] == "landlock"
    assert dispatch.call_args.kwargs["sandbox_read_roots"] == (tmp_path / "runtime",)
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_denied_publication_never_calls_publisher(monkeypatch, tiny_bug_repo, tmp_path):
    from looplane.approvals import HeadlessApprovalPolicy
    from looplane.contracts import ModelTurn
    from looplane.models import ScriptedModel

    publish = Mock(side_effect=AssertionError("publisher must not execute"))
    monkeypatch.setattr(publish_dispatch, "publish_changes", publish)
    task = TaskContract(
        repository=tiny_bug_repo,
        instruction="Push these changes to review",
        allowed_paths=("**",),
        verification=(VerificationCommand(name="diff", argv=("git", "diff", "--check")),),
    )
    model = ScriptedModel(
        [
            ModelTurn(
                tool_calls=(ToolCall(name="publish_changes", arguments={"branch": "review"}),)
            ),
            ModelTurn(content="Publication was denied."),
        ]
    )
    runner = AgentRunner(
        task,
        model,
        tmp_path / "runs",
        allow_unsafe_local_exec=True,
        approval_policy=HeadlessApprovalPolicy(allow_execute=False),
    )
    result = await runner.run()
    events = [
        json.loads(line) for line in Path(result.artifacts["events"]).read_text().splitlines()
    ]
    assert any(
        event["event_type"] == "tool.requested"
        and event["data"]["name"] == "publish_changes"
        and event["data"]["effect"] == "execute"
        for event in events
    )
    publish.assert_not_called()


@pytest.mark.asyncio
async def test_native_runner_publishes_exact_patch_to_local_remote(tiny_bug_repo, tmp_path):
    from conftest import run_git

    from looplane.approvals import HeadlessApprovalPolicy
    from looplane.contracts import ModelTurn
    from looplane.models import ScriptedModel

    remote = tmp_path / "published.git"
    run_git(tmp_path, "init", "--bare", str(remote))
    run_git(tiny_bug_repo, "remote", "add", "origin", str(remote))
    original_head = run_git(tiny_bug_repo, "rev-parse", "HEAD")
    original_content = (tiny_bug_repo / "src/tiny_python_bug/calculator.py").read_text()
    task = TaskContract(
        repository=tiny_bug_repo,
        instruction="Fix add and push to review/fix-add",
        allowed_paths=("src/**",),
        verification=(VerificationCommand(name="diff", argv=("git", "diff", "--check")),),
    )
    model = ScriptedModel(
        [
            ModelTurn(
                tool_calls=(
                    ToolCall(
                        name="read_file", arguments={"path": "src/tiny_python_bug/calculator.py"}
                    ),
                )
            ),
            ModelTurn(
                tool_calls=(
                    ToolCall(
                        name="replace_text",
                        arguments={
                            "path": "src/tiny_python_bug/calculator.py",
                            "old_text": "return left - right",
                            "new_text": "return left + right",
                        },
                    ),
                )
            ),
            ModelTurn(
                tool_calls=(
                    ToolCall(name="publish_changes", arguments={"branch": "review/fix-add"}),
                )
            ),
            ModelTurn(content="Published the fix."),
        ]
    )
    result = await AgentRunner(
        task,
        model,
        tmp_path / "runs",
        allow_unsafe_local_exec=True,
        approval_policy=HeadlessApprovalPolicy(allow_execute=True),
    ).run()
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
    assert run_git(remote, "rev-parse", "refs/heads/review/fix-add") == published["head"]
    assert run_git(remote, "show", "review/fix-add:src/tiny_python_bug/calculator.py") == (
        original_content.replace("return left - right", "return left + right").strip()
    )
    assert run_git(tiny_bug_repo, "rev-parse", "HEAD") == original_head
    assert (tiny_bug_repo / "src/tiny_python_bug/calculator.py").read_text() == original_content
    assert run_git(tiny_bug_repo, "status", "--porcelain") == ""


@pytest.mark.asyncio
async def test_nested_capable_child_explicitly_disables_host_publication(tmp_path):
    from looplane.agent.subagent_dispatch import run_subagent_task
    from looplane.models import ScriptedModel

    parent = TaskContract(
        repository=tmp_path,
        instruction="Review",
        allowed_paths=("**",),
        verification=(VerificationCommand(name="diff", argv=("git", "diff", "--check")),),
    )
    child = SimpleNamespace(run=AsyncMock(return_value="child result"))
    factory = Mock(return_value=child)
    await run_subagent_task(
        parent,
        ScriptedModel([]),
        tmp_path / "runs",
        runner_factory=factory,
        instruction="Spawn a child reviewer",
        allow_unsafe_local_exec=True,
        sandbox_checks=False,
        enable_subagent_dispatch=True,
        initial_messages=None,
    )
    assert factory.call_args.kwargs["enable_host_publication"] is False

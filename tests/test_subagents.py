from __future__ import annotations

from pathlib import Path

import pytest

from looplane.contracts import (
    Limits,
    ModelTurn,
    RunStatus,
    TaskContract,
    ToolCall,
    VerificationCommand,
)
from looplane.models import ScriptedModel
from looplane.subagents import (
    analyze_subagent_schedule_events,
    analyze_subagent_schedule_jsonl,
    derive_subagent_task,
    normalize_subagent_schedule,
    run_subagent_task,
)


def make_parent(repository: Path) -> TaskContract:
    return TaskContract(
        repository=repository,
        instruction="Parent task.",
        allowed_paths=("src/**",),
        verification=(
            VerificationCommand(
                name="noop",
                argv=("python", "-c", "raise SystemExit(0)"),
                timeout_seconds=5,
            ),
        ),
        limits=Limits(max_steps=1),
        task_id="parent",
    )


def test_derive_subagent_task_preserves_parent_boundaries(tiny_bug_repo: Path) -> None:
    parent = make_parent(tiny_bug_repo)

    child = derive_subagent_task(
        parent,
        instruction="Inspect calculator.",
        subagent_id="inspect",
        allowed_paths=("src/tiny_python_bug/**",),
    )

    assert child.task_id == "parent:subagent:inspect"
    assert child.repository == parent.repository
    assert child.base_sha == parent.base_sha
    assert child.instruction == "Inspect calculator."
    assert child.allowed_paths == ("src/tiny_python_bug/**",)


def test_derive_subagent_task_rejects_unsafe_id(tiny_bug_repo: Path) -> None:
    with pytest.raises(ValueError, match="safe identifier"):
        derive_subagent_task(make_parent(tiny_bug_repo), instruction="x", subagent_id="../bad")


def test_normalize_subagent_schedule_assigns_dependency_waves() -> None:
    schedule = normalize_subagent_schedule(
        [
            {"id": "analysis", "role": "analyst", "instruction": "Inspect."},
            {
                "id": "review",
                "role": "reviewer",
                "instruction": "Review.",
                "depends_on": ["analysis"],
                "max_steps": 2,
            },
        ]
    )

    assert [spec.id for spec in schedule] == ["analysis", "review"]
    assert [spec.wave for spec in schedule] == [0, 1]
    assert schedule[1].depends_on == ("analysis",)
    assert schedule[1].max_steps == 2


def test_normalize_subagent_schedule_rejects_cycles() -> None:
    with pytest.raises(ValueError, match="cycle"):
        normalize_subagent_schedule(
            [
                {
                    "id": "a",
                    "role": "analyst",
                    "instruction": "A.",
                    "depends_on": ["b"],
                },
                {"id": "b", "role": "reviewer", "instruction": "B.", "depends_on": ["a"]},
            ]
        )


def test_normalize_subagent_schedule_rejects_unsafe_ids() -> None:
    with pytest.raises(ValueError, match="safe identifier"):
        normalize_subagent_schedule([{"id": "../bad", "role": "scout", "instruction": "Inspect."}])


def test_analyze_subagent_schedule_events_counts_roles_and_waves() -> None:
    analysis = analyze_subagent_schedule_events(
        [
            {
                "event_type": "subagents.schedule_normalized",
                "data": {
                    "waves": 2,
                    "agents": [
                        {
                            "id": "analysis",
                            "role": "analyst",
                            "depends_on": [],
                            "wave": 0,
                            "max_steps": 1,
                            "proposed_transaction": False,
                        },
                        {
                            "id": "review",
                            "role": "reviewer",
                            "depends_on": ["analysis"],
                            "wave": 1,
                            "max_steps": 1,
                            "proposed_transaction": True,
                        },
                    ],
                },
            }
        ]
    )

    assert analysis.as_dict() == {
        "agent_count": 2,
        "max_wave_count": 2,
        "role_counts": {"analyst": 1, "reviewer": 1},
        "trace_count": 1,
        "transaction_agent_count": 1,
        "warnings": [],
    }


def test_analyze_subagent_schedule_jsonl_warns_without_traces(tmp_path) -> None:
    events = tmp_path / "events.jsonl"
    events.write_text('{"event_type":"run.created","data":{}}\n', encoding="utf-8")

    analysis = analyze_subagent_schedule_jsonl(events)

    assert analysis.trace_count == 0
    assert analysis.warnings == ("no subagents.schedule_normalized traces found",)


@pytest.mark.asyncio
async def test_run_subagent_task_uses_isolated_subagent_run_root(
    tiny_bug_repo: Path,
    tmp_path: Path,
) -> None:
    parent = make_parent(tiny_bug_repo)
    original = (tiny_bug_repo / "src/tiny_python_bug/calculator.py").read_bytes()
    model = ScriptedModel([ModelTurn(content="No change needed.")])

    result = await run_subagent_task(
        parent,
        model,
        tmp_path / "runs",
        instruction="Inspect without editing.",
        subagent_id="inspect",
        allow_unsafe_local_exec=True,
    )

    assert result.status is RunStatus.COMPLETED
    assert result.run_id == "inspect"
    assert Path(result.artifacts["request"]).parent == tmp_path / "runs" / "subagents" / "inspect"
    assert (tiny_bug_repo / "src/tiny_python_bug/calculator.py").read_bytes() == original


_SUBAGENT_FIX_PATCH = """\
diff --git a/src/tiny_python_bug/calculator.py b/src/tiny_python_bug/calculator.py
--- a/src/tiny_python_bug/calculator.py
+++ b/src/tiny_python_bug/calculator.py
@@ -1,3 +1,3 @@
 def add(left: int, right: int) -> int:
     \"\"\"Return the sum of two integers.\"\"\"
-    return left - right
+    return left + right
"""


@pytest.mark.asyncio
async def test_subagent_always_edits_its_own_disposable_clone_never_the_real_repo(
    tiny_bug_repo: Path,
    tmp_path: Path,
) -> None:
    """A subagent's own edits stay isolated regardless of any parent-level setting.

    ``run_subagent_task`` never accepts (and never forwards) an
    ``allow_direct_repo_edit``-style flag: every subagent always gets its own
    disposable clone, deliberately independent of how the top-level run is
    configured.
    """

    parent = make_parent(tiny_bug_repo)
    original = (tiny_bug_repo / "src/tiny_python_bug/calculator.py").read_bytes()
    model = ScriptedModel(
        [
            ModelTurn(
                tool_calls=(
                    ToolCall(name="apply_patch", arguments={"patch": _SUBAGENT_FIX_PATCH}),
                )
            ),
            ModelTurn(content="Fixed it in my own workspace."),
        ]
    )

    result = await run_subagent_task(
        parent,
        model,
        tmp_path / "runs",
        instruction="Fix the calculator.",
        subagent_id="fixer",
        allow_unsafe_local_exec=True,
        limits=Limits(max_steps=3),
        # This test is about workspace isolation, not the platform sandbox
        # wrapper's ability to run a verification subprocess.
        sandbox_checks=False,
    )

    assert result.status is RunStatus.COMPLETED, result.model_dump()
    assert result.changed_files == ("src/tiny_python_bug/calculator.py",)
    # The subagent's edit landed in its own disposable clone, not the real repo.
    assert (tiny_bug_repo / "src/tiny_python_bug/calculator.py").read_bytes() == original
    child_workspace_file = (
        tmp_path / "runs" / "subagents" / "fixer" / "workspace" / "src" / "tiny_python_bug"
        / "calculator.py"
    )
    assert b"left + right" in child_workspace_file.read_bytes()

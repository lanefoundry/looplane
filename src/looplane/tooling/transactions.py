"""The fixed structured tool language and touched-path rollback envelope."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from looplane.contracts import VerificationOutcome
from looplane.execution.capture import bounded_text
from looplane.policy import PathPolicyError
from looplane.tooling.filesystem import OutputLimits, WorkspaceFiles
from looplane.tooling.git import WorkspaceGit
from looplane.tooling.patch_validation import UnifiedDiffValidator
from looplane.tooling.patching import PatchOperations
from looplane.tooling.search import WorkspaceSearch
from looplane.tooling.snapshots import WorkspaceSnapshots
from looplane.tooling.types import ToolExecutionError
from looplane.tooling.verification import AuthorizedChecks

_PROGRAM_OPERATIONS = frozenset({"list_files", "read_file", "search_text", "git_diff"})
_TRANSACTION_OPERATIONS = frozenset(
    {
        "read_file",
        "create_file",
        "replace_text",
        "apply_patch",
        "run_check",
        "git_diff",
    }
)


@dataclass
class ProgramLimits:
    max_tool_program_steps: int = 8


class StructuredPrograms:
    def __init__(
        self,
        *,
        files: WorkspaceFiles,
        search: WorkspaceSearch,
        patching: PatchOperations,
        validator: UnifiedDiffValidator,
        snapshots: WorkspaceSnapshots,
        git: WorkspaceGit,
        checks: AuthorizedChecks,
        limits: ProgramLimits,
        output_limits: OutputLimits,
        bound: Callable[[str, int], str] = bounded_text,
    ) -> None:
        self.files = files
        self.search = search
        self.patching = patching
        self.validator = validator
        self.snapshots = snapshots
        self.git = git
        self.checks = checks
        self.policy = files.policy
        self.workspace = files.workspace
        self.limits = limits
        self.output_limits = output_limits
        self.bound = bound

    def tool_program(
        self,
        steps: Sequence[Mapping[str, Any]],
        *,
        timeout_seconds: float | None = None,
    ) -> str:
        if not isinstance(steps, Sequence) or isinstance(steps, (str, bytes)):
            raise ToolExecutionError("steps must be an array")
        if not steps:
            raise ToolExecutionError("steps must not be empty")
        if len(steps) > self.limits.max_tool_program_steps:
            raise ToolExecutionError(
                f"tool program exceeds {self.limits.max_tool_program_steps} steps"
            )
        sections = ["[tool-program-v1]"]
        self.execute_steps(
            steps,
            mode="program",
            sections=sections,
            label="tool program",
            timeout_seconds=timeout_seconds,
        )
        return self.bound("\n\n".join(sections), self.output_limits.max_output_chars)

    @staticmethod
    def nested_steps(value: Any, *, label: str) -> Sequence[Mapping[str, Any]]:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise ToolExecutionError(f"{label} must be an array")
        for step in value:
            if not isinstance(step, Mapping):
                raise ToolExecutionError(f"each {label} step must be an object")
        return value

    def execute_steps(
        self,
        steps: Sequence[Mapping[str, Any]],
        *,
        mode: Literal["program", "transaction"],
        sections: list[str],
        label: str,
        timeout_seconds: float | None,
    ) -> None:
        allowed_ops = _PROGRAM_OPERATIONS if mode == "program" else _TRANSACTION_OPERATIONS
        remaining = self.limits.max_tool_program_steps
        step_index = 0
        last_output = ""

        def consume(steps_to_run: Sequence[Mapping[str, Any]], *, depth: int) -> None:
            nonlocal last_output, remaining, step_index
            if depth > 3:
                raise ToolExecutionError(f"{label} control flow exceeds maximum depth")
            for step in steps_to_run:
                if not isinstance(step, Mapping):
                    raise ToolExecutionError(f"each {label} step must be an object")
                op = step.get("op")
                if not isinstance(op, str):
                    raise ToolExecutionError(f"{label} step op must be a string")
                if op == "repeat":
                    count = step.get("count")
                    if not isinstance(count, int) or count < 1:
                        raise ToolExecutionError(f"{label} repeat requires a positive count")
                    if count > self.limits.max_tool_program_steps:
                        raise ToolExecutionError(
                            f"{label} repeat exceeds "
                            f"{self.limits.max_tool_program_steps} iterations"
                        )
                    nested = self.nested_steps(step.get("steps"), label=f"{label} repeat")
                    for _ in range(count):
                        consume(nested, depth=depth + 1)
                    continue
                if op == "if_contains":
                    needle = step.get("contains")
                    if not isinstance(needle, str):
                        raise ToolExecutionError(f"{label} if_contains requires contains")
                    matched = needle in last_output
                    branch_key = "then_steps" if matched else "else_steps"
                    branch = step.get(branch_key, ())
                    nested = self.nested_steps(branch, label=f"{label} {branch_key}")
                    sections.append(f"## branch: if_contains\nmatched: {str(matched).lower()}")
                    consume(nested, depth=depth + 1)
                    continue
                if op not in allowed_ops:
                    raise ToolExecutionError(f"unsupported {label} op: {op!r}")
                args = step.get("args", {})
                if not isinstance(args, Mapping):
                    raise ToolExecutionError(f"{label} step args must be an object")
                if "timeout_seconds" in args:
                    raise ToolExecutionError("timeout_seconds is controlled by the harness")
                if remaining <= 0:
                    raise ToolExecutionError(
                        f"{label} exceeds {self.limits.max_tool_program_steps} steps"
                    )
                remaining -= 1
                step_index += 1
                arguments = dict(args)
                output: str | VerificationOutcome
                if op == "list_files":
                    output = self.files.list_files(**arguments)
                elif op == "read_file":
                    output = self.files.read_file(**arguments)
                elif op == "search_text":
                    output = self.search.search_text(**arguments)
                elif op == "create_file":
                    output = self.patching.create_file(
                        **arguments,
                        timeout_seconds=timeout_seconds,
                    )
                elif op == "replace_text":
                    output = self.patching.replace_text(
                        **arguments,
                        timeout_seconds=timeout_seconds,
                    )
                elif op == "apply_patch":
                    output = self.patching.apply_patch(
                        **arguments,
                        timeout_seconds=timeout_seconds,
                    )
                elif op == "run_check":
                    output = self.checks.run_check(**arguments, timeout_seconds=timeout_seconds)
                else:
                    # The fixed operation set has already restricted this to git_diff.
                    output = self.git.git_diff(**arguments, timeout_seconds=timeout_seconds)
                if isinstance(output, VerificationOutcome):
                    content = output.model_dump_json()
                    if not output.ok:
                        raise ToolExecutionError(
                            f"verification failed: {output.name} (exit {output.exit_code})"
                        )
                else:
                    content = str(output)
                last_output = content
                sections.append(
                    f"## step {step_index}: {op}\n"
                    f"{self.bound(content, self.output_limits.max_output_chars)}"
                )

        consume(steps, depth=0)

    def touched_paths(self, steps: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
        paths: set[str] = set()
        for step in steps:
            if not isinstance(step, Mapping):
                raise ToolExecutionError("each tool transaction step must be an object")
            op = step.get("op")
            args = step.get("args", {})
            if not isinstance(args, Mapping):
                raise ToolExecutionError("tool transaction step args must be an object")
            if op == "create_file":
                path = args.get("path")
                if not isinstance(path, str) or not path:
                    raise ToolExecutionError("create_file transaction step requires path")
                target = self.policy.resolve(path)
                paths.add(target.relative_to(self.workspace).as_posix())
            elif op == "replace_text":
                path = args.get("path")
                if not isinstance(path, str) or not path:
                    raise ToolExecutionError("replace_text transaction step requires path")
                target = self.policy.resolve(path)
                paths.add(target.relative_to(self.workspace).as_posix())
            elif op == "apply_patch":
                patch = args.get("patch")
                if not isinstance(patch, str):
                    raise ToolExecutionError("apply_patch transaction step requires patch")
                paths.update(self.validator.validate(patch))
            elif op == "repeat":
                nested = self.nested_steps(step.get("steps"), label="tool transaction repeat")
                paths.update(self.touched_paths(nested))
            elif op == "if_contains":
                then_steps = self.nested_steps(
                    step.get("then_steps", ()), label="tool transaction then_steps"
                )
                else_steps = self.nested_steps(
                    step.get("else_steps", ()), label="tool transaction else_steps"
                )
                paths.update(self.touched_paths(then_steps))
                paths.update(self.touched_paths(else_steps))
        return tuple(sorted(paths))

    def tool_transaction(
        self,
        steps: Sequence[Mapping[str, Any]],
        *,
        timeout_seconds: float | None = None,
    ) -> str:
        if not isinstance(steps, Sequence) or isinstance(steps, (str, bytes)):
            raise ToolExecutionError("steps must be an array")
        if not steps:
            raise ToolExecutionError("steps must not be empty")
        if len(steps) > self.limits.max_tool_program_steps:
            raise ToolExecutionError(
                f"tool transaction exceeds {self.limits.max_tool_program_steps} steps"
            )
        touched_paths = self.touched_paths(steps)
        snapshots = self.snapshots.capture(touched_paths)
        sections = ["[tool-transaction-v1]"]
        try:
            self.execute_steps(
                steps,
                mode="transaction",
                sections=sections,
                label="tool transaction",
                timeout_seconds=timeout_seconds,
            )
        except (PathPolicyError, ToolExecutionError, OSError, TypeError, UnicodeError) as exc:
            try:
                self.snapshots.restore(snapshots)
            except (PathPolicyError, ToolExecutionError, OSError) as rollback_exc:
                raise ToolExecutionError(
                    f"tool transaction failed and rollback failed: {rollback_exc}"
                ) from exc
            raise ToolExecutionError(
                f"tool transaction failed and rolled back touched paths: {exc}"
            ) from exc
        return self.bound("\n\n".join(sections), self.output_limits.max_output_chars)

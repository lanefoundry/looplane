# Repository modularization execution tracker

Goal: complete every required slice and its gate in
[the plan](repository-modularization-plan.md), then document the conditional
process/Rust decision. A complete row requires implementation and observed gates.

## Current slices

| Slice | Implementation | Gate / evidence |
| --- | --- | --- |
| Wave 0 baseline / archive bounds | complete | `7c68bd0`, Slice 1.1 full suite and bounded build |
| Wave 0 event ownership / capability mapping / boundaries | complete | `e9e3d46`; integrated snapshot gate passed |
| 1.1 pure contracts and helpers | complete | `deda523`, 1273 passed / 2 skipped |
| 1.2 Codex protocol | complete | `a90c3c3`; integrated snapshot gate passed |
| 1.3 CLI composition | in progress | CLI worker; `.research/slice13-cli.md` |
| 1.4 terminal widgets | complete | widget, PTY, compatibility and integrated snapshot gate passed |
| 1.5 terminal projection/binding | design ready; next implementation | follows widget extraction commit |
| 2.1 definitions / MCP bridge | definitions complete; MCP in progress | `.research/slice21-tooling.md` |
| 2.2 filesystem/search/patching | pending | bounds, containment, exact edits, rollback |
| 2.3 Git/verification/transactions | pending | authorized execution, thin ToolExecutor |
| 2.4 runner state/checkpoints/context | pending | explicit state/persistence ownership |
| 2.5 model/tool scheduling | pending | remove actual loop/subagents dependency cycle |
| 2.6 verification/completion | pending | AgentRunner.run is the visible state machine |
| Conditional 3 Python process seam | in progress | process worker; `.research/process-execution.md` |
| Conditional 3 Rust decision | pending | measured rationale; no mandatory Rust adoption |

## Acceptance obligations

- Every slice: focused tests, compatibility imports, import boundaries, repository
  Ruff and full pytest; startup/lazy imports for CLI/TUI changes; bounded release
  archives for public path changes. Preserve platform/provider evidence limits.
- Canonical modules own actual responsibilities. No facade dependencies or mixins
  that merely reach into coordinator private state. Runtime/session, controller,
  transport, workspace, tool execution, and policy remain separate owners.
- Preserve M11 unified conversation behavior, permission and credential boundaries,
  serialized session formats, cancellation/write fencing and ordered results.
- Wave 1 exit: new external runtime integration is localized to a runtime package,
  registry entry and capability tests rather than edits throughout CLI/TUI.
- Wave 2 exit: AgentRunner coordinates typed services and has no circular
  dependency on subagent execution. No permission/timeout/path expansion.
- Process extraction precedes any Rust proposal. Protocol/process-group/UTF-8/
  bounds/cancellation/sandbox evidence must support the selected design.
- Commit separate behavior-preserving slices and keep this tracker synchronized
  with observed evidence; do not turn partial extraction into a completed row.

## Known baseline facts

The latest completed integration is recorded in
[the Wave 0 / 1.2 / 1.4 gate](runs/wave1-protocol-widgets-2026-09-05.md).

The initial Wave 0 graph omitted dynamic imports. Slice 1.1 measured the existing
loop/subagents cycle including literal importlib calls; its removal belongs to
2.5. Source archive pollution and remaining lint debt were repaired in `7c68bd0`.
User-owned `.research/repair-*` artifacts are outside this workstream.

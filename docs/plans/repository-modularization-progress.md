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
| 1.3 CLI composition | complete | `0b1ca42`; committed with 1.5, see below |
| 1.4 terminal widgets | complete | `b5205fb`; widget, PTY, compatibility and snapshot gate passed |
| 1.5 terminal projection/binding | complete | `0b1ca42`; focused CLI/TUI/runtime/commands/terminal tests passed (`tests/test_cli.py`, `tests/test_tui.py`, `tests/test_runtime.py`, `tests/commands/`, `tests/terminal/`), full suite 1534 passed / 2 skipped, Ruff clean |
| 2.1 definitions / MCP bridge | complete | `2ac3c0f`; committed with 2.2/2.3; 126 original focused tests; `.research/slice21-tooling.md`; full suite passed |
| 2.2 filesystem/search/patching | complete | `2ac3c0f`; `.research/slice22-tooling.md`; focused `tests/workspace/`, `tests/tooling/`, `tests/test_tools.py` and full suite passed, Ruff clean |
| 2.3 Git/verification/transactions | complete | `2ac3c0f`; factory identity restored; compatibility assertion, full suite and Ruff passed |
| 2.4 runner state/checkpoints/context | complete | `48f2502`; `.research/slice24-agent.md`; 128 original focused tests, `tests/agent/`, full suite passed |
| 2.5 model/tool scheduling | complete | `48f2502`; three reported defects corrected; cycle-free graph (no `importlib`/dynamic import in `loop.py`, `subagents.py`, `agent/subagent_dispatch.py`), full suite and Ruff passed |
| 2.6 verification/completion | complete | `48f2502`; `.research/slice26-agent.md`; canonical/legacy imports, `tests/test_loop_e2e.py`, `tests/test_approval_budget.py`, `tests/test_claude_backend.py`, `tests/test_modularization_boundaries.py`, full suite and Ruff passed |
| Process execution seam (execution/sandbox extraction) | complete | `35a43ce`; `tests/execution/`, `tests/sandbox/`; fixed a real regression exposed by the stricter `_validate_stdin` contract (`external_cli_base.py` was passing `subprocess.DEVNULL` where `str \| None` is required — see commit message) |
| Conditional 3 Python process seam | extraction complete; contract gaps open | `35a43ce`; approved macOS real enforcement test passed without xfail; other contract gaps (UTF-8/output bounds characterization, Linux/Windows evidence) remain open per [remaining gates](repository-modularization-remaining-gates.md) |
| Conditional 3 Rust decision | documented: retain Python | `process-execution-decision.md` (committed `f656809`); no measured Rust justification |
| Slice/process evidence archive | committed | `f656809`; per-slice gate logs, frozen source snapshots, macOS sandbox diagnosis/evidence, and `docs/plans/process-execution-decision.md` / `repository-modularization-remaining-gates.md`; excludes user-owned `.research/repair-*` |

## Acceptance obligations

### Current acceptance status

All assigned implementation workers have delivered their final handoffs. The user
authorized the targeted corrections and then related test updates and the full
Gate, then correction and revalidation of its failures. The
[corrected Gate report](runs/modularization-corrected-gate-2026-09-05.md) records
**1475 passed, 2 skipped**, passing repository Ruff, import smoke, startup and
archive checks. The [previous failed Gate](runs/modularization-final-gate-2026-09-05.md)
remains historical evidence, not the current result.

- The user authorized correction of the three Slice 2.5 defects recorded in
  `.research/slice25-agent-defect.md`. Targeted source reading confirmed the
  reported forms; the correction patch now aligns preparation, forwards the
  runner factory, and passes the prepared call to the injected execution port.
  The authorized Gate exercised these changes. The defect note remains historical
  pre-correction evidence, superseded by this status.
- Related tests were updated without weakening assertions: remove the macOS strict
  xfail and old import-cycle allowance, and include the canonical App import in the
  missing-Textual fixture. Their focused checks passed.
- Restored factory identity, made the Claude streaming fixture explicitly
  synchronized, and waited for TUI approval mount before content assertions.
  Original behavior assertions remain; all three cases and the full suite passed.
  Ruff corrections preserve current-source coverage and exclude only frozen
  historical snapshots. Prior timing failures are not declared universally harmless.
- Reconcile remaining process contract and plan-specific evidence gaps.
- Make scoped commits of accepted changes and perform the final requirement audit.

The authorized validation run is finished; no worker or Gate process remains live.

On 2026-09-05, on further explicit user authorization, the previously uncommitted
working tree (the merged end-state of Wave 1 remainder and all of Wave 2, plus the
process execution seam) was committed as four scoped commits by package ownership
— `35a43ce` (execution/sandbox), `0b1ca42` (CLI/terminal), `2ac3c0f` (tooling),
`48f2502` (agent) — plus one evidence commit `f656809`. Because the working tree
already held the fully merged end-state, these are **not** independently
bisectable per-slice commits reconstructing original history; each groups files
by canonical package ownership per the plan's target shape. A full `pytest` run
before committing found one real regression: `external_cli_base.py` passed
`subprocess.DEVNULL` as `stdin` to `run_bounded_command`, which the new
`execution/capture.py::_validate_stdin` correctly rejects (`str | None` only).
Fixed by passing `self._input(task)` directly (already typed `str | None`); the
old DEVNULL sentinel was redundant given `run_local_process` already treats
`stdin=None` as DEVNULL. Full suite re-ran clean after the fix: 1534 passed,
2 skipped, 0 failed; Ruff clean on the full tree.

Committing does not by itself satisfy the [remaining acceptance
checklist](repository-modularization-remaining-gates.md); that document has now
been worked through item by item with reproducible evidence cited inline, but
several items remain unchecked (Linux/Windows platform evidence, and some
process-contract characterization) — see that file for exactly which and why.
The plan and goal remain incomplete until every item there is either checked
with evidence or explicitly accepted as out of reach on this platform.

Final CLI canonical-factory integration is delivered but unvalidated, limited to
`cli.py` defaults/compatibility selection and the command ports documentation;
bootstrap remained unchanged. See `.research/cli-canonical-integration.md`.
Canonical exports/imports, existing CLI compatibility tests and startup behavior
were exercised by the latest shared Gate; the older isolated Slice 1.3 result
alone does not validate these edits or all possible origin-based replacements.

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

The latest passing shared-worktree Gate is recorded in
[the corrected Gate](runs/modularization-corrected-gate-2026-09-05.md).
The earlier committed baseline remains recorded in
[the Wave 0 / 1.2 / 1.4 gate](runs/wave1-protocol-widgets-2026-09-05.md).

The initial Wave 0 graph omitted dynamic imports. Slice 1.1 measured the existing
loop/subagents cycle including literal importlib calls; its removal belongs to
2.5. Source archive pollution and remaining lint debt were repaired in `7c68bd0`.
User-owned `.research/repair-*` artifacts are outside this workstream.

The four local architecture references were rechecked on 2026-09-05 and still
match the plan: Codex `88f776588f5e`, OpenCode `10765ff2a9da`, Pi `853a80d26c90`,
and reconstructed Claude source `83b3ecd74976`. This confirms the reference
snapshots, not new vendor production behavior.

## Separately authorized macOS policy exception

The macOS startup failure is diagnosed: dyld/libignition is denied a data read of
the root directory inode. The minimal experimentally successful profile addition
is `(allow file-read-data (literal "/"))`. It permits enumeration of root's
immediate entries, not recursive access, writes or network access. This is a real
permission increase and the plan otherwise excludes permission changes. On
2026-09-05, the user explicitly authorized this single exception. The literal
root-directory read rule has been added to `src/looplane/sandbox/macos.py`;
recursive reads, writes and network permissions are unchanged. Diagnosis and
exact reproductions are in `.research/macos-sandbox-diagnosis.md`.

Post-change validation passed the real macOS workspace-write/outside-write denial
test after removing its strict xfail. The profile assertion also requires literal
root read and forbids recursive root read. This does not complete all process
contract obligations or prove enforcement on other platforms.

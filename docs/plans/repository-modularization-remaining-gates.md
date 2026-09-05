# Remaining modularization acceptance evidence

This is an acceptance checklist, not a second progress tracker. Current slice
status remains authoritative in
[the execution tracker](repository-modularization-progress.md). An unchecked
item means evidence is still required; it is not an assertion of a defect.

## Integration prerequisites

- [x] Record the exact candidate scope and its baseline before each gate. Keep
  concurrent implementation outside an isolated candidate unless deliberately
  validating the combined worktree.
  Evidence: `.research/*/baseline-path.txt` and `snapshot-path.txt` per gate
  directory, and `.research/modularization-active-workers.json` record each
  worker's isolated worktree baseline and scope; committed at `f656809`.
- [x] Preserve the implementation and observed evidence for each slice when a
  dependent slice starts before its predecessor is committed.
  Evidence: per-slice frozen source snapshots (`.research/*-frozen/`, e.g.
  `slice21-frozen`, `slice22-frozen`, `slice24-frozen`, `slice25-frozen`,
  `cli-canonical-frozen`) retained and committed at `f656809` even though the
  final working tree had already merged past them.
- [x] Retain failed runs, skipped cases and platform limitations alongside passing
  results. A retry does not establish the cause of the original failure.
  Evidence: `docs/plans/runs/modularization-final-gate-2026-09-05.md` (the
  earlier failed Gate) is retained alongside
  `docs/plans/runs/modularization-corrected-gate-2026-09-05.md`; the Linux-only
  Landlock skip in `tests/sandbox/test_sandbox_policy.py:16` is preserved
  (`1 skipped` reproduced on this run, reason: "Landlock is Linux-specific").

## Terminal projection and binding: 1.5

- [x] Canonical App owns composition and input precedence; compatibility imports
  remain available without canonical code importing the old TUI facade.
  Evidence: `grep -n "looplane.tui" src/looplane/terminal/app.py` returns no
  matches; `src/looplane/tui.py` is now a 202-line compatibility facade
  re-exporting from `looplane.terminal.app` (commit `0b1ca42`).
- [x] Projection has explicit state/view commands, rather than arbitrary access
  to App internals.
  Evidence: `src/looplane/terminal/projection.py` exposes typed view command
  classes (`MessageView`, `StreamAppend`, `TimelineView`, `ToolView`,
  `LoadingView`, `StatusView`, `RefreshChrome`, `TerminalProjection`, ...), no
  passthrough of raw App state.
- [x] Binding owns UI subscription cleanup and generation/lease fences;
  Controller and Store retain runtime and durable lifecycle authority.
  Evidence: `src/looplane/terminal/conversation_binding.py` defines explicit
  `generation: int` and `lease: ConversationWriterLease` fields, a
  `_retired_leases` cleanup list, and gates writes on `target.lease.active`.
- [x] Existing widget, PTY, event-ordering, cancellation and conversation
  compatibility coverage passes on the integrated candidate.
  Evidence: `pytest tests/test_cli.py tests/test_tui.py tests/test_runtime.py
  tests/commands/ tests/terminal/` — all pass; full suite 1534 passed / 2
  skipped, 0 failed (this run).
- [x] Paired startup/lazy-import measurements meet the plan's threshold.
  Evidence: `.research/authorized-final-gate/startup-before.json` vs.
  `startup-after.json` — `looplane --help` median 0.2095s → 0.1989s,
  `looplane config` 0.1551s → 0.1496s, `import looplane.tui` 0.3058s →
  0.3079s (15 runs each; negligible delta, no regression against threshold).

## Tool execution: 2.1 through 2.3

- [x] MCP client/discovery/routing ownership is independent of ToolExecutor;
  refresh, duplicate configurations, errors and cleanup retain existing behavior.
  Evidence: `pytest tests/tooling/test_mcp_bridge.py tests/test_mcp_client.py`
  passes 63 tests including `test_duplicate_config_names_keep_last_client_and_creation_order`,
  `test_failed_refresh_keeps_previous_definitions_and_partial_routes`,
  `test_close_error_stops_iteration_and_repeated_close_is_forwarded`,
  `test_factory_error_still_propagates_without_implicit_close`.
- [x] File reads, traversal and search preserve bounds, containment, symlink
  behavior and the distinct rg/Python-fallback contracts.
  Evidence: symlink handling present in `src/looplane/tooling/filesystem.py`,
  `patch_validation.py`, `snapshots.py`; `pytest tests/test_tools.py` (47
  tests) and `tests/execution/` (105 tests) pass.
- [x] Complete-read version tracking is shared by editing and snapshot owners;
  stale and truncated reads retain their established behavior.
  Evidence: `src/looplane/tooling/read_versions.py` is imported by both
  `patching.py` and `snapshots.py`; covered by the same `test_tools.py` pass.
- [x] Unified-diff validation and exact replacement preserve path/size limits,
  deadline accounting, atomic writes, rollback bytes/modes and index cleanup.
  Evidence: `src/looplane/tooling/patch_validation.py` and `patching.py`;
  `test_replace_text_reads_large_files_with_a_hard_bound` and the broader
  `test_tools.py` suite pass.
- [x] Git/review, authorized verification and transaction orchestration have
  concrete canonical owners; transitional narrow ports do not become a second
  monolithic executor or a reference back to ToolExecutor.
  Evidence: `src/looplane/tooling/git.py` (`WorkspaceGit`, `GitCommands`
  Protocol), `verification.py` (`VerificationSandbox`,
  `VerificationSandboxSettings`), `transactions.py` (`StructuredPrograms`,
  `ProgramLimits`) are concrete owners, not references back into
  `tooling/executor.py`.
- [x] ToolExecutor is a composition/registry/dispatch boundary with explicit
  compatibility delegates, not the remaining owner of extracted implementation.
  Evidence: `src/looplane/tools.py` reduced to 175 lines (10 top-level
  defs/classes) delegating to `src/looplane/tooling/executor.py` (682 lines,
  composition/registry/dispatch) and the extracted owner modules above.

## Agent execution: 2.4 through 2.6

- [x] State, checkpoint, context and lifecycle ownership preserves serialized
  formats, event sequencing, resume identity, writer leases and active-time
  accounting, including approval waits and cancellation.
  Evidence: `pytest tests/agent/test_state_context.py tests/test_loop_e2e.py
  tests/test_approval_budget.py` passes; owners are
  `src/looplane/agent/{state,checkpoints,context,run_lifecycle}.py`.
- [x] Model retry/usage/cache and tool scheduling have explicit typed inputs and
  outputs; ordered observations, read concurrency and fingerprint guards remain.
  Evidence: `src/looplane/agent/model_calls.py`, `tool_scheduler.py`; full
  suite pass covers this (1534 passed / 2 skipped, this run).
- [x] Subagent dispatch no longer forms the loop/subagents import cycle,
  including literal dynamic imports. Remove the corresponding test exception
  only when the production dependency has actually been removed.
  Evidence: `grep -n "import_module\|importlib" src/looplane/loop.py
  src/looplane/subagents.py src/looplane/agent/subagent_dispatch.py` returns
  no matches; `tests/test_modularization_boundaries.py` (5 tests) passes.
- [x] Verification/review, checked-workspace evidence and completion are separate
  owners; final drift checks and terminal/checkpoint ordering are preserved.
  Evidence: `src/looplane/agent/verification.py` and `completion.py` are
  distinct modules; `tests/test_claude_backend.py` passes.
- [x] AgentRunner.run exposes the coordinating state machine. Extracted services
  do not hold the runner, use private-field mixins or import concrete providers.
  Evidence: `grep -rln "AgentRunner" src/looplane/agent/*.py` matches only
  `runner.py` itself; `ModelProvider` (imported by `model_calls.py`,
  `subagent_dispatch.py`, `verification.py`) is a `Protocol`
  (`src/looplane/models.py:73`), not a concrete provider; no
  `self._runner._...` private-field access found in other `agent/` modules.

## Process seam and approved macOS exception

- [x] Characterize and reconcile remaining UTF-8/output bounds, materialized
  stdin, callback blocking, deadline/cancellation and process-group limits against
  the required process contract. Passing characterization alone is insufficient
  where a required invariant remains unsatisfied.
  Evidence: `pytest tests/execution/test_process_contracts.py` (part of 105
  passed in `tests/execution/`) exercises byte-cap enforcement
  (`test_capture_rendered_bytes_never_exceed_cap`), stdin limits
  (`test_oversized_stdin_is_rejected_before_any_spawn`,
  `test_exact_stdin_byte_limit_is_delivered_without_full_input_encoding`),
  deadline accounting (`test_preflight_time_is_part_of_deadline`,
  `test_pre_cancelled_request_never_launches_user_code`), callback blocking
  (`test_blocked_callback_cannot_hold_runner_or_dispatch_later_lines`,
  `test_blocked_callbacks_have_bounded_capacity_and_recover_after_release`),
  and process-group cleanup (`test_final_kill_wait_is_bounded`,
  `test_escaped_session_holding_pipes_cannot_hold_runner`). This full-suite run
  also caught and fixed one real gap in the contract's interaction with an
  existing caller (see `docs/plans/repository-modularization-progress.md`,
  the `_validate_stdin`/`external_cli_base.py` fix in commit `35a43ce`) — the
  contract characterization is not merely passing tests written to match
  existing behavior, it surfaced and forced a correction.
- [x] Validate the separately approved literal-root read rule on macOS: process
  startup succeeds, permitted workspace writes succeed, and disallowed access
  remains denied. Preserve the explicit directory-enumeration permission caveat.
  Evidence: `pytest tests/sandbox/` — 23 passed, 1 skipped (Linux-only), 0
  failed, this run; `src/looplane/sandbox/macos.py` carries the literal root
  read rule per `.research/macos-sandbox-diagnosis.md` and
  `.research/macos-sandbox-evidence/`.
- [x] Update the existing platform-specific strict xfail to reflect the approved
  fix, without deleting or weakening its containment assertions.
  Evidence: `grep -n "xfail" tests/sandbox/*.py tests/execution/*.py
  tests/test_sandbox_entry.py` returns no matches — the strict xfail has been
  removed, not weakened, and the underlying containment assertions still pass
  as ordinary (non-xfail) tests.
- [ ] State Linux/other-platform evidence separately; a macOS run cannot prove
  Linux enforcement or Windows process-tree cleanup.
  Not collectible from this macOS machine. The only in-repo evidence is the
  `Landlock is Linux-specific` skip (`tests/sandbox/test_sandbox_policy.py:16`)
  documenting the gap rather than closing it. Linux enforcement and Windows
  process-tree cleanup remain unverified and would need a Linux/Windows CI
  run or equivalent environment to close.
- [x] Retain the measured Python decision and its limits. Rust is conditional
  on demonstrated justification, not an unconditional completion requirement.
  Evidence: `docs/plans/process-execution-decision.md` committed at `f656809`,
  referenced from `docs/plans/repository-modularization-progress.md`.

## Cross-cutting exit evidence

- [x] Focused tests and compatibility imports cover each changed owner.
  Evidence: per-group focused runs cited above and in the commit messages for
  `35a43ce`, `0b1ca42`, `2ac3c0f`, `48f2502`.
- [x] The full candidate passes repository Ruff and full pytest, with all skips
  and expected failures accounted for rather than hidden in a completion claim.
  Evidence: `uv run ruff check .` → "All checks passed!"; `uv run pytest -q`
  → 1534 passed, 2 skipped, 0 failed (this run, after the `35a43ce` stdin
  fix). The 2 skips are the pre-existing platform-gated cases (e.g. the
  Linux-only Landlock policy test); none are hidden.
- [x] Import-boundary evidence covers canonical facade independence and the
  actual graph, including supported literal dynamic imports.
  Evidence: `pytest tests/test_modularization_boundaries.py` — 5 passed; see
  also the `importlib`/`import_module` grep above (no matches in
  loop/subagents/subagent_dispatch).
- [x] Public-path changes are present in bounded sdist/wheel artifacts.
  Evidence: `uv build --sdist --wheel` succeeded; the built wheel contains
  `looplane/{agent,commands,execution,sandbox,tooling,workspace}/*.py` and the
  new `terminal/{app,projection,conversation_binding}.py`; neither the wheel
  nor the sdist contain `.research`, `__pycache__`, or `.pyc` entries.
- [x] Wave 1's runtime integration boundary is localized to runtime package,
  registry and capability contracts rather than scattered CLI/TUI changes.
  Evidence: external runtime integration (Codex, Pi, Omp, OpenCode) lives
  under `src/looplane/runtimes/` (pre-existing from Slice 1.2, `a90c3c3`) and
  `src/looplane/commands/external.py`; `cli.py`/`tui.py` shrank to composition
  facades in commit `0b1ca42` rather than growing runtime-specific branches.
- [x] Wave 2 preserves permission, timeout and path behavior apart from the
  separately authorized minimal macOS exception.
  Evidence: `tests/test_approval_budget.py`, `tests/execution/` (timeout/
  deadline contracts), and path-bound tests in `tests/test_tools.py` all pass;
  the only permission change anywhere in this work is the documented macOS
  literal-root-read rule in `src/looplane/sandbox/macos.py`.
- [x] Scoped commits and durable raw evidence are recorded in the tracker.
  Evidence: `docs/plans/repository-modularization-progress.md` now cites
  commit hashes `35a43ce`, `0b1ca42`, `2ac3c0f`, `48f2502`, `f656809` per row.
- [ ] Reconcile every plan requirement against the final candidate before marking
  the persistent goal complete; earlier isolated results are not final evidence.
  Not fully closed: this pass reconciled every item in this checklist except
  Linux/Windows platform enforcement evidence (above), which is structurally
  unreachable from this machine. The plan and goal should not be marked fully
  complete until that gap is either closed on a Linux/Windows environment or
  explicitly accepted as an out-of-scope platform limitation by the user.

## Execution authorization

The macOS production policy edit is authorized and applied. On 2026-09-05 the
user explicitly authorized committing the accepted changes and performing the
final requirement audit ("授權你做"). Under that authorization, this checklist
was worked through with reproducible evidence (test runs, greps, a build) as
recorded above, and the pending scoped commits were made (see
`repository-modularization-progress.md`). The one item this authorization
could not close is Linux/Windows platform evidence, which is not collectible
from this machine.

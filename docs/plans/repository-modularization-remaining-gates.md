# Remaining modularization acceptance evidence

This is an acceptance checklist, not a second progress tracker. Current slice
status remains authoritative in
[the execution tracker](repository-modularization-progress.md). An unchecked
item means evidence is still required; it is not an assertion of a defect.

## Integration prerequisites

- [ ] Record the exact candidate scope and its baseline before each gate. Keep
  concurrent implementation outside an isolated candidate unless deliberately
  validating the combined worktree.
- [ ] Preserve the implementation and observed evidence for each slice when a
  dependent slice starts before its predecessor is committed.
- [ ] Retain failed runs, skipped cases and platform limitations alongside passing
  results. A retry does not establish the cause of the original failure.

## Terminal projection and binding: 1.5

- [ ] Canonical App owns composition and input precedence; compatibility imports
  remain available without canonical code importing the old TUI facade.
- [ ] Projection has explicit state/view commands, rather than arbitrary access
  to App internals.
- [ ] Binding owns UI subscription cleanup and generation/lease fences;
  Controller and Store retain runtime and durable lifecycle authority.
- [ ] Existing widget, PTY, event-ordering, cancellation and conversation
  compatibility coverage passes on the integrated candidate.
- [ ] Paired startup/lazy-import measurements meet the plan's threshold.

## Tool execution: 2.1 through 2.3

- [ ] MCP client/discovery/routing ownership is independent of ToolExecutor;
  refresh, duplicate configurations, errors and cleanup retain existing behavior.
- [ ] File reads, traversal and search preserve bounds, containment, symlink
  behavior and the distinct rg/Python-fallback contracts.
- [ ] Complete-read version tracking is shared by editing and snapshot owners;
  stale and truncated reads retain their established behavior.
- [ ] Unified-diff validation and exact replacement preserve path/size limits,
  deadline accounting, atomic writes, rollback bytes/modes and index cleanup.
- [ ] Git/review, authorized verification and transaction orchestration have
  concrete canonical owners; transitional narrow ports do not become a second
  monolithic executor or a reference back to ToolExecutor.
- [ ] ToolExecutor is a composition/registry/dispatch boundary with explicit
  compatibility delegates, not the remaining owner of extracted implementation.

## Agent execution: 2.4 through 2.6

- [ ] State, checkpoint, context and lifecycle ownership preserves serialized
  formats, event sequencing, resume identity, writer leases and active-time
  accounting, including approval waits and cancellation.
- [ ] Model retry/usage/cache and tool scheduling have explicit typed inputs and
  outputs; ordered observations, read concurrency and fingerprint guards remain.
- [ ] Subagent dispatch no longer forms the loop/subagents import cycle,
  including literal dynamic imports. Remove the corresponding test exception
  only when the production dependency has actually been removed.
- [ ] Verification/review, checked-workspace evidence and completion are separate
  owners; final drift checks and terminal/checkpoint ordering are preserved.
- [ ] AgentRunner.run exposes the coordinating state machine. Extracted services
  do not hold the runner, use private-field mixins or import concrete providers.

## Process seam and approved macOS exception

- [ ] Characterize and reconcile remaining UTF-8/output bounds, materialized
  stdin, callback blocking, deadline/cancellation and process-group limits against
  the required process contract. Passing characterization alone is insufficient
  where a required invariant remains unsatisfied.
- [ ] Validate the separately approved literal-root read rule on macOS: process
  startup succeeds, permitted workspace writes succeed, and disallowed access
  remains denied. Preserve the explicit directory-enumeration permission caveat.
- [ ] Update the existing platform-specific strict xfail to reflect the approved
  fix, without deleting or weakening its containment assertions.
- [ ] State Linux/other-platform evidence separately; a macOS run cannot prove
  Linux enforcement or Windows process-tree cleanup.
- [ ] Retain the measured Python decision and its limits. Rust is conditional
  on demonstrated justification, not an unconditional completion requirement.

## Cross-cutting exit evidence

- [ ] Focused tests and compatibility imports cover each changed owner.
- [ ] The full candidate passes repository Ruff and full pytest, with all skips
  and expected failures accounted for rather than hidden in a completion claim.
- [ ] Import-boundary evidence covers canonical facade independence and the
  actual graph, including supported literal dynamic imports.
- [ ] Public-path changes are present in bounded sdist/wheel artifacts.
- [ ] Wave 1's runtime integration boundary is localized to runtime package,
  registry and capability contracts rather than scattered CLI/TUI changes.
- [ ] Wave 2 preserves permission, timeout and path behavior apart from the
  separately authorized minimal macOS exception.
- [ ] Scoped commits and durable raw evidence are recorded in the tracker.
- [ ] Reconcile every plan requirement against the final candidate before marking
  the persistent goal complete; earlier isolated results are not final evidence.

## Execution authorization

The macOS production policy edit is authorized and applied. This checklist does
not itself authorize new test edits, test runs, lint/build commands, validation
or commits. Those actions await explicit authorization under the current working
constraints. Independent production implementation can continue meanwhile.

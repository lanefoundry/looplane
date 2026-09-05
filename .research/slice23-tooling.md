# Slice 2.3 tooling: phase A handoff

Date: 2026-09-05
Status: phase A and phase B source applied and wired; unvalidated. Repository gate pending.

## Scope applied

Only these production files were created:

- `src/looplane/tooling/git.py`
- `src/looplane/tooling/verification.py`
- `src/looplane/tooling/timeouts.py`

This report is the only additional written path. No `tools.py`, `executor.py`,
`transactions.py`, 2.2-owned source, process/sandbox source, tests, tracker or
package export file was modified. No facade imports or executor references were
introduced into these owners.

## Shared limit coordination

No `send_input` or other worker messaging capability is callable in this session.
Coordination therefore used Pasteur's published `.research/slice22-tooling.md`
handoff and one read of the relevant actual 2.2 declarations. This is not a claim
that a direct message was sent or that Pasteur reviewed the new source.

The exact existing shared record types are:

- `tooling.filesystem.OutputLimits`, containing `max_output_chars`.
- `tooling.patch_validation.PatchLimits`, containing `max_patch_bytes`,
  `max_patch_lines`, `max_changed_files`.

`WorkspaceGit` requires both objects and retains their references. No new limit
record, scalar snapshot or default duplicate limit instance is created.
`AuthorizedChecks.output_limits` returns `git.output_limits` directly, so its
output bound has the same mutable source, including if Git's record is replaced.
Integration must supply the same existing records already used by the 2.2 owners,
rather than constructing fresh records from facade scalar attributes.

Accepted 2.2 callable contracts observed:

- `patching.PatchGitCommand`: the new `WorkspaceGit.run` bound method matches the
  argv/stdin/timeout/output/extra-env call shape.
- `patching.PatchReview`: `WorkspaceGit.reviewable_patch` matches the timeout and
  `ReviewablePatch` result shape.
- `snapshots.ResetSnapshotIndex`: `WorkspaceGit.reset_paths` matches paths and
  timeout; it returns the `CommandResult` for the existing snapshot result policy.

No 2.2 port definitions or wiring were changed. `git.py` also exposes the proposed
method-shaped `GitCommands`, `PatchReview` and `IndexReset` consumer interfaces;
there are no adapter/proxy instances. Later integration can use the existing
bound-method seams directly before deciding whether to consolidate their types.

## Concrete owners

`WorkspaceGit` owns the bounded Git invocation and ordinary/pinned review and
fingerprint implementations, with explicit command/environment/clock/ID seams.
It gets workspace from the shared policy, normalizes task-home/Git-directory
configuration, and retains base SHA and preexisting dirty-path exclusions.
`reset_paths` is the narrow sorted-path Git reset operation; its caller retains
the decision to reset and how to handle failure.

`AuthorizedChecks` owns command-registry validation and the one outcome ledger.
It consumes a concrete `WorkspaceGit`, exact configured commands, normalized
verification sandbox settings, and explicit process/environment/sandbox/clock/
output-bounding dependencies. It executes already-authorized named checks; it
does not choose checks, request approval, schedule verification or decide agent
completion. Its public registries are `commands` and `outcomes`; integration must
expose the historical facade names without copying those maps.

`effective_timeout` preserves the original default/override cap and exhausted
harness-budget error. No common deadline manager or process lifecycle Protocol
was introduced. `GitProcess` and `VerificationProcess` describe callable
substitution seams for the existing synchronous local function, not new process
implementations.

## Preservation intent from the known frozen implementation

Source baseline: `.research/slice21-frozen/tools.py`, whose selected method bodies
were already available from the design task. They were not reread during this
phase, apart from locating the previously omitted secret-helper import. The
new-owner inputs from 2.2 were read once. Production extraction was applied in one
patch phase without a subsequent review or corrective pass.

- Preserve argv and stage order, sanitized environment then trusted extra-env
  merge, optional explicit-Git-directory prefix, 30-second Git cap and output
  fallback expression.
- Preserve ordinary content-before-name review; pinned base index, force-add,
  whole dirty-path exclusion before policy checks and filtered cached diff;
  fingerprint HEAD seed, non-force add and write-tree. Both temporary indexes
  retain their `finally` unlink behavior and independent unique names.
- Preserve per-review/fingerprint monotonic deadlines and exhaustion text, byte/
  line/file truncation rejection, sorting/NUL names, and policy checks.
- Preserve exact named-check validation and the special argv tuple
  `('git', 'diff', '--check')` routed through Git with its additional timeout cap.
- Preserve sandbox construction and only the existing auto-backend, return-126,
  macOS/generic-unavailable prefix fallback. Reuse the same effective timeout on
  fallback. Linux-unavailable and SIGABRT are not additional fallback cases.
- Preserve duration measurement, configured-timeout message, scanning both raw
  streams, secret redaction and finding labels, final output bound, and storing
  both passed and failed outcomes only after evidence construction.

These are implementation intent and source-transfer details, not a claim of
verified behavioral equivalence. The preexisting verification fallback remains
a specific exception to the low-level runner's fail-closed sandbox contract.

## Unintegrated work and phase B boundary

The existing active executor still owns and executes its old Git/check methods;
these new files are not imported into that path by this phase. Slice 2.3 is not
complete merely because these owners now exist.

After main authorizes stable 2.2 integration:

1. Construct `WorkspaceGit` with the exact existing policy/output/patch records,
   task home and Git/base/dirty configuration; wire the 2.2 bound-method seams.
2. Construct `AuthorizedChecks` and expose its single registry/ledger through
   compatibility names. Carry historical monkeypatch hooks inward through the
   explicit constructor seams, never through facade backimports.
3. Extract transactions and canonical executor only under separate phase B
   authorization. Preserve the program/transaction ordering and timeout ledger
   in `.research/slice23-design.md`.
4. Separately authorize relevant tests, compatibility/import checks, lint and
   repository gates. No prior slice pass count is evidence for these files.

No tests were edited or run; no lint, formatting, build, syntax/import checks,
Git, validation, postwrite source reads or re-review were performed. Main owns
integration, validation, tracker updates and commit decisions. No web research
or private payload transfer was needed.

## Phase B completed production handoff

Main authorized full Slice 2.3 after phase A and relayed Pasteur's stable
shared-record interfaces and source freeze. Pasteur confirmed successful cp of
pre-2.3 tools.py and the seven 2.2 leaves under .research/slice22-frozen/.
That preserved snapshot was used; no Git snapshot operation or live-source
polling was required. The phase-A sections above describe the earlier checkpoint;
the status and integration details in this section supersede its unintegrated
state and deferred phase-B work.

Additional applied production paths:

- src/looplane/tooling/transactions.py: concrete StructuredPrograms and shared
  mutable ProgramLimits.
- src/looplane/tooling/executor.py: canonical ToolExecutor composition, registry,
  compatibility delegates and observation dispatch.
- src/looplane/tools.py: explicit legacy imports and a narrow compatibility
  subclass; no Git/check/transaction algorithm remains here.

Phase A git.py, verification.py and timeouts.py were wired without another source
edit or reread. No 2.2 leaf required modification: its existing callable
interfaces accept the concrete owner methods directly.

### Concrete canonical wiring

- WorkspaceGit receives the executor's existing _output_limits and _patch_limits
  objects; no numeric copies or duplicate record instances are passed downstream.
- AuthorizedChecks uses that WorkspaceGit and its output-limit property. Historical
  verification_commands and verification_outcomes are properties onto its one
  command registry and outcome ledger, including replacement setters.
- PatchOperations receives git.run, git.reviewable_patch and atomic_writer.replace.
- WorkspaceSnapshots receives git.reset_paths and atomic_writer.replace.
- StructuredPrograms receives concrete files/search/patch/validator/snapshot/Git/
  check owners, the same output record, and one ProgramLimits record. The public
  max_tool_program_steps property delegates into that record; validation remains
  constructor-owned, like the accepted 2.2 max_* properties.
- No canonical Git/review/reset/atomic callback closes over ToolExecutor. The
  static process/environment/sandbox/clock/bounds/ID hooks are function objects,
  not methods retaining a composition instance. The existing MCP client factory
  is consumed during McpBridge construction, as in the accepted 2.1 contract.

### Structured behavior carried forward

The implementation was transferred from the frozen source, with owner references
and fixed operation dispatch substituted for executor methods:

- Programs permit only list/read/search/diff; transactions permit only read,
  create/replace/apply/check/diff. No MCP, shell or caller-configurable handler
  registry is introduced.
- Preserve top-level validation, both-branch touched-path discovery, sorted
  snapshots before the rollback try, execution-time depth/leaf budgets, and
  previous full rendered output as conditional input.
- Preserve per-leaf timeout forwarding, outcome failure before transcript append,
  original caught exception sets, restoration order and rollback exception
  chaining. Snapshot coverage and verification evidence limitations remain as
  documented in slice23-design.md.
- The old private arbitrary-handler _execute_structured_steps implementation is
  replaced by StructuredPrograms.execute_steps with a fixed program/transaction
  mode. It is not retained as an extensible handler-map API. Existing public
  program/transaction entry points and private nested/touched/snapshot delegates
  are retained.

### Compatibility and intended evidence boundary

The tools.py subclass supplies explicit dynamic wrappers for historical
run_bounded_command, environment, sandbox resolution, clock, executable lookup,
bounded text, UUID and MCP-client construction globals. Canonical executor
defaults use canonical modules only. The facade's dependency functions retain
neither an executor instance nor an application service registry.

Important public operation signatures, max_* properties, type/error exports,
MCP mapping accessors, and thin Git/review/fingerprint/snapshot/edit delegates
remain. Direct atomic-helper calls retain the static signature. Canonical
operations now use the concrete writer/Git owner rather than an executor's
private method callback; arbitrary instance monkeypatches of those former
callback targets have not been characterized as a supported extension API.
The observed module-level fsync, process/sandbox and MCP patch targets informed
the explicit facade wrappers. Tests were read for those targets, not executed.

The retained dispatch body came from the frozen source; its bounded observation
construction uses the explicit bound hook. There are no dynamic facade imports,
module replacement tricks, private-self mixins or generic coordinator/proxy
objects in this implementation.

### Final status and pending gate

The one phase-B source-application patch also updated this handoff. No written
source was reread, reviewed or corrected afterward. No tests were changed or run,
and no lint, formatting, build, import/syntax checks, Git or validation commands
were run. Source application is not proof of correctness or a passed gate.

No confirmed implementation bug or command blocker was identified during this
bounded application. Compatibility, import direction, formatting, transaction
contracts and real Git/verification behavior still require the separately
authorized gate. Existing design limitations, including fallback and per-leaf
timeout semantics, are intentionally not repaired by this extraction.

Main has the full applied Slice 2.3 source and handoff for integration review and
subsequent gate authorization. No queued instruction is awaiting handling and
no capacity issue blocked delivery. No staging or commit was performed.

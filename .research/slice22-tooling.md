# Slice 2.2 filesystem, search and patching

Status: production implementation applied; unvalidated. Main integration gates are pending.

## Preserved pre-Slice 2.2 snapshot

The first filesystem action used cp to preserve the exact then-current files at:

- `.research/slice21-frozen/tools.py`
- `.research/slice21-frozen/mcp_bridge.py`

No Git command or snapshot verification was performed. The copy commands returned exit 0.

## Applied source paths

- `src/looplane/tools.py`
- `src/looplane/tooling/filesystem.py`
- `src/looplane/tooling/search.py`
- `src/looplane/tooling/patch_validation.py`
- `src/looplane/tooling/read_versions.py`
- `src/looplane/tooling/snapshots.py`
- `src/looplane/tooling/patching.py`

`tooling/mcp_bridge.py`, its tests, the Slice 1.2 sources/tests, and all other tests were untouched. No Slice 2.3 Git-review, verification, or transaction orchestration extraction was started.

## Explicit owners

`SafePathPolicy` remains the existing shared policy owner. Each extracted consumer uses that policy directly; no replacement authorization or symlink policy was introduced.

`ReadVersionStore` owns the single complete-read hash dictionary. Files record complete reads, exact replacement checks unread/stale content through it, and snapshot restoration records or forgets restored paths. The historical `_read_versions` accessor points at this store.

`WorkspaceFiles` owns sorted traversal, bounded list/read and complete-read recording. `WorkspaceSearch` owns rg execution and the existing Python fallback using the file owner's public walk operation. Separate output/read/search limit records are shared with the composition root.

`UnifiedDiffValidator` owns the hunk expression, header parsing, forbidden metadata/path validation and patch byte/line/file bounds. `PatchLimits` is shared with the facade and patch operations.

`AtomicFileWriter` owns exclusive temporary writes, atomic replacement, mode/fsync and cleanup, with an explicit ID factory. `WorkspaceSnapshots` owns capture/restore of the existing canonical `_PathSnapshot` values, version-store synchronization and a narrow index-reset callback.

`PatchOperations` owns create-file patch generation, apply/check, intent-to-add handling, exact replacement and operation rollback. It consumes named typed Git, review and atomic-write callbacks, the shared validator/version store and an explicit timeout/clock dependency.

No extracted owner imports ToolExecutor or the tools/runtime facades, stores an executor attribute, or inherits executor methods through a mixin. Cross-boundary Git/review/reset/atomic operations are composition-supplied callbacks. Those compatibility callbacks are defined in ToolExecutor and dynamically resolve its existing operations; they are narrow callback dependencies rather than a new Git/review implementation. The callback closures retain the composition instance, which should be distinguished from claiming total object-graph independence.

## Canonical execution and retained composition

New file/search code imports canonical bounded capture, environment and local-process primitives. Search defaults use `execution.local_process.run_local_process` and canonical CommandResult. The compatibility composition injects dynamic command/environment/availability callbacks so existing tools-level patch points remain available without reverse imports.

ToolExecutor retains public construction, dispatch, built-in/MCP definition composition, Git invocation/review/fingerprinting, verification and structured transaction orchestration. Its file/search/validation/edit/snapshot methods forward to the new owners. Its mutable max_* attributes delegate into shared records so later changes do not leave the extracted owners with stale copied limits. Original imported-module patch targets are retained as explicit compatibility exports.

The original method bodies were transferred mechanically with dependency/state references changed to their owners. This was a single application phase, with no subsequent source reading or corrective pass.

## Intended behavior preservation, not yet verified

- Same path-policy checks, sorted traversal, read/sample/output limits and truncation behavior.
- Complete original bytes supply read hashes; truncated reads do not update the ledger.
- Separate rg/Python fallback behavior, exact command construction and existing timeout/environment behavior.
- Same diff grammar, byte/line/file limits, create-file quoting and no-newline handling.
- Same apply/check/review ordering, intent-to-add and reverse/reset rollback branches.
- Same exact-replacement preconditions, tracked-file check, content/mode rollback verification, exception text and chaining.
- Same atomic write/fsync sequence, snapshot capture behavior, reset-before-restore ordering and version updates.
- Same existing canonical value/error types and MCP bridge implementation.

These statements describe the implementation intent and body-transfer approach. No tests or review were performed to establish behavioral equivalence.

## Execution evidence and pending gates

The source-application script returned exit 0. This confirms that the application command completed; it is not syntax, import, lint or behavior validation.

Per the active instructions, no tests were modified or run, and no lint, formatting, build, Git, verification, diff review or re-read of written source was performed. No fresh pass count is available. The prior Slice 2.1 result of 126 passing tests predates this implementation and must not be treated as a Slice 2.2 result.

Pending, for a subsequently authorized gate run:

- Syntax/import and Ruff checks for all changed sources; formatting has not been run.
- Existing policy/tools/MCP/tooling integration tests and compatibility patch contracts.
- New owner-level characterization tests proposed in `.research/slice22-design.md`; none were written in this task.
- Shared read-version transitions, exact replacement, cumulative review and snapshot/atomic rollback fault cases.
- Canonical import boundaries and no-new-SCC architecture gates.
- Main's full test/build/package gates and scoped integration/commit.

No command-level blocker occurred. Implementation application and this handoff are finished; correctness and architecture gate results remain unknown until validation is authorized. No staging or commit was performed, and no web tools were used.

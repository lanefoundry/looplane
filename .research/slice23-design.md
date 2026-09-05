# Slice 2.3: Git, authorized verification, and structured transactions

Status: design only, 2026-09-05. No production/test edits, tests, lint, build, Git
commands or validation were performed. Pasteur's active `tools.py` was not read.
The available `.research/slice21-frozen/tools.py` supplied the implementation
baseline; `.research/slice22-design.md` supplied the proposed 2.2 owner interfaces.
The initial combined source output was truncated; a bounded recovery extracted
only the missing Git/verification/constructor declarations from that frozen file.
There was no repeated polling or inspection of the active implementation.

This design follows `repository-modularization-plan.md`, Slice 2.3 and its
dependency rule. It is not confirmation that Pasteur's eventual 2.2 implementation
uses every proposed spelling below. At implementation handoff, reconcile the
accepted 2.2 API once; do not overwrite its work to match a design-only document.

## Decision and exact canonical files

Create these five files for Slice 2.3:

| File | Owner and responsibility |
|---|---|
| `src/looplane/tooling/git.py` | `WorkspaceGit`: bounded Git invocation, ordinary/pinned review, fingerprint index lifecycle and path-index reset. Define the narrow Git/review/reset consumer interfaces here. |
| `src/looplane/tooling/verification.py` | `AuthorizedChecks`: named command registry, already-authorized execution, output redaction and `VerificationOutcome` ledger. |
| `src/looplane/tooling/transactions.py` | `StructuredPrograms`: the existing bounded program/transaction language, fixed operation allowlists, touched-path discovery and transaction rollback. |
| `src/looplane/tooling/timeouts.py` | Pure `effective_timeout(default, override)` function preserving the current cap/error behavior. No deadline manager or scheduling framework. |
| `src/looplane/tooling/executor.py` | Canonical `ToolExecutor`: construction/ownership, definitions, dispatch and conversion to `ToolObservation`, with thin existing method delegates. |

`src/looplane/tools.py` becomes the compatibility entry point after the owned
behavior is extracted. Existing `tooling/types.py`, definitions and MCP bridge
retain their owners. Existing domain contracts remain in `contracts.py`.

The existing 2.2 `patching.py` and `snapshots.py` require only dependency wiring and
calls to the final Git interfaces. Do not reimplement their file mutation, atomic
write, validation, read-version or snapshot behavior. Filesystem/search owners
likewise remain independent. No `coordinator.py`, arbitrary operation proxy,
generic service locator, private-state mixin or replacement all-tools executor is
introduced.

## Canonical dependency and construction graph

```text
tools.py compatibility -> tooling/executor.py composition and dispatch
executor -> McpBridge + WorkspaceFiles + WorkspaceSearch + PatchOperations
         + WorkspaceGit + AuthorizedChecks + StructuredPrograms
WorkspaceGit -> execution + SafePathPolicy + tooling types + timeout helper
AuthorizedChecks -> WorkspaceGit + execution + sandbox policy + secret primitives
StructuredPrograms -> 2.2 file/search/patch/snapshot/validator owners
                   + WorkspaceGit + AuthorizedChecks
PatchOperations -> GitCommands + PatchReview
WorkspaceSnapshots -> IndexReset
```

There is no reverse dependency from Git to patch operations, snapshots, programs,
verification, the executor, agent decisions or a compatibility facade. Sharing
`SafePathPolicy`, a read-version store and accepted limit records does not require
sharing private executor state.

Construct the shared path policy/read-version/limit owners first; then Git, atomic
writer and snapshots, file/search/patch owners, authorized checks, and structured
programs. Finally compose the built-in definitions and MCP mappings. This makes
the dependency direction real rather than repairing cycles with lazy backimports.

## Replace the 2.2 transitional interfaces

These signatures are the proposed final consumer contracts, defined alongside
their concrete implementation in `tooling/git.py`:

```python
class GitCommands(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        stdin: str | None = None,
        timeout_seconds: float | None = None,
        max_output_bytes: int | None = None,
        extra_env: Mapping[str, str] | None = None,
    ) -> CommandResult: ...


class PatchReview(Protocol):
    def reviewable_patch(
        self, *, timeout_seconds: float | None = None,
    ) -> ReviewablePatch: ...


class IndexReset(Protocol):
    def reset_paths(
        self, paths: Sequence[str], *, timeout_seconds: float = 5.0,
    ) -> CommandResult: ...
```

`WorkspaceGit` implements all three and additionally exposes
`workspace_fingerprint(*, timeout_seconds=None) -> str` and
`git_diff(*, timeout_seconds=None) -> str`. The latter simply returns review
content. `reset_paths` executes `reset --quiet HEAD -- <sorted paths>`; snapshot
restoration keeps ownership of whether to call it and how its result is handled.

These are separate consumer views of one concrete owner, not three forwarding
objects. Their substitution boundary is specific: patch tests can script Git and
review failures; snapshot tests can inject reset failure without implementing a
whole executor or acquiring unrelated mutation powers. Do not add a `ProcessRunner`
Protocol as a side effect. Canonical defaults call the existing
`execution.local_process.run_local_process` function.

| 2.2 transitional seam | 2.3 replacement |
|---|---|
| Bound `_git`/Git callable supplied by executor | `GitCommands.run` supplied by the constructed `WorkspaceGit` |
| Bound `reviewable_patch` callable supplied by executor | `PatchReview.reviewable_patch` on that same instance |
| Bound index-reset callback | `IndexReset.reset_paths`, same instance |
| Shared timeout cap formerly reached through executor | Pure `effective_timeout` helper |

Bind owners directly. Do not retain `ToolExecutor` inside patch/snapshot objects,
create a generic callable dictionary, or dynamically mirror facade globals into
canonical modules. If 2.2's accepted implementation already has structurally
equivalent typed ports, rename/relocate those definitions rather than creating
parallel interfaces. The implementation may retain direct bound-method injection
where that is already the accepted explicit seam, but its provider becomes
`WorkspaceGit`, not the executor.

## Git ownership and invariants

Frozen-source anchors: `_git` line 618, `reviewable_patch` line 920,
`workspace_fingerprint` line 969, `_reviewable_patch_pinned` line 1031.

`WorkspaceGit` receives explicit workspace, policy, optional Git directory/base
SHA, preexisting dirty-path set, task home, and relevant shared limit records.
Keep constructor alias precedence and public mutable limit compatibility in
executor composition. Patch byte/line/file limits must have one shared source
across patch validation and final review, not separate stale copies.

Preserve the following behavior:

- `_git` uses exact argv and `shell=False` through the canonical process function.
  With an explicit Git directory only, prepend `--git-dir`, `--work-tree`,
  `-c core.fsmonitor=false`, `-c core.hooksPath=/dev/null` in the existing order.
  Do not silently apply that prefix/hook behavior to the ordinary repository path.
- Construct the sanitized task-home environment first, then apply trusted
  `extra_env`; this is how private indexes set `GIT_INDEX_FILE`. That mapping is
  internal, never a model-provided environment escape hatch.
- Retain the 30-second Git cap and `max_output_bytes or max_output_chars`
  selection, including existing zero/falsy behavior. Do not reinterpret the
  historical output-limit name as a new character-count contract.
- Ordinary review performs content diff before names diff. It uses
  `--no-ext-diff --no-textconv --no-color --no-renames`, retains NUL-delimited
  names, sorting and policy checks, and rejects truncated content/names or exceeded
  byte/line/file limits. It is not expanded to include all staged/untracked changes.
- Pinned review resolves the Git directory, creates a uniquely named review index,
  initializes it from `base_sha`, and stages using `add -A -f -- .`. It computes
  cached changed names, removes **entire preexisting dirty paths**, checks remaining
  paths/limits, and generates content only for those paths. The real index is not
  the review index. No partial reconstruction of preexisting dirty files is added.
- Empty pinned changed-path results return an empty review before content diff.
  Never check excluded dirty paths against allowed paths or include them in the
  final changed-file cap; that would change existing semantics.
- Fingerprinting uses its own unique index, `read-tree HEAD`, `add -A -- .`
  **without `-f`**, then `write-tree`. It includes tracked and non-ignored untracked
  state and executable-bit changes, and is not limited to allowed edit paths or
  filtered by preexisting dirt. Do not reuse pinned-review indexing semantics.
- Both temporary-index operations unlink the generated index in `finally`.
  Preserve current cleanup/exception precedence. Do not introduce broader index
  cleanup or pretend arbitrary preexisting real-index state is backed up by this.

A shared helper for the identical Git-directory lookup is reasonable. A generic
temporary-index transaction abstraction is unnecessary: review and fingerprinting
have different seeds, force-add rules, filtering and evidence semantics.

## Already-authorized verification

Proposed public API:

```python
class AuthorizedChecks:
    commands: dict[str, VerificationCommand]
    outcomes: dict[str, VerificationOutcome]

    def run_check(
        self, name: str, *, timeout_seconds: float | None = None,
    ) -> VerificationOutcome: ...
```

Constructor inputs are explicit command entries, workspace/task home, `WorkspaceGit`,
the output limit and verification sandbox configuration. If grouped, use a narrow
immutable `VerificationSandboxSettings` record in `verification.py` containing
exactly enabled/profile/backend/read_roots. Do not pass the executor or a generic
configuration bag. Expose the single registry/outcome maps through compatibility
properties; do not duplicate ledger state in the executor.

The plan's authorization boundary is substantive: this owner executes the named
allowlisted command after the caller's authorization path. It does not request
approval, decide when verification is due, choose checks, mark an agent complete,
or clear approval state. Registry membership is not a new authorization token or
proof that a check's subprocess can only affect `allowed_paths`.

Frozen-source anchor: `run_check` line 842; constructor line 54. Preserve:

1. Build the registry with the existing exact argv/name/positive-timeout validation
   and duplicate-name rejection. Unknown names fail before any process invocation.
2. Compute the effective command timeout. Only the **exact** argv tuple
   `('git', 'diff', '--check')` uses the canonical Git boundary and its additional
   30-second cap. It does not newly receive the generic verification sandbox.
3. Other entries use the exact configured argv, sanitized task-home environment,
   workspace cwd, output cap, and the existing requested sandbox configuration.
4. Preserve the wrapper's existing fallback literally: when a requested sandbox
   has backend `auto`, result code is 126, and stderr starts with the macOS
   unavailable or generic OS-unavailable prefix, it retries without a sandbox.
   Linux-unavailable errors, SIGABRT and other failures do not match. This is a
   preexisting verification-layer exception to the lower runner's fail-closed
   behavior, not a reason to expand fallback or claim universal fail-closed checks.
5. Measure duration around the existing execution path, derive status from
   `CommandResult.ok`, scan both raw streams for secrets, redact emitted output,
   append the same findings labels, then apply the final output bound.
6. Preserve `VerificationOutcome` fields and formatting, including the timeout
   message using the configured timeout rather than the effective harness cap.
   Store the outcome by name before returning, including failed outcomes.
   Earlier exceptions do not create a new synthetic outcome.

The separately approved macOS literal-root change belongs to the canonical policy
owner and main's separate workflow. Slice 2.3 consumes the accepted sandbox API;
it must not duplicate that rule, change its scope, or fold xfail removal into this
extraction without authorization.

## Timeout ledger: do not silently repair behavior

| Operation | Existing timing rule |
|---|---|
| `effective_timeout` | `None` keeps default; nonpositive override raises `harness timeout budget is exhausted`; otherwise minimum of default/override. |
| One Git invocation | At most 30 seconds, capped by supplied override. |
| Review or fingerprint | One monotonic deadline for that operation; every Git stage receives remaining time, preserving operation-specific exhaustion text. |
| One named check | Configured timeout capped by override; exact Git check also passes through the Git cap. |
| Unavailable-sandbox retry | Reuses the same effective timeout; it does not subtract the failed first attempt. |
| Structured program/transaction | Forwards the original supplied timeout independently to each eligible leaf; no shared decreasing deadline currently exists. |
| Snapshot/index or patch rollback | Existing independent five-second cleanup budgets remain independent of exhausted forward-work budgets. |

The canonical helper should not turn all these operations into a common deadline
object. A whole-program or retry-wide deadline would be a separate behavioral fix.

## Structured programs and transaction ordering

Use one concrete `StructuredPrograms` owner in `transactions.py`, with two public
entry points:

```python
def tool_program(
    self, steps: Sequence[Mapping[str, Any]], *,
    timeout_seconds: float | None = None,
) -> str: ...

def tool_transaction(
    self, steps: Sequence[Mapping[str, Any]], *,
    timeout_seconds: float | None = None,
) -> str: ...
```

Its collaborators are the explicit 2.2 file/search/patch/validator/snapshot owners,
`WorkspaceGit`, and `AuthorizedChecks`. It owns only the existing structured
language and transaction envelope. It cannot dispatch arbitrary tool names,
discover MCP tools, inspect executor state or call back into `ToolExecutor`.

Keep the existing control-flow evaluator private to this domain owner. Select
between two fixed operation sets with a typed internal
`Literal['program', 'transaction']` mode and explicit operation branches, rather
than accepting a caller-supplied `Mapping[str, Any]` handler registry. The operation
result is `str | VerificationOutcome`. `Any` remains only at the existing untrusted
argument boundary; it is not a service interface.

- Read-only programs permit only `list_files`, `read_file`, `search_text`, `git_diff`.
- Transactions permit only `read_file`, `create_file`, `replace_text`, `apply_patch`,
  `run_check`, `git_diff`. Do not add list/search/MCP, nested transactions or free
  shell execution because another owner happens to expose a method.
- Preserve dynamic argument binding/type failures and their timing. Introducing
  whole-program typed DTO validation at entry would change which earlier steps
  execute and which transaction errors receive rollback wrapping.

The required transaction sequence is:

```text
top-level sequence/nonempty/count validation
  -> discover touched paths recursively
  -> capture snapshots using the existing 2.2 snapshot owner
  -> begin rollback-protected execution
  -> evaluate selected branches/repetitions and execute leaf operations in order
  -> return bounded transcript on success
  OR restore snapshots on the existing caught exception set
  -> raise the existing rollback-success or rollback-failure error
```

Specific invariants:

- Touched-path discovery resolves create/replace paths through the same policy and
  validates apply-patch targets with the same validator. It traverses repeat bodies
  and **both** conditional branches before execution, deduplicates and sorts paths.
  Thus invalid paths/patches in an untaken branch can still reject a transaction.
- Discovery and snapshot capture occur outside the rollback `try`. Do not wrap
  their failures as successful rollback, defer snapshots until mutation, or
  eagerly move all executable-step validation before capture.
- Runtime evaluation validates the selected branch only, except for transaction
  preflight just described. Maximum depth is checked on execution at depth > 3;
  preflight currently has no equivalent depth guard. Preserve rather than quietly
  claiming a bounded preflight recursion guarantee.
- The outer length cap counts top-level entries. The execution budget counts leaf
  operations, not control nodes. Repeat count is a positive `int` up to the step
  cap; existing `isinstance(True, int)` behavior is not tightened incidentally.
- `if_contains` uses the previous leaf's full rendered content, initially empty,
  not its transcript-truncated version. Emit the existing branch marker before
  consuming the selected branch. Keep leaf numbering and section ordering.
- Reject model-supplied `timeout_seconds` at both outer dispatch and leaf argument
  boundaries. Pass the harness override only to the same timed operations.
- A failed `VerificationOutcome` is stored by `AuthorizedChecks`, then evaluation
  raises the existing verification-failed error before appending that leaf's
  transcript section. Transaction restoration does not clear verification
  outcomes or restore other evidence state.
- Catch exactly `PathPolicyError`, `ToolExecutionError`, `OSError`, `TypeError`,
  `UnicodeError` for forward execution. Rollback failure handling catches exactly
  `PathPolicyError`, `ToolExecutionError`, `OSError`. Preserve exception chaining
  and both `tool transaction failed ...` messages; do not add a blanket catch.
- Restore resets the index before restoring files, uses the independent cleanup
  budget, preserves mode/bytes, and updates the shared read-version store using
  the accepted 2.2 behavior. Nonexistent originals are unlinked and forgotten.
  Keep the existing handling of a nonzero reset result versus a raised exception.
- Transaction snapshots cover declared edit paths, not arbitrary filesystem
  effects of a verification command. They do not restore the entire repository,
  arbitrary preexisting staged state, directories, or all check artifacts.
- Per-patch cumulative review and local rollback remain owned by PatchOperations.
  The transaction envelope then handles its original wider touched-path snapshot.
  Do not add a new final cumulative-review call, implicit check or fingerprint
  invalidation after every transaction.

No generic workflow engine, callback proxy, configurable coordinator or alternate
transaction API is required to implement these four existing domain methods.

## Thin ToolExecutor end state and compatibility

Canonical `tooling/executor.py` retains:

- Constructor argument compatibility, workspace/policy agreement, limit alias
  precedence, dependency ownership and explicit public compatibility properties.
- Built-in definitions/allowlist enum assembly and unchanged MCP bridge ownership,
  factory injection, discovery/refresh/close entry points.
- Built-in and MCP dispatch, untrusted argument checks, harness timeout ownership,
  bounded exception/result conversion and `ToolObservation` construction.
- One-call public delegates for existing file/search/patch/Git/check/program and
  transaction APIs. `verification_commands` and `verification_outcomes` refer to
  the authorized-check owner's maps; `_read_versions` refers to the shared store.

It no longer implements Git argv/index logic, secret formatting for check evidence,
subprocess launch policy, structured recursion, snapshot traversal or rollback.
Construction may name concrete owners; feature owners may not name this executor.

Keep old public imports and important private delegates explicitly in `tools.py`.
Any legacy monkeypatch adaptation must flow inward through a named constructor or
factory seam; canonical defaults import canonical modules and never the facade.
Retain the already characterized MCP client construction hooks from Slice 2.1.
Do not assume the necessary private patch surface without consulting the accepted
2.2 handoff and existing tests at implementation time. This task did not inspect
or run the test suite to establish a new compatibility inventory.

Avoid making a forwarding-only subclass the canonical implementation. A narrow
compatibility subclass/delegate at the old path can be justified for historical
patch targets, but it must not restore executor callbacks as the canonical Git,
review or transaction dependencies. Moving only public names while leaving all
operations in `tools.py` does not satisfy the slice.

## Implementation handoff and future evidence, not executed here

1. Receive Pasteur's stable 2.2 API/ownership handoff. Map its Git/review/reset seams
   to the contracts above and identify shared limit/version owners once.
2. Extract `WorkspaceGit` with exact commands and error/timing order, then connect
   patch and snapshot consumers directly. Preserve ordinary/pinned/fingerprint
   distinctions before considering helper deduplication.
3. Extract `AuthorizedChecks`, preserving allowlist validation, evidence ledger,
   secret redaction and the precise existing fallback. Keep agent authorization
   and verification scheduling above this owner.
4. Extract the fixed structured language and rollback envelope. Preserve preflight,
   snapshot, leaf execution and error-wrapping order rather than redesigning the DSL.
5. Complete canonical executor ownership and explicit compatibility imports. Main
   then authorizes/runs scoped evidence and repository gates separately.

Candidate future test owners: `tests/tooling/test_git.py`,
`tests/tooling/test_verification.py`, `tests/tooling/test_transactions.py`.
Required cases include temporary-index failures/cleanup and real-index preservation;
pinned dirty exclusions versus fingerprint ignored-file behavior; named-check
allowlist, timeout/fallback matrix and redaction; both-branch preflight, leaf budget,
snapshot/reset/restore ordering, rollback-failure chaining, failed-check ledger
retention and unlisted-check-side-effect limitations. Preserve existing integration
and private-patch contracts. Canonical import and thin-facade checks belong to the
authorized implementation gate, not this design task.

No implementation readiness, test result, lint status or full contract completion
is claimed by this report. The known retry/program timing, verification fallback,
snapshot coverage and dirty-file exclusion limitations require separate deliberate
decisions if the intended product contract is stronger than the frozen behavior.

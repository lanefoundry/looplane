# Slice 2.2 filesystem, search and patching design

Status: design only. Implementation awaits main authorization after the separate Slice 2.1 snapshot/commit.

Only this report is written for this task. Slice 1.2 and Slice 2.1 sources/tests remain frozen. No source edits, tests, formatting, staging or commits were performed.

## Canonical scope

The plan requires bounded file walking/read/search, unified-diff validation, exact replacement, rollback and snapshots, with shared deterministic containment/symlink guards. Slice 2.3 retains Git review/fingerprinting, verification and structured transaction orchestration. Slice 2.1 MCP ownership stays unchanged.

The extraction must create independent owners and typed dependencies. Moving methods into a class that holds ToolExecutor, a private-field mixin, a bag of arbitrary callables, or another combined executor is not the proposed implementation.

## Proposed owners and files

| Owner | Proposed file | State and responsibility | Dependencies |
|---|---|---|---|
| SafePathPolicy, existing | `policy.py`, unchanged | Workspace root, allowed paths, lexical path guards and resolved containment | pathlib/glob only |
| ReadVersionStore | `tooling/read_versions.py` | One relative-path-to-SHA256 map; record, require-current, forget | hashlib, canonical ToolExecutionError |
| WorkspaceFiles | `tooling/filesystem.py` | Sorted bounded traversal, read limits, complete-read version recording | existing policy instance, version store, explicit limits, canonical bounded_text |
| WorkspaceSearch | `tooling/search.py` | rg invocation and current Python fallback semantics | same policy/files owner, search limits, typed command callback and task environment |
| UnifiedDiffValidator | `tooling/patch_validation.py` | Header/hunk parsing, size/count validation and allowed-path checks | same policy instance, patch limits; no process/session state |
| AtomicFileWriter | `tooling/snapshots.py` | Exclusive temporary files, atomic replacement, mode/fsync and cleanup | explicit ID factory; OS primitives |
| WorkspaceSnapshots | `tooling/snapshots.py` | Capture/restore bytes, existence and mode; synchronize read versions during restoration | same policy, version store, atomic writer; narrow index-reset port |
| PatchOperations | `tooling/patching.py` | Create-file patch construction, apply/check flow, exact replacement, cumulative-review checks and operation rollback | validator, policy, version store, atomic writer, typed Git/review ports, explicit limits/clock |
| ToolExecutor, retained | `tools.py` | Composition, dispatch, compatibility delegates and later-slice orchestration | these explicit owners plus unchanged MCP bridge |

Keep `ReviewablePatch`, `_PathSnapshot` and `ToolExecutionError` in their existing canonical `tooling/types.py` owner. Preserve exact re-exports and pickle identities; do not introduce a duplicate snapshot dataclass. Snapshot mappings belong to the caller's individual transaction, not a global bridge cache.

No new path-policy class should duplicate SafePathPolicy. Add a helper only when both extracted consumers need a shared existing operation; do not broaden policy or turn every filesystem operation into a generic service.

## Explicit APIs and ownership

ReadVersionStore:

- `record(relative_path: str, content: bytes) -> None` records the full-byte hash.
- `require_current(relative_path: str, content: bytes) -> None` preserves distinct unread/stale errors.
- `forget(relative_path: str) -> None` removes a nonexistent restored target.
- A compatibility accessor for `ToolExecutor._read_versions`, if retained, refers to this single dictionary. Owners never refer to that private executor field.

WorkspaceFiles:

- `walk(root: Path) -> Iterator[Path]`, `list_files(path: str = '.') -> str`, and `read_file(path: str) -> str`.
- Own only file-read/list behavior and its limit values; share the version store explicitly with editing and snapshot owners.

WorkspaceSearch:

- `search_text(query, path='.', glob=None, case_sensitive=True) -> str`.
- Consume WorkspaceFiles.walk through its public API, plus a narrow command callable returning canonical CommandResult. No method-dispatch object or executor reference.

UnifiedDiffValidator:

- `validate(patch: str) -> tuple[str, ...]`, with header helpers owned locally.
- Return sorted target paths, leaving all execution and rollback to PatchOperations.

WorkspaceSnapshots:

- `capture(paths: Sequence[str]) -> dict[str, _PathSnapshot]`.
- `restore(snapshots: Mapping[str, _PathSnapshot]) -> None` preserves index-reset-before-write ordering and existing version-store changes.
- The narrow reset dependency receives only sorted path names and the established five-second budget. This does not transfer all Git or transaction orchestration into the snapshot owner.

PatchOperations:

- `create_file`, `apply_patch`, `replace_text`, and `rollback_patch` keep current return text, signatures and exceptions.
- Its Git callable accepts argv plus explicitly typed stdin/timeout/output/env options and returns CommandResult. Its review callable accepts a timeout and returns ReviewablePatch.
- These ports are bounded transitional seams to Slice 2.3. The composition root may supply existing Git/review operations as narrow callbacks; no port exposes arbitrary attributes, dispatch, or access to a ToolExecutor object.
- Later Slice 2.3 replaces those callbacks with the canonical Git/review owner without changing patch logic. Do not relocate fingerprint/pinned-index/verification implementations just to satisfy this slice.

Use small explicit limit records for read/search/patch concerns rather than passing ToolExecutor or its original generic `limits` object downstream. Constructor parsing and alias precedence remain in ToolExecutor. If existing mutable max_* attributes remain supported, delegate those properties into the owned limit records so values do not silently become stale copies. Do not broaden accepted limit values or add new limits during extraction.

## Canonical execution imports now available

The current execution package exposes these actual names:

```python
from looplane.execution.capture import bounded_text
from looplane.execution.environment import sanitized_subprocess_env
from looplane.execution.local_process import run_local_process
from looplane.execution.types import CommandResult
```

`run_local_process` accepts argv, cwd, timeout_seconds, max_output_chars, env, stdin, cancellation/line callbacks and an optional sandbox. It returns CommandResult with full byte-count/truncation fields. `run_bounded_command` is the historical facade entry point, not the canonical function name.

New tooling modules must not import `looplane.tools` or `looplane.runtime`. Default command/environment behavior should use the canonical functions above. Composition can supply explicit dynamic compatibility callbacks when needed to preserve existing facade monkeypatch contracts. Do not turn a canonical module's fallback into an import of the facade.

The search invocation must preserve rg availability detection, exact argv, ten-second timeout, sanitized task-home environment and output bounds. Do not substitute shell execution or add a sandbox where one was not previously used. Leave run_check's current sandbox/fallback behavior in the retained verification owner for Slice 2.3.

## Behavior ledger to preserve

### Paths and reads

- Keep the same policy instance/root check. Preserve traversal, absolute/Windows-drive/backslash/NUL/.git guards and allowed-path resolution.
- SafePathPolicy resolves paths and permits symlinks whose resolved targets stay inside the permitted workspace. Do not introduce a blanket symlink ban. The walker separately skips symlink directories and .git directories, sorts traversal, and checks files through the policy.
- Root-file walking and later policy filtering preserve their current differences; no new traversal limit or filtering optimization.
- read_file reads max_read_bytes + 1, replacement-decodes visible UTF-8, and appends the same truncation marker. Only a nontruncated read records a version. An existing version entry is not removed merely because a later read was truncated.
- Hashing uses original complete bytes, not decoded or output-truncated text. Output bounds and read-byte bounds remain separate.

### Search

- Keep rg and Python fallback behavior separately characterized; they are not promised identical semantics.
- rg exit 0/1 is accepted; unavailable rg, non-workspace root or other return codes select the existing fallback. Preserve existing exception behavior.
- Python fallback excludes files containing NUL in the sampled bytes, applies fnmatch to relative paths, casefolds when requested, and scans the same max_read_bytes + 1 sample.
- Preserve sorted output, policy filtering, match caps, exact markers and bounded_text behavior. Do not silently change partial final-line handling or glob semantics.

### Validation and patch execution

- Preserve UTF-8 byte/line/file caps, metadata whitelist, binary/symlink/rename/copy rejection, duplicate file sections, matching headers, hunk counts and no-newline-marker state.
- Create-file continues generating the same quoted Git patch, including mode and no-final-newline handling, before using the validated patch flow.
- apply_patch retains check-before-apply, --whitespace=error-all, intent-to-add of new files, cumulative review, and existing reverse/reset rollback branches. Do not add new rollback branches for failures that previously propagated directly.
- Each operation retains its single monotonic deadline and remaining-budget error text. Rollback keeps its independent five-second Git budgets.
- replace_text still requires a complete prior read, exact one-match replacement, UTF-8/NUL/read/patch bounds, and a Git-tracked file. Its version entry changes only after success or the existing snapshot-restore path.
- Failed replacement preserves its rollback verification: a rollback write/fsync error can be tolerated only when restored bytes and mode are already correct. Preserve exception chaining and text.
- Successful apply_patch does not eagerly rewrite the read-version ledger; subsequent replacement detects stale content by hashing as before.

### Snapshots and atomic writes

- Atomic replacement preserves O_EXCL/O_NOFOLLOW when supported, temporary mode 0600, file and directory fsync, preserved target mode, os.replace and exception cleanup.
- Snapshot capture currently reads complete bytes without a dedicated snapshot-size cap. Preserve this during extraction; adding bounds would change rollback behavior and belongs in a separately authorized improvement.
- Restore resets the index first, recreates parents for existing files, restores bytes/mode, records their hashes, and unlinks/forgets originally nonexistent paths. Existing reset-result handling and exception propagation remain unchanged.
- Restoring an existing snapshot currently records a read version even if the agent had not read that file beforehand. Preserve this established behavior in the shared version-store integration.

## Source transfer map

| Current ToolExecutor members | Destination |
|---|---|
| _walk_files, list_files, read_file | WorkspaceFiles |
| search_text, _search_text_with_rg | WorkspaceSearch |
| _HUNK_HEADER, _header_path, _diff_git_paths, _validate_unified_diff | UnifiedDiffValidator |
| _quoted_diff_path, create_file, apply_patch, replace_text, _rollback_patch | PatchOperations |
| _atomic_replace_file | AtomicFileWriter |
| _snapshot_paths, _restore_snapshots | WorkspaceSnapshots |
| _read_versions mutations | ReadVersionStore |
| _git, reviewable_patch, _reviewable_patch_pinned, git_diff, workspace_fingerprint | Retain until Slice 2.3; supply narrow ports |
| run_check, tool_program, _execute_structured_steps, tool_transaction, _transaction_touched_paths | Retain orchestration until Slice 2.3; delegate snapshot/validation calls |
| MCP bridge construction/dispatch/refresh | Frozen Slice 2.1 ownership |

## Implementation sequence after authorization

1. Confirm main's committed Slice 2.1 snapshot and assigned source ownership. Use that snapshot, preserving any integrated process/import changes.
2. Add policy/version/atomic/snapshot characterization cases and typed narrow dependencies. Keep all tests bounded and deterministic.
3. Extract validation, filesystem and search; connect one shared ReadVersionStore to complete reads and exact editing.
4. Extract patch operations and snapshot restore with explicit Git/review/reset ports; retain Slice 2.3 orchestration in ToolExecutor.
5. Preserve public imports/signatures and private compatibility patch points with explicit forwarding. Keep canonical imports independent; do not copy facade globals into modules or use module replacement tricks.
6. Run focused policy/tools/tooling tests and Ruff, then give main the scoped report for full architecture/package/suite gates and the separate Slice 2.2 commit.

Potential new tests: `tests/tooling/test_filesystem.py`, `test_search.py`, `test_patch_validation.py`, `test_patching.py`, and `test_snapshots.py`. Keep existing public ToolExecutor tests, including the directory-fsync failure/rollback test, as integration contracts. Do not rewrite them solely to hide broken patch targets.

## Required evidence before calling Slice 2.2 complete

- Complete-read, stale-read, truncated-read and snapshot-restore version transitions, shared across independently constructed file/patch/snapshot owners.
- Existing policy boundary tests plus escaped and in-workspace symlinks and directory-walker filtering.
- Separate rg/fallback tests covering exact commands, env, timeout, return codes, order, limits and errors.
- Validator fixtures covering multi-file hunks, quoted paths, no-newline markers and malformed/forbidden sections.
- Real temporary-Git tests for create/apply/replace, cumulative limits, rollback bytes/mode, new-file index cleanup, deadline failures and exact existing exception behavior.
- Atomic-write fault injection, especially directory fsync after os.replace and rollback verification.
- Canonical import smoke proving extracted tooling does not load tools/runtime facades; no new SCC or tooling-to-agent dependency.
- Existing tools/MCP tests remain green, proving frozen Slice 2.1 behavior is not regressed.

No implementation or verification is claimed by this report. The remaining decision is main's authorization to begin source changes after the Slice 2.1 commit.

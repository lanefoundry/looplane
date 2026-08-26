# Source filesystem changed investigation

## Symptom

`pca` exits with `Conversation cleanup failed: source filesystem changed` after a
native conversation run.

## Plan

- [completed] Trace cleanup and source-invariant snapshot behavior.
- [completed] Compare the invariant scope with current concurrent worktree changes.
- [completed] Determine whether this is expected protection, a false positive, or a
  workspace escape.
- [completed] Record reproduction/evidence and safe next action.

## Constraint

Diagnosis only; do not change runtime behavior in this task.

## Conclusion

- The supported cause is concurrent source-worktree mutation, not an observed
  disposable-workspace escape.
- Conversation `ed1ac4...` ran from 16:41:08 to 16:42:35 Asia/Taipei. During that
  interval `docs/plans/clean-brand-name-plan.md` was created at 16:41:23 and root
  `.DS_Store` changed at 16:41:40. Either change is sufficient to trip the check.
- The source snapshot excludes only root `.git`; it hashes tracked, untracked,
  ignored, cache, and metadata files by type, mode, size, and SHA-256 content.
- The same drift can fail terminal workspace review and then be reported again on
  close as `Conversation cleanup failed`, even though disposable cleanup completes.
- Persisted diagnostics do not store the differing paths, so attribution currently
  requires external timestamps and cannot be reconstructed reliably after close.

## Recommended product change

- Keep HEAD and Git-control drift as hard integrity failures.
- Report working-tree drift with concrete added/removed/changed paths.
- Treat cleanup-only working-tree drift as a warning rather than exit code 2.
- Decide separately whether mid-turn source drift should block patch acceptance;
  do not silently ignore it.

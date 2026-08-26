# Repeated native run failure diagnosis

Status: root cause confirmed; no production code changed in this diagnosis

## Symptom

Every native Codex turn ends with:

```text
Workspace audit failed: source filesystem changed
No file changes were reported before failure.
```

## Root cause

The native conversation creates one disposable `HEAD` clone and one source-repository invariant
when the long-lived session starts. The invariant hashes every filesystem entry below the source
repository except `.git`, including ignored and untracked files. Every terminal turn calls
`workspace.review()`, which first compares the current source snapshot to that original invariant.

The live PCA process started at 16:40:57 and its disposable workspace was created at 16:41:10.
Its first turn completed at 16:41:18. Source files changed after that baseline, including:

- `docs/plans/clean-brand-name-plan.md`, created at 16:41:23;
- `.DS_Store` at 16:41:40;
- `src/.DS_Store` at 16:30:39 (already before this particular baseline, so not sufficient alone);
- `src/coding_agent/tui.py` at 16:43:08;
- other code/research files during concurrent repository work.

At least the new plan, `.DS_Store`, and `src/coding_agent/tui.py` therefore changed after the live
workspace was created. The next turn failed at 16:41:47. The invariant is permanently different for that session. Reusing the same conversation
workspace makes every later turn fail at the same audit gate, even when the turn only answers a
question and makes no disposable-workspace edits.

`No file changes were reported before failure` describes the disposable runtime's `changed_files`,
not the source-repository mutation that caused the integrity failure. It is not contradictory.

## Evidence

- `ConversationWorkspace.create()` captures `_filesystem_snapshot()` once.
- `_filesystem_snapshot()` excludes only the root `.git`; it deliberately includes ignored files.
- `review()` calls `_source_postcheck_sync()` before reviewing the disposable patch.
- both Codex and Claude conversation hosts reuse one workspace across turns and audit every terminal
  event.
- `tests/test_conversation_workspace.py::test_review_detects_source_filesystem_mutation_including_ignored`
  explicitly proves that changing an ignored file yields `source filesystem changed`.
- the two failed persisted turns contain user, assistant, and terminal-failure events but no tool or
  file-change event; the disposable workspace is clean and shares source HEAD `9a325f6`.
- live PID 99446 runs `pca` from this repository; PID 99868 is its Codex app-server; the live
  disposable workspace is `/private/var/folders/tj/7wzf4jxn13lf9lv36y_54slh0000gn/T/pca-conversation-mqrbeu96/workspace`.

## Immediate recovery

The current conversation workspace cannot become valid again because its original invariant is
immutable. Quit the current PCA process and start a new session only after all other agents,
formatters, tests, Finder metadata writes, and repository edits have stopped. A separate stable Git
worktree is the safer source when concurrent development must continue.

## Product follow-up

Do not silently refresh the invariant after drift; that would accept unreviewed source mutations.
The smallest safe UX improvement is to report the first changed source paths and explicitly mark the
conversation workspace invalid/restart-required after the first drift. A narrowly justified
`.DS_Store` exclusion can reduce macOS noise, but it would not have prevented this incident because
tracked source files also changed after session start.

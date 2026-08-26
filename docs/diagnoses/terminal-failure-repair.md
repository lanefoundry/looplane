# Terminal failure repair

## Goal

Make native conversation failures diagnosable and unambiguous after partial tool
success, without weakening workspace audit or runtime isolation.

## Plan

- [completed] Trace contracts and persistence schema for a lossless failure reason.
- [completed] Preserve terminal error separately from streamed assistant summary.
- [completed] Render a durable failure card/status with partial-change disclosure.
- [completed] Persist the concrete error for resume/postmortem use.
- [completed] Add failed-native and failed-audit regression coverage.
- [completed] Run focused and full relevant verification.

## Non-goals

- Do not silently enable arbitrary MCP servers, plugins, or web access.
- Do not weaken disposable-workspace auditing.
- Preserve unrelated shared-worktree changes.

## Result

- `RunResult.error` now carries the bounded concrete terminal diagnostic separately
  from streamed assistant `summary`.
- Failed conversation events persist both the stable machine `reason` and exact
  `error`; old schema-v1 records remain readable because the field is optional.
- Live failures render one red `Run failed` timeline entry with the error and any
  files changed before failure. The sticky status keeps the error visible.
- `/resume` restores persisted failure diagnostics without replaying failed-turn
  content into model history.
- Fixed a runtime-event/result ordering race that could duplicate assistant text or
  let a late `Thinking…` overwrite the terminal status.
- `uv run ruff check .` passed.
- All 342 tests passed on the final full-suite run. An earlier full run hit one
  unrelated transient macOS `os.killpg` permission failure; its isolated rerun and
  the final full rerun passed.
- Groundlane/plugins/subagents remain disabled by design; enabling them is a
  separate network/auth/trust-boundary change.

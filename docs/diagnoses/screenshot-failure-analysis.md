# Screenshot failure analysis

## Question

Explain why the captured Codex CLI turn shows successful shell/file-change activity
but ends as `failed · conversation_turn_failed`, and assess the TUI evidence quality.

## Checklist

- [completed] Compare visible event sequences in both screenshots.
- [completed] Trace `conversation_turn_failed` and native Codex event handling.
- [completed] Separate confirmed facts, likely cause, and UI defects.
- [completed] Record evidence-backed conclusions here.

## Constraint

Diagnosis only; do not modify runtime behavior in this task.

## Conclusions

### Confirmed

- Both screenshots show the same conversation/workspace. Screenshot 1 is scrolled
  earlier in the transcript; screenshot 2 is near the bottom and reveals later
  file-change events.
- Shell and file-change completion are action-local successes. The later terminal
  event independently marks the overall turn failed.
- The concrete terminal error is lost: the controller prefers streamed summary
  text over `TurnCompletedEvent.error`; the TUI shows only `Failed`, then the generic
  `failed · conversation_turn_failed`; persistence stores only that generic reason.
- The disposable workspace has already been deleted, so its audited Git state is
  no longer available for postmortem inspection.
- Codex is deliberately launched with hooks/plugins/remote plugins disabled and
  `mcp_servers={}`, so Groundlane cannot be available in this runtime configuration.

### Plausible causes, not recoverable as fact

1. Native Codex marked the turn failed after the subagent/collaboration error
   `no thread with id`. The ephemeral thread plus unavailable collaboration path
   makes this plausible.
2. The isolation wrapper replaced an otherwise completed terminal event with
   failed because audited workspace paths did not exactly match claimed file-change
   paths.

### UX defects

- Generic internal status hides the exact error and recovery action.
- The UI does not say that a failure occurred after a successful edit.
- Command/approval context is overwritten by output/diff instead of retained as
  durable audit evidence.
- Internal `.codex-task.md` bookkeeping appears as a product change, repeats three
  times, and is deleted with the disposable workspace.
- Long tool output is always expanded; raw Markdown fences are not rendered.
- Scroll position can make later actions invisible while the pinned terminal status
  already says failed, producing the misleading screenshot-1 view.

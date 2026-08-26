# Claude Code UI research continuation

Status: implementation and automated verification complete

- [x] Recover the latest research thread and identify the unfinished item.
- [x] Confirm whether Groundlane research tools are available in this session.
- [x] Verify the Textual underline cursor component styles and focused composer behavior.
- [x] Regenerate wide and narrow deterministic screenshots and inspect them.
- [x] Run focused and complete verification (31 TUI tests; 334 complete tests; Ruff; diff check).
- [x] Redesign approval around the pending action instead of a tall generic button modal.
- [x] Add wide/narrow geometry, copy, preview, keyboard, and decision coverage.
- [x] Render and inspect approval screenshots from the real fixture shape.
- [x] Run focused and complete verification after the approval change.
- [x] Update the UI investigation record with evidence and the remaining live-Warp boundary.

## Approval design direction

Subject: a terminal coding agent asking a developer to authorize one concrete side effect.
Single job: let the developer understand the exact action and choose a scope without losing the
conversation context.

```text
  Allow this action?
  ● Modify src/coding_agent/tui.py
    concise command or diff preview

  › 1. Allow once
    2. Allow for this session
    3. Deny
```

- Palette: inherit terminal theme; warning amber only for the authorization boundary; semantic
  error only for deny/cancel state; current-row inverse highlight for keyboard focus.
- Type: terminal monospace throughout; hierarchy comes from one question, one action node, muted
  preview, and aligned numbered choices.
- Layout: bottom-docked, content-height, bounded preview; never reserve an empty third of screen.
- Signature: the approval visually continues the pending tool row instead of becoming a detached
  desktop form.
- Deliberate risk: remove visible push buttons entirely and make the permission request behave like
  a terminal choice list. This is specific to a keyboard-first coding agent and keeps context visible.

Automated result: 33 TUI tests and all 337 collected tests passed; Ruff and `git diff --check`
passed; wide/narrow approval screenshots were visually inspected. Manual macOS/Warp IME preedit
confirmation remains outside Textual Pilot's capabilities.

Constraint: external web research must use Groundlane `web_search`, `web_fetch`, or
`web_extract`. Groundlane is not exposed in the current session, so this continuation is
limited to the checked-out source, installed local binaries, and existing research evidence.

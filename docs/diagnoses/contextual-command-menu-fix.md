# Contextual command menu fix

## Outcome

Typing a command with a finite argument set should immediately show those arguments above the
composer. Idle-only controls must not leak into the initial layout.

## Interaction contract

- `/runtime` and `/runtime <prefix>` list configured runtime choices.
- `/model` lists known models for the active runtime when available.
- `/permissions` lists ask, accept-edits, read-only, and clear.
- Up/Down changes the highlighted choice; Tab completes it; Enter selects and executes it.
- Unknown or free-form arguments remain editable and are never forwarded as hidden commands.
- Stop is hidden by default and appears only while a run is active.
- No persistent GUI action buttons remain in the status/composer surface; interruption is keyboard
  driven and details appear automatically only when needed.
- Escape/Ctrl+C can cancel before or after the runtime runner is assigned.
- Commands entered during an active turn stay in the composer until they can be run, not discarded.
- Any parser-accepted whitespace suppresses free-form argument completion without losing text.
- Codex MCP tool lifecycle items render as terminal tool activity instead of failing the turn.

## Verification

- [x] Runtime and permission options appear automatically at exact command input.
- [x] Prefix filtering, keyboard completion, and Enter execution work.
- [x] Stop and Details widgets are absent; Escape/Ctrl+C interrupt safely.
- [x] Wide/narrow screenshots and full regression checks pass.

## Status

Complete.

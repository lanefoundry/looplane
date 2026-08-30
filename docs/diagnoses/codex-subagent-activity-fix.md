# Codex subagent activity compatibility

## Outcome

Codex `subAgentActivity` items must render as bounded agent activity and must not terminate the
conversation. Known non-side-effect protocol additions should degrade visibly without blocking the
turn, while unknown side-effecting tools remain fail-closed.

## Tasks

- [x] Inspect the installed Codex app-server schema for the exact item shape and notifications.
- [x] Map subagent activity to the provider-neutral runtime lifecycle.
- [x] Add lifecycle and forward-compatibility regression tests.
- [x] Run focused and full verification.

## Exit contract

- Idle `Escape`, `Ctrl+C`, and `Ctrl+D` close looplane even when the composer has focus.
- `/exit` and `/quit` close immediately when idle.
- `/exit` and `/quit` cooperatively stop an active agent, then close after cleanup.

## Status

Complete.

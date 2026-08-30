# Groundlane in the Codex child runtime

## Goal

Allow the Codex native conversation to initialize the user-configured Groundlane MCP server without exposing unrelated host credentials or enabling unrelated MCP servers.

## Plan

- [completed] Read the Codex MCP configuration as a local trust boundary.
- [completed] Explicitly disable every configured MCP server except Groundlane.
- [completed] Forward only Groundlane's explicitly configured bearer-token environment variable to the Codex child.
- [completed] Add focused regression tests and run the relevant/full verification suite.

## Verification

- Codex app-server initialized a real ephemeral thread with Groundlane required.
- Focused Codex adapter tests passed (19 tests).
- Runtime/conversation/TUI regression selection passed (83 tests).
- Full pytest suite passed on the clean rerun (383 tests).
- Ruff passed for the two changed Python files. Repository-wide Ruff remains blocked by unrelated existing edits in `src/looplane/tui.py` and `tests/test_tui.py`.

## Security boundary

- Do not persist, print, or copy credential values.
- Do not forward arbitrary `AUTH`, `TOKEN`, API-key, password, or credential variables.
- Keep hooks, plugins, and remote plugins disabled.
- Keep the disposable workspace and approval boundaries unchanged.

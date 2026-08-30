# Claude Code cancel and rewind behavior audit

## Outcome

Document the exact Claude Code flow for interrupting a turn, rewinding to a prior submitted prompt,
restoring that prompt into the composer, editing it, and resubmitting. Separate prompt history recall,
conversation rewind, and code rollback, then map the safe looplane implementation boundary.

## Tasks

- [x] Inspect installed Claude Code version and local reference evidence.
- [x] Verify current official interactive-mode and checkpointing documentation via Groundlane.
- [x] Audit looplane storage/runtime gaps and cancellation semantics.
- [x] Produce a concrete interaction contract and recommended implementation sequence.

## Status

Complete. No product code was changed during this audit.

## Verified Claude Code contract

- `Esc` while a turn is running cancels the request and preserves partial output/work already done.
- If cancellation happened before a meaningful response, Claude Code automatically removes that
  submitted user turn and restores its text into the composer.
- With an idle, empty composer, double `Esc` opens the rewind selector. `/rewind` opens the same
  selector.
- Selecting a prior prompt and choosing **Restore conversation** forks the conversation at the point
  immediately before that prompt, then restores the selected prompt into the composer for editing.
- Prompt history (`Up`, `Ctrl+R`) only recalls text. It does not alter conversation state.
- Code restoration is independent from conversation restoration and is offered only when tracked file
  history exists. Bash/manual changes are not covered.

## looplane gaps

- Idle `Esc` exits the app (`src/looplane/tui.py:1151-1161`), so it cannot open rewind.
- `Ctrl+C` exits immediately when idle (`src/looplane/tui.py:3002-3021`), unlike Claude Code's
  clear-then-double-press exit behavior.
- `_prompt_history` is process-local text recall only (`src/looplane/tui.py:1326-1328`,
  `1689-1703`); it cannot fork or truncate a persisted conversation.
- The append-only conversation schema has no rewind/branch marker and `completed_turns()` replays all
  completed turns (`src/looplane/conversation.py:46-54`, `765-815`).
- The disposable conversation workspace has one committed-HEAD base and no per-prompt file checkpoint
  API (`src/looplane/conversation_workspace.py:33-89`).

## Recommended implementation contract

1. Cancel and wait for a terminal turn event before rewinding; fence the old writer/generation.
2. Add `/rewind` and idle-empty double `Esc` to open a keyboard-first selector of persisted user turns.
3. Create a new conversation branch from the event prefix immediately before the selected user turn;
   preserve the parent `events.jsonl` byte-for-byte and record parent/cutoff provenance in the child
   manifest.
4. Rebuild transcript and provider-native context only from that child prefix, allocate a fresh native
   session/context, and restore the selected text into the composer.
5. Keep the existing disposable workspace unchanged for **Restore conversation**.
6. Do not expose **Restore code** until per-prompt workspace checkpoints exist and their limitations are
   explicit.

## Claude Code source evidence

- `src/hooks/useCancelRequest.ts:148-167`: `Esc` owns cancellation only when work/queue exists.
- `src/components/PromptInput/PromptInput.tsx:1254, 1948-1956`: queued prompts are taken back for
  editing; empty double `Esc` opens the selector.
- `src/screens/REPL.tsx:2996-3020`: early cancellation auto-restores the last submitted prompt.
- `src/screens/REPL.tsx:3656-3747`: rewind slices to before the selected prompt, creates a new
  conversation ID, resets derived state, and restores text/mode/images into the composer.
- `src/components/MessageSelector.tsx:93-133, 141-245, 328-357`: conversation/code restore choices
  are distinct and restoration targets the point before the selected prompt.

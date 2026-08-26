# Model-switch conversation diagnosis

Status: implemented and verified

## Finding

Rivumi currently treats every native runtime or model change as a new conversation context. This is an explicit application behavior, not a fundamental Claude or Codex limitation.

## Evidence

- `RivumiApp._run_configuration()` compares the previous runtime/model, then releases the conversation lease, clears `_ask_history`, resets the transcript, generates a new `_runtime_context_id`, and marks the native session as context-free.
- Native controller identity is `(runtime, repository, model, context_id)`, so any model/context change selects a different `ConversationController` and provider-native session.
- `ConversationManifest` pins one `runtime` and `model_override` for its lifetime.
- The durable store intentionally persists only provider-neutral completed user/assistant turns. It excludes vendor thread/session IDs, tool protocol state, workspace state, and credentials.
- Semantic replay already exists for restart/resume, bounded to the most recent 12 messages and 48,000 characters, but model switching clears the in-memory history instead of invoking that replay path.

## Boundary

- Preserving the visible dialogue across a model switch is feasible: close the old native session, keep the Rivumi conversation, start a new session, and replay bounded completed turns.
- Preserving opaque vendor thread state across Claude and Codex is impossible because their session protocols are unrelated.
- Preserving the exact editing workspace/tool state across provider switches requires an explicit workspace handoff or patch checkpoint; the current durable conversation store does not contain that state.

## Smallest product fix

Treat model/runtime switching as a new native session segment inside the same Rivumi conversation. Retain the transcript and completed-turn history, record a runtime/model switch event, replay bounded history once, and show a visible boundary marker. Keep `/new` as the only action that clears the conversation.

## Implementation checklist

- [x] Persist a provider-neutral context-change event and update the conversation manifest.
- [x] Preserve transcript, conversation ID, writer lease, and completed-turn replay history across native runtime/model changes.
- [x] Start a new native session identity and replay bounded completed turns once.
- [x] Show an explicit runtime/model boundary in the timeline.
- [x] Keep switches to/from the non-native Rivumi agent on the existing new-context behavior.
- [x] Add regression tests for same-runtime model switches, Claude/Codex switches, persistence, and `/new` semantics.
- [x] Update M11 documentation and run focused/full verification.

## Verification

- `uv run pytest`: 352 passed.
- `uv run ruff check .`: passed.
- `uv lock --check`: passed.

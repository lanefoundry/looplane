# Runtime parity plan

## Outcome

Reduce the remaining semantic gap with Claude Code without widening Rivumi's trust boundary.

## Contracts

- Context telemetry is provider-reported when available and explicitly estimated otherwise.
- Native compaction is delegated only when the provider supports it. Rivumi never pretends local
  trimming is model summarization; a provider may omit a portable checkpoint.
- Permission mode is Rivumi-owned deterministic policy: ask, accept-edits, or read-only. Provider
  prompt text is never treated as enforcement.
- Approval remains correlated to one action. The choice surface is rendered adjacent to that
  pending action while the policy retains the final decision authority.
- Proposed diffs are computed only from bounded tool input and the isolated pre-edit workspace.
- Background task state is explicit and fenced. Queued follow-ups are not presented as concurrent
  execution when only one runtime turn is active.

## Workstreams

- [x] Provider-neutral telemetry, capability, preview, permission, task, and checkpoint contracts.
- [x] Claude/Codex runtime mapping and bounded pre-execution approval previews.
- [x] Permission-mode editor and deterministic enforcement tests.
- [x] Inline approval presentation, numeric shortcuts, and focus restoration.
- [x] Accurate queued state and provider-aware `/compact`/`/context`; unsupported background task
  management remains explicitly false instead of being simulated.
- [x] Full local verification and deterministic narrow/wide screenshots.

## Preserved boundaries

- Agent/Task, Web, MCP, plugins, and provider settings remain unavailable until each has a separate
  capability and threat model.
- The current dirty worktree remains outside the disposable runtime workspace.

## Status

Complete for the bounded runtime-parity phase. Native subagents, web, MCP/settings, concurrent
background task management, and writes to the user's dirty worktree remain intentional gaps behind
separate capability and threat-model work.

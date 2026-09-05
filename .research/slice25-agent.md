# Wave 2 Slice 2.5

Status: production implementation applied; all gates explicitly unrun.

## Scope and frozen Slice 2.4 sources

Before implementation, `cp -p` preserved `src/looplane/loop.py` as
`.research/slice24-frozen/loop.py`, and `cp -Rp` preserved `src/looplane/agent/` as
`.research/slice24-frozen/agent/`. No Git or verification command was used.

Owned production changes:

- `src/looplane/agent/model_calls.py`: typed `ModelCallState`, bounded model waits,
  retry/backoff/fallback, usage aggregation, per-model cost accounting, and cache trace
  artifacts/events. Existing model-provider types only; no provider adapters.
- `src/looplane/agent/ports.py`: typed event/hook/approval/deadline/execution callbacks,
  `PreparedToolCall`, and explicit `SubagentRunnerFactory` / `SubagentRunner` contracts.
- `src/looplane/agent/tool_scheduler.py`: fingerprint repetition guards, bounded audit
  arguments, tool previews, preparation and patch validation, sequential approval
  lookahead, prepared execution, concurrent read batches, and blocking-side-effect
  cancellation deferral.
- `src/looplane/agent/subagent_dispatch.py`: canonical roles, child-task boundaries,
  schedule normalization/analysis, wave dispatch, handoff reports, parent-approved
  transactions, tool definition, and child execution through an injected runner factory.
- `src/looplane/subagents.py`: compatibility exports and public `run_subagent_task`
  wrapper. The public wrapper supplies the default runner factory; the canonical leaf
  requires injection and never imports the coordinator.
- `src/looplane/loop.py`: composition and coordinator calls to the new leaves, retaining
  the completed Slice 2.4 state/context/persistence/lifecycle owners.
- `.research/slice25-agent.md`: this implementation record and required follow-up.

No tests, architecture allowance, tooling/execution/CLI/TUI/Codex files, Slice 2.4
owner files, or unrelated research repair artifacts were modified. No Slice 2.6
verification/completion extraction was performed.

## Implemented responsibility and dependency changes

The former production cycle had a dynamic edge from `loop.py` to `subagents.py` and
another edge back from `subagents.py` to `loop.py`. The implemented dependency path is:

```text
subagents.py -> loop.py -> agent.subagent_dispatch
subagents.py -----------> agent.subagent_dispatch
agent.subagent_dispatch -> agent.ports + typed state + lower-level contracts
agent.tool_scheduler ---> agent.ports + persistence + lower-level contracts
```

`loop.py` supplies `AgentRunner` through `SubagentRunnerFactory`; the canonical child
runner function does not resolve, import, or globally register a concrete runner.
The public compatibility wrapper retains a one-way lazy coordinator import for callers
that do not inject a factory. There is no canonical import of `subagents.py`, `loop.py`,
CLI/TUI, or tool compatibility facades. The graph has not been executed or checked in
this slice; this describes the applied source dependency change, not a passing gate.

Scheduling functions receive explicit typed state, executor capabilities, and operation
callbacks per invocation. They do not retain a runner or proxy arbitrary attributes.
Model state owns candidate selection and retry failure codes; shared usage remains in
Slice 2.4 `TurnState`. Event dictionaries remain serialized payloads, not opaque state.

The source extraction preserves these existing ordering decisions:

- Reset the retry budget for each candidate and each logical model request; emit retry
  and fallback events before proceeding; use the same delay function and server-hint cap.
- Cancel model waits promptly; drain a started blocking tool before returning from task
  cancellation; cancel and join parallel read tasks before emitting batch completion.
- Record fingerprints before requested events and approval, including lookahead that
  stops at repetition/unknown-tool guards. Do not roll back lookahead fingerprint progress.
- Prepare lookahead sequentially, stop at denial/non-read boundaries, defer denied
  observations until prior read results, and return `gather` results in call order.
- Emit tool-started before clearing the matching pending action; execute the action,
  emit completion, and run the post-tool hook in the original order.
- Keep check-fingerprint capture and check-evidence admission around scheduled execution
  in the coordinator, so agent-level verification decisions remain Slice 2.6 work.
- Run child waves concurrently, process parent-approved proposed transactions in order,
  keep read-only child approvals and recursion disabled, and retain parent path bounds.
- Keep model retry constants and `retry_delay_seconds` available through `loop.py`.

## Validation status: deliberately unrun

No tests were read or edited for this slice. No pytest, Ruff, formatter, build, import
smoke, AST validation of generated output, dependency-graph check, Git command, or
post-write code review was run. The implementation was applied in one production write
phase using an external mechanical extraction script. The Slice 2.4 green test results
are pre-2.5 evidence only and must not be reported as validation of these changes.

Required follow-up when validation/test edits are authorized:

1. Check parsing/imports and focused Ruff for the changed production modules.
2. Run model retry, retry exhaustion/fallback, server-hint/jitter, cancellation, usage,
   cache trace, wind-down, and public compatibility tests. Retarget private monkeypatches
   to canonical owners where extraction moved the actual call boundary; preserve assertions.
3. Run tool preparation, patch validation, hook/approval, read-only/MCP concurrency,
   deterministic result/checkpoint order, lookahead denial, and repeated-action coverage.
4. Run native subagent role/wave/dependency/handoff/transaction tests and programmatic
   child-runner tests, including an injected fake runner factory and cancellation cases.
5. Run resume, continuation, pending/reused approval reconciliation, event sequencing,
   active-time budgets, and verification-state drift coverage against Slice 2.4 behavior.
6. Update `tests/test_modularization_boundaries.py` to remove the obsolete allowance for
   the `loop.py` / `subagents.py` SCC, then run the graph and canonical import gates.
   The allowance remains untouched as explicitly instructed.
7. Run main-goal integration gates (full tests/Ruff, startup and packaging as applicable)
   separately from this worker's unvalidated production implementation.

## Remaining Slice 2.6 boundary

`loop.py` still owns `_verify_all`, `_run_review_lane`, `_persist_verification`,
verification/workspace fingerprints, checked-workspace evidence admission, `_finish`,
patch collection, final-summary/result assembly, and terminal/checkpoint transitions.
`_execute_prepared_tool_call` retains only the check-evidence policy surrounding the
scheduler call; this is intentionally not moved into tool execution policy.

The loop remains the temporary public coordinator; `agent/runner.py`, verification, and
completion leaves are not introduced here. Runtime success, lint cleanliness, and cycle
elimination still require the unrun gates above. No commit or staging was performed.

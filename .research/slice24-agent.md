# Wave 2 Slice 2.4

Status: Slice 2.4 complete; focused pytest and Ruff passed.

Scope: Wave 2 Slice 2.4 only. No scheduling/subagent extraction (2.5), verification
or completion-policy extraction (2.6), staging, commits, web calls, or provider calls.

## Changed paths and responsibility owners

- `src/looplane/agent/__init__.py`: package boundary; deliberately does not import the coordinator.
- `src/looplane/agent/state.py`: `TurnState`, `ContextState`, and `ActiveRunClock`;
  restores messages, step, usage, model usage, action/verification fingerprints, and
  versioned context markers. Preserves the existing fresh repetition budget on resume.
- `src/looplane/agent/checkpoints.py`: `RunPersistence`, `ClaimedSession`,
  `claim_session`, `check_resume_identity`, and `session_phase`; owns request,
  manifest, checkpoint, event sequencing, and writer-lease acquisition on resume.
- `src/looplane/agent/context.py`: initial memory/instruction/skill/tool/workspace/runtime
  context, pressure reminders, history compaction plans, workspace reminders,
  context-provider collection, IDE diagnostics/open files, instruction reload, and
  project-context reload. `ContextUpdate` returns additions and event notices;
  `HistoryCompaction` returns explicit source indices and hook payloads.
- `src/looplane/agent/run_lifecycle.py`: `BoundedRunLifecycle` owns charged wall time,
  approval-time pause/resume, persisted clock recovery, and final lease cleanup.
  Its `run` method accepts a typed asynchronous engine callback and returns its result.
  Safe run-ID and run-location validation also live here.
- `src/looplane/loop.py`: temporary coordinator, wired to these typed owners. Public
  `AgentRunner.run` invokes the lifecycle facade; `_run_turns` retains the original
  preparation/turn/terminal state machine. The coordinator applies context additions
  before emitting their events and retains pre/post-compaction hook ordering.
- `tests/agent/test_state_context.py`: 19 direct boundary cases, including parameterized
  checkpoint phases and identity dimensions.
- `tests/test_loop_e2e.py`: four existing private-manifest assertion paths now reference
  `runner._persistence.manifest`; assertions are preserved.
- `tests/test_approval_budget.py`: three fake-clock patch targets now reference the
  canonical lifecycle owner; all approval/cancellation assertions are preserved.
- `.research/slice24-agent.md`: this slice's status, evidence, and handoff.

Other workers' tooling, execution, CLI, TUI, Codex, and `subagents.py` files were not
written. Existing unrelated `.research` repair artifacts were not changed.

## Behavior and dependency boundaries

- Event persistence still saves the manifest before delivering the event and advances
  the sequence only after delivery succeeds. A failed manifest save prevents delivery;
  a failed delivery retains the sequence for recovery.
- Checkpoints still precede their manifest phase update, retain their writer token and
  tool-call count, and include the verified workspace fingerprint in the manifest.
- Active time excludes approval waits and includes already-consumed persisted time.
  Cancellation propagates through the engine while lifecycle cleanup settles the clock
  using the existing shielded-save ordering and releases the writer lease.
- Typed state objects replace the extracted private scalar fields. There are no mixins,
  whole-runner service references, dynamic attribute forwarding, or state dictionaries.
  Dictionaries are limited to existing serialized event/manifest/hook payloads.
- New leaves import canonical execution/workspace modules and domain/session/event
  contracts. They import no compatibility facade, CLI/TUI, provider adapter, or
  `looplane.models` implementation. The coordinator still constructs tools/providers.
- Resume and continuation retain executor reconstruction and verification-drift
  coordination in `loop.py`; persistence claims and restored state have dedicated owners.

## Validation evidence

Completed:

```text
uv run ruff check src/looplane/agent src/looplane/loop.py \
  tests/agent/test_state_context.py tests/test_loop_e2e.py tests/test_approval_budget.py
All checks passed!

uv run pytest -o addopts='' -q tests/agent/test_state_context.py tests/test_approval_budget.py
22 passed in 3.09s
```

Logs: `/tmp/looplane-slice24-ruff.log`, `/tmp/looplane-slice24-unit.log`.

Combined focused command:

```text
uv run pytest -o addopts='' -q \
  tests/agent/test_state_context.py tests/test_loop_e2e.py \
  tests/test_interactive_runner.py tests/test_approval_budget.py \
  tests/test_session.py tests/test_session_replay.py \
  tests/test_modularization_boundaries.py \
  tests/contracts/test_event_sink_compatibility.py tests/test_sdk.py
128 passed in 58.39s
```

Log: `/tmp/looplane-slice24-pytest.log`.

This run covers cancellation and nonterminal resume without duplicate patch application,
approval abandonment/reconciliation, contiguous resumed event sequences, durable grants,
spent wall-time recovery, continuation and fallback, reminder deduplication, compaction
hook order, context reload, model retry/fallback, verification/drift behavior, public SDK
and event-sink compatibility, facade import rules, and the unchanged cycle allowance.

The initial existing-suite run reached only three failures, all stale `loop.time`
monkeypatch targets after moving the clock. The initial new tests also exposed fixture
expectations for computed checkpoint `total_tokens` and the `ToolObservation.content`
field; those were corrected without removing assertions from existing tests.

Full repository pytest/Ruff, release builds, and live provider execution are not claimed
by this focused worker slice. Shared-workspace integration gates belong to the main run.

## Slice 2.5 handoff: still in loop.py

- Model calls and retry: `_complete_model_or_cancel`, `_complete_model_with_retry`,
  `_complete_model_wind_down`, `_backoff_sleep`, retry constants and `retry_delay_seconds`.
- Usage/cache accounting: `_add_usage`, `_record_model_usage`,
  `_record_provider_cache_trace`, `_aggregate_cost`, and `_token_budget_error`.
- Scheduling: `_prepare_tool_call`, `_execute_prepared_tool_call`,
  `_execute_read_only_batch`, `_can_execute_concurrently`, fingerprint guards, tool
  definitions, and the ordering within `_run_turns`.
- Subagent dispatch: `_execute_dispatch_subagents`, `_run_dispatch_subagents`, and
  `_dispatch_subagents_definition`. The existing literal dynamic import still forms
  the original `loop.py` / `subagents.py` cycle; this slice does not alter it.
- `_run_blocking_safely` and cooperative cancellation remain engine-owned. Context
  provider collection receives the narrow typed `BlockingCall` port. Preserve this
  cancellation-safe boundary when moving scheduling.
- New owners should accept `TurnState` or narrower explicit arguments and return
  observations/accounting updates. They must not import `loop.py` or retain the runner.

## Slice 2.6 handoff: still in loop.py

- Agent verification policy: `_verify_all`, `_run_review_lane`, `_persist_verification`,
  verification state fingerprints, checked-workspace evidence, and the final drift gate.
- Completion/result assembly: `_finish`, `_collect_patch`, final-summary persistence,
  terminal guards, and the remaining terminal branches in `_run_turns`.
- Approval policy/reconciliation and continuation workspace comparison remain coordinator
  work; only their clock and durable-write mechanisms moved in this slice.
- Keep checkpoint/event ordering through `RunPersistence`; use `BoundedRunLifecycle`
  around the extracted engine without creating another conversation/session abstraction.
- `agent/runner.py`, model/scheduler/subagent modules, and verification/completion modules
  were deliberately not created in this slice.

## Remaining issues

No production regression identified by the completed focused checks. All 128 combined
tests, including import-boundary and cycle tests, passed. The original loop/subagents
cycle and the remaining engine policy concentration are explicit 2.5/2.6 work, not
completed architecture claims.

The imported `Codex-omc.md` was absent from the workspace and checked user-level
locations. The supplied task scope and AGENTS instructions govern this slice.

# Slice 2.6 verification and completion design

Status: design only. Not implementation, code review, test execution, or gate evidence.
No production/test files were edited. No tests, Ruff, builds, git commands, Internet
requests, or validation of written code were run. Delivered Slice 1.5 source is frozen.

## Stable input and coordination boundary

Inputs are `.research/slice24-agent.md` and the preserved Slice 2.4 files:

- `.research/slice24-frozen/loop.py`, relevant verification/completion and engine sections.
- `.research/slice24-frozen/agent/state.py`.
- `.research/slice24-frozen/agent/checkpoints.py`.
- `.research/slice24-frozen/agent/run_lifecycle.py`.

The actively edited `src/looplane/loop.py` was not read. Source references below are
frozen-snapshot anchors, not claims about the current working coordinator. The Slice
2.4 report's historical validation is producer-reported context only; none was rerun
or extended here. Context assembly was not reread because 2.6 does not own it.

No agent-message tool was present in the callable inventory, so no direct message to
Archimedes was sent and no fresh interface agreement is claimed. The specified frozen
handoff supplies stable design input. Main/Archimedes should reconcile the narrow
2.5 interfaces listed below before implementation; this does not block this document.

## Recommendation and canonical owners

Keep the finalization state machine visible in canonical `agent/runner.py`.
Extract agent-level verification policy to `agent/verification.py` and artifact/result
completion to `agent/completion.py`. The former owns what constitutes reusable/passing
check evidence and the optional read-only review lane; the latter owns bounded patch
collection, artifact/result construction and ordered terminal persistence. Neither
module receives an AgentRunner or accesses a runner's private fields.

`RunPersistence`, `TurnState`, `ContextState`, and `BoundedRunLifecycle` from 2.4 remain
canonical. No new Session, event hierarchy, manifest format, or generic workflow
engine is needed. The tooling layer executes already-authorized checks; it must not
acquire agent-level verification/review/completion policy.

| Frozen source anchor | New owner | Scope |
|---|---|---|
| `loop.py:836` `_verification_state_fingerprint` | verification | Stable hash of workspace fingerprint plus ordered declared check configuration |
| `loop.py:1075` `_verify_all` | verification | Complete ordered declared-check pass, reuse eligibility, approval request, execution, evidence and cancellation handling |
| `loop.py:1176` `_run_review_lane` | verification | Optional reviewer prompt, empty tool list, bounded response, reviewer events and review artifact; model call/accounting through 2.5 ports |
| `loop.py:1251` `_persist_verification` | verification | Publish evidence, write verification.json/test.log, then reject workspace drift |
| `loop.py:1285` `_current_verification_state_fingerprint` | verification | Cancellation-safe workspace fingerprint call plus stable configuration hash |
| `loop.py:1293` `_capture_verified_workspace_fingerprint` | verification helper, coordinator applies returned stamp | Capture trusted baseline; preserve failed-read behavior at call sites |
| `loop.py:1483` manual run_check evidence inside `_execute_prepared_tool_call` | shared verification evidence port called by 2.5 scheduler | Cache invalidation/start stamp, post-hook end stamp, success record and log contribution; do not move general tool dispatch back into verification |
| `loop.py:1428` `_collect_patch` | completion | Reviewable patch call, changed paths and changes.patch write |
| `loop.py:1845` `_finish` | completion | Fallback patch collection, result assembly, artifacts, checkpoint/event/result ordering and executor close |
| `loop.py:1917` `_run_turns`, final-answer branch | canonical runner | Persist proposed summary, detect drift, invoke verification, request repair or review, budget/final drift gates, choose terminal outcome |
| `_run_turns` cancellation/deadline/provider/max-step branches | canonical runner | Visible policy decisions producing a typed terminal request; no blanket catch inside completion |
| `_open_continuation` around 307 | canonical runner using verification stamp helper | Compare resumed workspace against persisted evidence; keep resume/approval reconciliation in existing owners |
| `agent/checkpoints.py` | existing RunPersistence | Sole sequence, manifest and checkpoint mechanisms, used through explicit ports |
| `agent/run_lifecycle.py` | existing BoundedRunLifecycle | Active-time accounting, approval pause/resume and final writer-lease cleanup |

The reviewer can remain a named class/helper in verification.py. Splitting it into
another file is optional if useful during implementation; it must not become another
model retry/accounting owner.

## Typed inputs, evidence and ports

Use existing `VerificationCommand`, `VerificationOutcome`, `RunStatus`, `RunResult`,
`Usage`, `ModelUsageRecord`, `Message` and existing cost/checkpoint/event models.
Do not encode service requests as arbitrary runner-field dictionaries.

Proposed small records (design names, not files or code already created):

| Record | Fields / meaning |
|---|---|
| `VerificationInputs` | Ordered `tuple[VerificationCommand, ...]`, task instruction, run artifact directory, active-budget handle, output bound |
| `CheckedCommandEvidence` | Combined verification stamp, successful `VerificationOutcome`, originating `tool_call_id` |
| `VerificationEvidence` | Outcomes tuple and `verified_workspace_fingerprint: str | None`; a snapshot of the existing durable state fields |
| `VerificationReport` | Completed outcomes tuple, initial combined stamp, final evidence; only returned on the existing normal path |
| `ReviewInputs` | Patch text, changed-path tuple, verification tuple, instruction and output bound; optional reviewer port and remaining budget |
| `ReviewOutcome` | Optional bounded review text; distinguish absent/skipped/failed review for local control if useful without changing persisted events |
| `CollectedPatch` | `content: str`, `changed_paths: tuple[str, ...]`; preserve the currently collected tuple's meaning |
| `CompletionRequest` | Requested status/reason/summary/error, optional verification override, optional collected patch, optional patch timeout |
| `ResultAccounting` | Usage, model-usage tuple and aggregate cost using the existing type of RunResult.cost; obtain its exact canonical type/port from 2.5 |
| `CompletionIdentity` | Run/task IDs and artifact directory; no SessionStore or provider construction hidden inside |

`TurnState.last_verification` and `TurnState.verified_workspace_fingerprint` remain the
only durable verification fields. Verification service may own ephemeral per-run
`CheckedCommandCache` and test-log entries, but must not mirror all of TurnState.
Do not restore the ephemeral cache from a manifest unless an existing contract already
does so: the frozen constructor starts `_checked_workspaces` empty.

The policy service accepts an explicit publication callback/port for
`VerificationEvidence`. This applies only those two TurnState fields before artifact
writes, preserving the snapshot's failure semantics. Returning evidence only after
all writes succeed would subtly change which state the error/final checkpoint sees.
A successful `verify()` additionally returns `VerificationReport`; existing exceptional
cancellation and drift paths remain exceptions after their required evidence work.

Needed ports, supplied explicitly by the coordinator/composition root:

- `VerificationExecution`: run a named declared check and obtain workspace fingerprint;
  implemented over the canonical tooling executor without changing execution policy.
- `BlockingCall`: reuse the cancellation-safe boundary from 2.5. Frozen
  `_run_blocking_safely` shields a started operation until it returns and records the
  cooperative cancel request. A plain `to_thread` replacement changes semantics.
- `ApprovalRequestPort`: typed final-check request returning decision and request ID;
  existing approval policy, reconciliation, durable audit and active-time pause remain
  coordinator/approval-owned. Include a typed pending-action/start-marker adapter;
  do not give verification access to `_persistence.manifest.pending_action`.
- `CancellationProbe`: read cooperative cancellation state; do not create another token
  or catch all cancellation paths in a new verification wrapper.
- `ActiveBudget`: `remaining()` and a phase label setter, backed by existing lifecycle.
  Frozen `deadline` is the task's active-wall-time allowance, not a Unix/monotonic
  absolute timestamp. Do not reinterpret the float during extraction.
- `EmitRunEvent`: ordered event emission through existing RunPersistence. A bounded
  typed event-data adapter is sufficient; there must not be a second sequence counter.
- `PublishVerificationEvidence`: apply the explicit evidence snapshot to TurnState.
- `ReviewModelPort`: read-only provider/model identity and one `complete(messages, ())`
  call returning ModelTurn. No concrete provider construction/import in verification.
- `ModelAccountingPort`: 2.5-owned record usage, cache trace and cost accounting;
  preserve reviewer call order and return the exact reviewer accounting record if
  needed instead of reaching into a shared model-usage list's private last element.
- `TerminalPersistencePort`: checkpoint, emit terminal event and write result using
  existing mechanisms and current TurnState/lifecycle clock supplied by the coordinator.
- `PatchSource`: reviewable patch and close operations from the canonical tooling
  executor. Keep patch I/O cancellation behavior distinct from started check execution.

Bind these ports once per run location/executor incarnation and rebind explicitly on
resume/continuation. Do not retain stale run paths after `_rebind_run_location` or
stale executors after reconstruction. No `self: AgentRunner` callback facade, field
forwarding proxy, mutable service locator, or feature import back to `loop.py`.

## Verification evidence and ordering contracts

The persisted field called `verified_workspace_fingerprint` contains a combined
verification-state hash, not just a raw workspace hash. Preserve the exact input:
workspace fingerprint plus each declared command's name, argv list and timeout in
declaration order; JSON uses ensure_ascii=False, sort_keys=True and separators
(',', ':'), then SHA-256. Changing any of this invalidates resume/reuse semantics.

A final pass performs these operations in this order:

1. Set active phase to final verification, check remaining budget, capture the initial
   combined verification stamp. Fingerprint execution currently uses a fixed 10-second
   bound; do not replace it with another deadline interpretation as part of a move.
2. Before each declared command, check cooperative cancellation.
3. Reuse only if cached stamp equals both the current stamp and the pass's initial
   stamp, cached outcome is successful, and argv equals the declared argv. Emit
   `verification.reused` with the original tool call ID; do not fabricate started/
   completed events or duplicate its log entry.
4. Otherwise request FINAL_VERIFICATION approval. CANCEL raises cancellation. Check
   cooperative cancellation again. DENY produces a failed outcome and a
   `verification.completed` event with approval-denied details, then continues to
   the next check. Do not turn denial into a run-level exception or skip later checks.
5. Recheck remaining budget, emit `verification.started`, then mark the pending
   approved action started using the existing approval adapter, then execute the
   named check through cancellation-safe blocking execution.
6. Append the outcome, append the existing test-log text, emit
   `verification.completed`, then examine cancellation. If cancelled after a started
   check returns, publish/persist partial evidence before raising cancellation.
7. At normal pass end, publish/persist all outcomes and return the recorded outcomes.

Before-start cancellation and cancellation after a completed command are not
interchangeable: the frozen implementation only persists partial final-pass evidence
at its explicit after-execution cancellation point. Preserve those distinctions.

Evidence publication/persistence itself is ordered:

1. Assign the outcomes tuple to TurnState's existing field.
2. Only if the outcome count equals declared command count, every outcome passed,
   and an executor exists, compute an ending combined stamp. Missing expected stamp
   or unequal stamps means drift; clear verified evidence. Otherwise record the stamp.
   Any incomplete/failed set also clears verified evidence.
3. Atomically write `verification.json` with the outcome models.
4. Write accumulated `test.log` using the current format.
5. If drift was found, raise the existing ToolExecutionError after those artifacts
   and state have been recorded. Do not return an apparently verified report.

An unreadable ending fingerprint can raise before the artifact writes. Do not flatten
all failures into a bool and lose this ordering. Zero declared commands preserve the
existing vacuous `all(...)`/count behavior, including its fingerprint validation.

### Shared manual-check evidence: handoff to Archimedes / 2.5

The scheduler's manual `run_check` path must use the same evidence owner:

1. Remove the named cache entry and old executor verification outcome before starting.
2. Attempt a start combined stamp; unreadable fingerprint means no reuse candidate.
3. Preserve tool.started -> approved-action start -> execute -> tool.completed.
4. Run the existing POST_TOOL_USE hook.
5. Only after that hook, obtain ending stamp and cache a successful outcome when
   start and end match. Record the originating tool-call ID and the existing log entry.

This catches workspace mutations from hooks as well as from the check itself. A
`record_check_result(outcome)` callback with no start/end or hook ordering is
insufficient. Proposed scheduler interface: `begin_manual_check(name)` returning a
stamp token and `finish_manual_check(token, outcome, tool_call_id)` after the hook.
The verification owner supplies fingerprinting/cache/log mechanics; scheduler owns
actual tool/hook order. Confirm this contract against 2.5 before applying a patch.

## Reviewer policy and accounting

The optional reviewer is advisory. No model or blank patch returns no review. The
snapshot emits role_lane.requested, builds its current bounded read-only prompt,
calls complete with an empty tool list once, records usage, records cache trace,
bounds text, writes review.md, then emits role_lane.completed including cost/preview.
If nonempty, the coordinator appends the review to final_summary.

Preserve the exact caught failure classes: TimeoutError, ProviderError, OSError and
ValueError produce role_lane.failed and no review. Cancellation is not swallowed by
that handler. Do not add primary-model retries, tools, another reviewer veto, stricter
response-shape rules, or another billing owner during extraction. Reuse 2.5 accounting
without inheriting its primary-lane retry policy accidentally.

The final token-budget check happens after reviewer accounting. A reviewer-induced
budget excess is still a failed run, even if checks passed. Final workspace drift
must also be checked after review. A review artifact or successful review event is
not proof that the final workspace remained verified.

## Run-visible state machine

Keep `AgentRunner.run()` calling `BoundedRunLifecycle.run(self._run_turns)`.
The reduced `_run_turns` should visibly express these phases rather than delegating
all terminal branches to a hidden service loop:

```text
prepare / resume / continuation
  -> context and model turn
  -> tool scheduling and checkpoint, or truncated-answer continuation
  -> candidate final summary persisted to manifest
  -> current verification stamp / made_changes decision
       unchanged -> finish(COMPLETED, no_changes, verification=())
       changed   -> verification.verify(...)
          cancelled -> finish(CANCELLED, verification_cancelled)
          failures  -> append untrusted verification feedback
                       -> checkpoint(VERIFYING, verification_passed=False)
                       -> next model turn
          passed    -> collect patch
                       -> optional reviewer
                       -> token-budget gate
                       -> final verification-stamp gate
                       -> finish(COMPLETED, verified, collected_patch)
step limit -> existing toolless wind-down -> finish(FAILED, max_steps_exceeded)
other cancellation / budget / provider / initialized-run errors
  -> the existing explicit terminal classification -> finish(request)
lifecycle finally -> settle charged clock -> release session writer lease
```

No-changes is not just `made_changes == False` from successful editing tools. The
engine also considers fingerprint read failure or inequality with verified evidence;
this captures partial side effects from failed tools. Fresh baseline capture and
continuation's restored evidence remain conservative on unreadable state.

Do not turn all terminal reasons into a new generic enum or rename serialized strings.
Preserve existing reasons such as no_changes, verified, verification_cancelled,
workspace_changed_after_verification, verification_state_unreadable,
patch_artifact_failed, token_budget_exceeded, wall_time_exceeded,
max_steps_exceeded, approval_cancelled, user_cancelled and provider/error variants.

Max-step wind-down stays a failed bounded run even if its summary sounds successful.
Provider errors still emit model.failed before finalization; initialized generic
failures emit run.error; pre-initialization failures propagate. Context assembly,
retry/fallback, scheduling, subagent dispatch and approval reconciliation are not 2.6
policy to duplicate.

## Final drift and terminal persistence sequence

Successful verified completion is deliberately multi-stage:

1. Verification evidence and artifacts finish.
2. Collect reviewable patch and changed paths; changes.patch is written.
3. Run optional reviewer and account its usage/cache trace/review artifact/event.
4. Apply token-budget gate.
5. Read the final combined verification stamp. On read failure finish with
   verification_state_unreadable. On inequality clear verified evidence and finish
   with workspace_changed_after_verification. These failure paths may collect a
   bounded diagnostic patch; passing outcomes alone do not make that patch verified.
6. Only the matching path passes its previously collected patch into completion.
   Do not recollect a different successful patch after the final drift check.

Inside completion, preserve frozen `_finish` exactly at the observable boundaries:

1. Use the explicit verification override or the existing last verification tuple.
2. Collect patch unless supplied, otherwise rewrite changes.patch with the supplied
   content. On ToolExecutionError or TimeoutError only, override status/reason to
   FAILED/patch_artifact_failed, append the existing explanatory summary, clear
   changed paths and write an empty patch. Preserve the existing error field behavior.
3. Ensure test.log exists, even when no check ran.
4. Construct RunResult using current accounting, identity, verification and artifacts.
   The artifact mapping is request/events/checkpoint/patch/test_log/result with
   optional cache_traces and review based on file presence. verification.json is not
   automatically added to that mapping by the frozen implementation.
5. Await terminal checkpoint. RunPersistence writes checkpoint.json first, then updates
   and saves manifest phase/terminal/state/verified evidence.
6. Emit run.<status>. RunPersistence saves the updated manifest before delivering the
   event and advances its sequence only after successful delivery.
7. Atomically write result.json.
8. Close the executor and return the result.
9. BoundedRunLifecycle finally settles active time with shielded clock save and
   releases the writer lease. Executor close and writer-lease release are distinct.

Do not advertise this as a cross-file transaction. A failure at checkpoint, event,
result write or close has different observable evidence; do not reorder writes to
make a refactor superficially cleaner, silently retry finalization, or release the
writer lease from CompletionWriter. Broader OSError handling, terminal retry
idempotence and stronger artifact atomicity would be separate behavior changes.

The proposed final summary is saved to the manifest before verification. Reviewer
text is later included in the returned/persisted result summary; the frozen path does
not separately resave that augmented summary to the manifest. Preserve the distinction
instead of silently changing resumed summaries during extraction.

## Compatibility transition to agent/runner.py

Move the reduced coordinator only after 2.5 scheduling/model ports and 2.6 service
bindings are stable. Canonical runner imports canonical tooling/execution/workspace
and agent service paths. Verification/completion never import `looplane.loop`,
`looplane.tools`, concrete provider implementations, CLI/TUI, or `subagents` to obtain
a runner. Do not reintroduce the loop/subagents cycle that 2.5 is removing.

Keep `looplane.loop.AgentRunner`, resume/continuation construction, cancellation and
other existing public facade names available. Prefer a direct class reexport if it
preserves all existing monkeypatch contracts; use a narrow facade constructor/factory
adapter with late-bound callbacks where a demonstrated old module patch needs it.
Do not use sys.modules probing, whole-module forwarding, or a facade import from the
canonical runner. Preserve resume returning the expected class/subclass when choosing
between alias and subclass; retain original constructor defaults and keyword meanings.

The frozen constructor already stores `model_retry_delay` as a callable, and 2.4
already moved clock test targets to the canonical lifecycle owner. Do not undo those
choices by moving clock/retry implementation back into loop.py. Exact remaining
monkeypatch requirements must be inventoried by the authorized integration owner;
this task did not read active tests or assert they already pass.

Only 2.6 should replace coordinator verification/completion method bodies with narrow
service calls. Updating SDK/CLI/public factory imports is integration work for their
owners, using the canonical runner once available. Keep the serialized request,
manifest, checkpoint, event, verification and result schemas unchanged.

## Concrete coordination checklist for Archimedes / main

- Confirm the frozen 2.4 state/persistence/lifecycle interfaces still match the chosen
  integration base; this document does not follow active loop.py edits.
- Agree on ownership of manual-check cache/log state and the begin/after-hook evidence
  callbacks before 2.5 moves `_execute_prepared_tool_call`.
- Supply 2.5's cancellation-safe blocking, review-model contract/error type, accounting,
  aggregate-cost and budget interfaces. Keep reviewer single-call semantics distinct
  from primary retry/fallback.
- Select one owner for canonical runner creation; avoid concurrent creation of
  `agent/runner.py` by 2.5 and 2.6 workers.
- Keep approval/reconciliation pending-action adapter and final summary publication
  in the visible coordinator/persistence boundary.
- Confirm run-location/executor rebinding updates every service explicitly on
  resume/continuation and continuation fallback.

## Pending acceptance work, not executed

After implementation is authorized, characterize ordered traces with deterministic
ports for check reuse before/after hook mutation, denied/cancelled checks, cancellation
while a started check runs, partial evidence writes, verification-time drift, reviewer
timeout/error/accounting, post-review token excess, final drift/read failure, no-change
baseline and resumed evidence, patch collection refusal, each terminal persistence
failure boundary, event redelivery/sequence recovery, and executor/lease cleanup.

Compare exact terminal reasons, schemas, artifact contents and call order. Include
existing approval-budget, cancellation, session-resume/continuation and SDK/import
compatibility cases. Main owns any tests, lint, startup, builds and final scoped commit
under the authorization current at that time. This document supplies design decisions
and frozen-source evidence only; Slice 2.6 is not implemented or validated here.

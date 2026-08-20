# M2 independent release review

Date: 2026-08-21
Baseline: `859db23`
Scope: uncommitted M2 implementation in `python-coding-agent`
Decision: **NEEDS WORK — do not close or commit M2 as implementation-complete yet**

## Severity summary

- High: 2
- Medium: 2
- Low: 1
- Confirmed strengths: the CLI owns the real `AgentRunner` loop; the gateway is a canonical
  translator rather than a raw proxy; provider shutdown is owned by ASGI lifespan; Codex credentials
  use a fixed audience, separate 0600 store, redacted errors, and one in-process refresh retry.

## Findings

### [HIGH] Resume does not reconcile a persisted pending approval

`AgentRunner._approval()` deliberately saves `phase=waiting_approval` and `pending_action` before it
calls `approval_policy.decide()` (`loop.py:239-255`). This is the correct write ordering for an audit
trail. However, `AgentRunner.resume()` only hydrates messages/counters (`loop.py:143-180`), and the
resumed `run()` emits `session.resumed` then immediately starts the next model request
(`loop.py:539-550`, `604-619`). It never examines `manifest.phase` or `manifest.pending_action`.

Therefore Ctrl-C/process death while the terminal is waiting at `Allow?` leaves an assistant tool
call without its required tool observation. On resume, the pending action is neither re-presented,
denied, cancelled, nor converted into an observation. A strict Chat/Responses provider may reject
that conversation; a permissive provider may continue from an ambiguous side-effect state. The
existing resume test interrupts only *after* a patch completed and checkpointed, so it does not
cover this state.

Release requirement: define and test an explicit reconciliation rule. The safe MVP is to re-present
the persisted request when no execution-start record exists, record the new decision, then execute
or append a denied observation exactly once. If exactly-once execution cannot be established after
`tool.started`, fail the resume with a precise manual-recovery status instead of asking the model to
continue an invalid history.

### [HIGH] Event and manifest persistence have an unrecoverable crash window

Every `_event()` first durably appends JSONL, then separately updates `session.json`
(`loop.py:185-204`). Resume requires the JSONL last sequence to equal the manifest last sequence
exactly (`session.py:304-333`). A crash after the append succeeds but before the manifest replace
succeeds leaves a valid next event in JSONL and a one-event-behind manifest. All later resumes reject
the session permanently as `manifest event sequence does not match events.jsonl`.

This conflicts with the broad README/stage claims that an interrupted non-terminal run is resumable.
The demonstrated Ctrl-C proves one safe interruption point, not crash safety across the persistence
protocol.

Release requirement: add deterministic recovery for a contiguous JSONL suffix (replay state or
truncate only an event proven not to represent an executed effect), or store the authoritative state
transition and event atomically in one journal. Add a fault-injection test between event append and
manifest save. Until then, document resume as checkpoint-bound and not crash-safe.

### [MEDIUM] Session budgets are not durable across interruption

The manifest declares `active_wall_time_seconds`, but the field is never read or updated. Each
`run()` creates a fresh `deadline = now + task.wall_time_seconds` (`loop.py:524-527`). In addition,
`step` is incremented in memory before the model request, while manifest `step` is updated only by a
later checkpoint. Repeated interruption during an in-flight provider request can therefore reset the
wall-time allowance and repeat the same logical step.

This weakens the stated bounded-loop invariant specifically on the new resume path. Persist elapsed
active time and the in-flight step/request state, then subtract elapsed time on resume. Include a
resume test showing that wall time and max steps cannot be replenished by restart.

### [MEDIUM] `ALLOW_SESSION` grants are represented in the manifest but never persisted or restored

`SessionManifest.granted_effects` exists (`session.py:92`), while `TTYApprovalPolicy` keeps grants
only in its private in-memory set. No production reference writes or reads `granted_effects`; the
only `.grants` assertion is the isolated policy unit test. A resumed process constructs a fresh
policy, so a prior “session” approval silently becomes no grant.

Conservative re-prompting is safe, but it contradicts the presence of durable grant state and makes
the user-facing word “session” ambiguous. Pick one contract: either restore only explicitly durable
grants after validating their scope, or remove the dead manifest field and document that
`ALLOW_SESSION` means the current process lifetime only. Add an integration test across resume.

### [LOW] Codex OAuth client is not closed when login fails before code exchange

`login_codex()` constructs `CodexOAuthClient` before opening the browser and waiting for the callback,
but closes it only inside `_exchange_codex_code()` (`cli.py:232-260`). Timeout, invalid callback,
port-in-use, or browser failure before exchange leaves the owned `httpx.AsyncClient` unclosed. This
does not expose the token, but it can produce resource warnings and makes auth lifecycle ownership
incomplete. Close the client in an outer `finally` that covers the entire login flow.

## Documentation truthfulness

The following statements are well supported:

- bare `pca` executes this repository's own Python `AgentRunner`; no official CLI launcher appears
  in the production path;
- `pca run` remains a distinct headless command;
- the gateway parses OpenAI Chat input into `ConversationItem`/`ToolDefinition`, invokes one pinned
  `ModelProvider`, rejects arbitrary model selection and remote bind addresses, bounds request size,
  redacts upstream errors, and closes the provider through lifespan;
- explicit `ModelProtocol` values exist for all built-in adapters and Codex Responses is separate
  from OpenAI Chat;
- the docs correctly label Codex OAuth as mocked/experimental, the failed Ollama coding eval as a
  failure, the local executor as unsandboxed, and Cloudflare deployment as deferred.

The following should change unless the high findings are fixed:

- `docs/stages/m2-...md` must not say “implementation complete”;
- `progress.md` must not mark general safe resume complete;
- README should say resume is supported only from a consistent saved checkpoint, and that an
  interruption during approval/tool execution can require manual recovery.

The runtime fallback `getattr(model, "protocol", model.provider_name)` also weakens the otherwise
explicit protocol boundary. It exists to accommodate an incomplete test double. Prefer requiring
the protocol declared by `ModelProvider` and update the double, so a future adapter cannot silently
persist a provider name as its wire protocol.

## Verification performed

```text
uv run ruff check .
All checks passed!

uv run pytest tests/test_interactive_runner.py tests/test_session.py \
  tests/test_approvals.py tests/test_gateway.py tests/test_codex_oauth.py \
  tests/test_oauth_login.py tests/test_cli.py -q
40 passed

uv run pytest -q
119 passed

git diff --check 859db23
passed (no output)
```

Passing tests establish the implemented happy paths, including safe-checkpoint resume, writer
fencing, gateway auth/lifespan, and mocked OAuth refresh. They do not cover the two release-blocking
crash windows above.

---

## Re-review after remediation

Re-review date: 2026-08-21
Decision: **NEEDS WORK — substantially improved, but one High and one Medium residual remain**

### Finding-by-finding result

| Original finding | Re-review result |
|---|---|
| HIGH: pending approval resume | **Partially fixed; High residual remains** |
| HIGH: event/manifest crash window | **Fixed for the reported mismatch and ambiguous-start cases** |
| MEDIUM: durable wall-time/step budgets | **Partially fixed; Medium residual remains** |
| MEDIUM: `ALLOW_SESSION` persistence | **Fixed** |
| LOW: OAuth client cleanup | **Fixed** |

The separate protocol-boundary note is also fixed: `AgentRunner` now requires `model.protocol`
directly and all reviewed adapters/test doubles declare it.

### [HIGH residual] A resolved approval can still strand an orphaned tool call

The new `_reconcile_interrupted_approval()` correctly converts an approval interrupted *inside*
`approval_policy.decide()` into a canonical failed `ToolObservation`, clears `pending_action`, and
records `approval.abandoned`. The new integration test exercises that exact state and proves the
patch is not executed before the model requests it again.

There is still a second pre-execution window. After `decide()` returns, `_approval()` clears
`pending_action` and persists the decision (`loop.py:278-294`), then emits `approval.resolved` and
returns (`295-303`). The caller does not persist `tool.started` until `loop.py:772-777`. If the
process stops anywhere after line 294 and before `tool.started` is committed, resume sees:

- no `pending_action`, so `_reconcile_interrupted_approval()` is a no-op;
- no `tool.started`, so the side-effect ambiguity validator permits resume;
- an assistant tool call with no matching `ToolObservation`.

The next operation is therefore another model request with an orphaned tool call. This is the same
provider-validity failure as the original finding, just after the user's decision rather than during
it. The added test interrupts only from inside `decide()` and does not cover post-decision/pre-start.

Release requirement: keep a durable action record through `approved_not_started`, or append a
canonical abandoned observation on resume whenever the latest assistant tool call has neither an
observation nor a committed `tool.started`. Add fault injection immediately after the approval
manifest save and immediately after `approval.resolved` append.

### Event/manifest result: fixed for the original High finding

`_event()` now atomically saves its full state snapshot and target sequence before appending JSONL.
Resume accepts only an exact match or a manifest exactly one event ahead, repairs the latter, and
retains the state snapshot. It rejects an exact-last `tool.started` or `verification.started`, where
completion cannot be proven. The new tests cover one-event-ahead repair and both ambiguous start
types. This closes the original unrecoverable one-event mismatch.

One non-blocking follow-up is worth tracking: verification outcomes are assigned to
`_last_verification` only after all commands finish, so a crash after a command returns while its
`verification.completed` event is state-ahead can recover without retaining that command's outcome.
The command may be rerun. Persist each outcome before its completed event if verification commands
must have at-most-once semantics.

### [MEDIUM residual] In-flight elapsed time is still lost on hard process death

Step and accumulated active time are now included in every state-first event snapshot, and resume
subtracts persisted `active_wall_time_seconds` from the new deadline. This fixes budget reset at
normal event boundaries, and the new test proves an already-persisted exhausted budget cannot be
replenished.

However, the active time stored by `model.requested` is measured just before the provider call. If a
provider call runs for several minutes and the process is killed before the next event, those minutes
never reach the manifest. Repeating that interruption can still replenish most of the wall-time
budget. The test manually writes a fully consumed budget; it does not simulate time spent between
the last event and hard termination.

Release requirement: persist a wall-clock `active_segment_started_at`/deadline that resume can
charge, or heartbeat elapsed time while a long provider/tool/check operation is active. A hard-kill
test should show that time between `model.requested` and restart is deducted.

### `ALLOW_SESSION`, OAuth cleanup, and protocol result: fixed

- `_approval()` consults manifest grants, saves `ALLOW_SESSION` effects, and emits
  `approval.reused`. Because resume hydrates the same manifest, grants survive process restart. The
  current test proves persistence/reuse within a run; a direct resume assertion would improve
  coverage but the production path is present.
- `login_codex()` now closes the owned client in the pre-exchange failure path, while
  `_exchange_codex_code()` retains close ownership once exchange begins.
- The protocol fallback was removed, so a provider missing the required wire protocol now fails
  instead of silently persisting its provider name as a protocol.

### Re-review verification

```text
uv run ruff check .
All checks passed!

uv run pytest tests/test_interactive_runner.py tests/test_session.py \
  tests/test_approvals.py tests/test_oauth_login.py tests/test_cli.py \
  tests/test_gateway.py tests/test_codex_oauth.py -q
47 passed

uv run pytest -q
126 passed

git diff --check 859db23
passed (no output)
```

The re-review accepts three findings as fixed and the event journal remediation as materially
correct. M2 should remain open until the post-decision/pre-start approval window is reconciled. The
in-flight budget residual can either be fixed in M2 or explicitly narrowed in the stage contract,
but the current unqualified “bounded across resume” claim is broader than the hard-kill behavior.

---

## Final re-review after second remediation

Re-review date: 2026-08-21
Decision: **NEEDS WORK — one approval-reuse release blocker remains**

### Accepted fixes

The two residuals from the previous re-review are fixed on the normal approval path:

- An allow decision now retains `pending_action` through the durable `approval.resolved` event.
  `tool.started`/`verification.started` is committed state-first, and only then does
  `_mark_approved_action_started()` clear the pending record. A crash before the started event is
  reconciled into a canonical non-executed observation; a crash after a durable started event is
  rejected as ambiguous. The new post-decision/pre-start fault-injection test covers the formerly
  missing window.
- `active_started_at` is persisted as a wall-clock segment marker. Resume charges the elapsed wall
  time left by a hard-killed process; graceful `finally` folds monotonic elapsed time into the base
  and clears the marker. The new past-timestamp test proves that a hard-kill interval cannot restore
  an exhausted budget.

The earlier state-first journal, exact-last side-effect rejection, grants persistence, OAuth cleanup,
and explicit protocol fixes remain intact.

### [HIGH residual] Reused session grants bypass the new pending-action protocol

`_approval()` has an early path for an effect already present in `manifest.granted_effects`
(`loop.py:253-261`). It emits `approval.reused` and immediately returns `ALLOW_ONCE`; it does **not**
set `pending_action=request`. The caller then emits `tool.started` later (`loop.py:805-810`).

Consequently, interruption after durable `approval.reused` but before `tool.started` recreates the
same orphaned tool-call window:

- no pending action exists for `_reconcile_interrupted_approval()`;
- no started event exists for the ambiguity validator to reject;
- the assistant tool call has no matching `ToolObservation` when resume requests the model again.

This affects the second and later modify/execute action after the user selects “allow session”. The
new fault-injection test exercises a fresh `ALLOW_ONCE`, not the `approval.reused` fast path.

Release requirement: even when permission is reused, persist the concrete action request as pending
before emitting `approval.reused`, and clear it only after the durable started event, exactly like a
fresh allow decision. Add fault injection between `approval.reused` and `tool.started` and assert the
stale action is abandoned without execution or orphaned provider history.

### Final re-review verification

```text
uv run ruff check .
All checks passed!

uv run pytest tests/test_interactive_runner.py tests/test_session.py \
  tests/test_approvals.py tests/test_oauth_login.py tests/test_cli.py \
  tests/test_gateway.py tests/test_codex_oauth.py -q
48 passed

uv run pytest -q
127 passed

git diff --check 859db23
passed (no output)
```

No regression appeared in the implemented fixes. M2 can pass independent release review after the
grant-reuse path participates in the same durable pending/start protocol and its fault test passes.

---

## GO/NO-GO final verification

Final review date: 2026-08-21
Verdict: **GO — M2 passes independent release review**

The final blocker is closed. When an effect is already covered by `granted_effects`, `_approval()`
now saves the concrete request as `pending_action` before emitting `approval.reused`
(`loop.py:253-268`). The caller uses the same state-first `tool.started` event and
`_mark_approved_action_started()` clearing path as a fresh approval (`loop.py:812-821`). Thus:

- interruption before `tool.started` leaves a recoverable, provably unexecuted pending action;
- interruption after durable `tool.started` remains fail-closed as an ambiguous side effect;
- normal reuse retains the user's session grant without adding another prompt.

The new `test_resume_reconciles_reused_grant_before_started_event` exercises the actual fast path:
the first patch establishes the session grant, the second modify reaches `approval.reused`, the
test interrupts immediately before its `tool.started`, and resume completes with the calculator
patch and note patch each present exactly once. This directly covers the prior release blocker.

### Final gate

```text
uv run ruff check .
All checks passed!

uv run pytest tests/test_interactive_runner.py tests/test_session.py \
  tests/test_approvals.py tests/test_oauth_login.py tests/test_cli.py \
  tests/test_gateway.py tests/test_codex_oauth.py -q
49 passed

uv run pytest -q
128 passed

git diff --check 859db23
passed (no output)
```

All five original findings and both residual approval/budget findings are now resolved to the M2
contract. The earlier note about persisting each individual verification outcome before its
`verification.completed` event remains a non-blocking durability enhancement if future check
commands require at-most-once result retention; it does not invalidate the current fail-closed
side-effect or verified-success contract.

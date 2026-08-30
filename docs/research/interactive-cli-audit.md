# Interactive CLI architecture audit

Date: 2026-08-21
Scope: current staged worktree only; read-only audit of production and tests
Target: make `coding-agent` an interactive default with live tool trace, approvals for edits and
repository-code execution, resumable sessions, while preserving headless `coding-agent run`

## Evidence and baseline

- The repository has no commits yet; all 29 product/test/docs files are staged additions. `.research/`
  is untracked and is the only area written by this audit.
- Local verification passed: `uv run pytest` reports **55 passed** in 7.93s,
  `uv run ruff check .` passed in the independent test audit, and `git diff --cached --check`
  reports no staged whitespace errors.
- `uv run coding-agent --help` currently shows help and one `run` command because the Typer app is
  configured with `no_args_is_help=True`. There is no default interactive path.
- `uv run coding-agent run --help` confirms that headless execution currently requires `--repo`,
  `--task`, `--model`, and at least one `--check`, with explicit `--tool-calling` and
  `--unsafe-local-exec` acknowledgements.
- No external web evidence was used. The audit is entirely based on the staged local source,
  tests, generated CLI help, and successful local test execution.

## Executive conclusion

The provider adapters, immutable contracts, path/runtime protections, disposable workspace,
bounded tool implementations, verification result model, and append-only artifact format are good
foundations and should be reused. The missing features are not a thin Typer/UI addition, however.
`AgentRunner.run()` currently owns initialization, model turns, tool dispatch, verification, event
writing, checkpointing, and terminalization in one uninterrupted method. Approvals and resume must
interpose *inside* that control flow, so the loop needs a modest state-machine refactor before an
interactive shell can be correct.

Recommended shape:

```text
Typer commands                application/session layer             existing core

coding-agent (interactive) -> InteractiveCLI -> AgentSession -----> ModelProvider
                                             |       |             ToolExecutor
coding-agent run (headless) -> HeadlessCLI ---+       +-----------> LocalGitWorkspace
                                                     |
                                ApprovalPolicy <-----+----> EventSink(s)
                                SessionStore  <------+      JSONL + console trace
```

Do not put `typer.confirm()` calls in `ToolExecutor` or `AgentRunner`. The core must remain usable
headlessly and testably. Approval is an injected async policy; rendering/input is a CLI adapter.

## Current flow and the exact blockers

Current staged flow:

```text
cli.run
  -> build TaskContract and ModelProvider
  -> AgentRunner.run
       -> require global allow_unsafe_local_exec
       -> resolve base SHA / create run dir / clone workspace
       -> initialize messages and checkpoint
       -> repeat:
            model.complete
            for each tool call: ToolExecutor.execute immediately
            checkpoint only after tool completion
            if model returns text: run every verification immediately
       -> write patch / checkpoint / result
```

Feature gaps:

| Target | What exists | Why it is insufficient |
|---|---|---|
| Default interactive CLI | Typer app plus explicit `run` command | `no_args_is_help=True`; callback is empty; all task/provider configuration is embedded in `run()` |
| Live tool trace | `RunEvent`, `EventWriter`, `tool.started`, `tool.completed`, verification events | Events only go to JSONL; no subscriber/fan-out API. `tool.completed` omits bounded observation content and `model.completed` omits assistant content, so replay cannot render a useful transcript |
| Edit approval | `apply_patch` is bounded and path checked | The loop calls it immediately. Tool definitions have no effect/risk metadata and there is no permission decision contract |
| Execution approval | Global `allow_unsafe_local_exec` gate; exact allowlisted `run_check` argv | One startup boolean authorizes all model-requested checks and all final verification. Final `_verify_all()` bypasses tool-call interception entirely |
| Resume | `request.json`, `checkpoint.json`, workspace, events, patch/test artifacts | `run()` requires a new run directory and workspace. No checkpoint loader, state hydration, event sequence recovery, or writer lock exists. `active_writer_token` is only stored, never checked |
| Headless compatibility | `coding-agent run` and `AgentRunner.run()` | Must remain non-interactive and retain current JSON/exit-code behavior; an injected allow/deny policy can preserve it |

Two subtle correctness issues matter for resume:

1. A checkpoint is written after a tool finishes, not before it starts. A crash during
   `apply_patch` or `run_check` leaves an ambiguous action that must not be blindly replayed.
2. `Checkpoint.last_action_fingerprint` is stored, but `_repeat_count`, final summary, last
   verification, event sequence, and any pending action/approval are not. Restoring only the
   message list would change guards and can duplicate work.

## APIs that can be reused as-is

| Existing API | Reuse | Notes |
|---|---|---|
| `TaskContract`, `Limits`, `VerificationCommand` | Yes | Stable session/task input. Persist the effective task in `request.json` as today |
| `Message`, `ToolCall`, `ToolObservation`, `ModelTurn`, `Usage` | Yes | Suitable provider-neutral conversation and action values |
| `RunResult`, `VerificationOutcome`, `RunStatus` terminal values | Mostly | Preserve result JSON. Add non-terminal paused/waiting state to session state, not to terminal `RunResult.status` literals |
| `ModelProvider.complete()` / `aclose()` and all four real adapters | Yes | Interactive mode does not require provider streaming to deliver live tool traces; streaming text can remain later work |
| `SafePathPolicy` | Yes | Approval must never weaken path enforcement |
| `LocalGitWorkspace.prepare()` | Yes for new sessions | Resume needs a separate `open_existing()`/validation path rather than calling `prepare()` again |
| `ToolExecutor` bounded methods and `execute()` | Mostly | Keep actual validation/execution. Add effect metadata outside or alongside definitions; route all calls through an approval-aware dispatcher |
| `ReviewablePatch` / `reviewable_patch()` | Yes | Use for edit preview, recovery checks, and final result |
| `run_bounded_command()` and sanitized environment | Yes | Execution approval is separate from sandbox/process bounds |
| `RunEvent` and atomic JSON/JSONL helpers | Yes | Keep on-disk compatibility; generalize delivery through an event sink/fan-out |
| Existing artifact names/layout | Yes | A resumed run should continue the same run directory and append to the same event log |

## APIs that need refactoring or extension

### 1. Split orchestration from the monolithic runner

Refactor `AgentRunner` into an application-facing `AgentSession` (lifecycle/persistence) and a
small step-driven engine. The useful minimum is not a public `step()` that leaks internal phases;
it is a resumable `run()` whose blocking dependencies are injected:

```python
class ApprovalPolicy(Protocol):
    async def decide(self, request: ApprovalRequest) -> ApprovalDecision: ...

class EventSink(Protocol):
    async def emit(self, event: RunEvent) -> None: ...

class AgentSession:
    @classmethod
    async def create(cls, task, model, run_root, *, approvals, events, ...) -> "AgentSession": ...

    @classmethod
    async def resume(cls, run_dir, model, *, approvals, events, ...) -> "AgentSession": ...

    async def run(self) -> RunResult: ...
```

Compatibility: retain `AgentRunner(...)` as a facade or alias that constructs a new session with a
headless policy. Existing Python callers and tests can continue calling `await runner.run()`.

The engine must checkpoint at safe state transitions: model turn committed, action requested,
approval resolved, action completed/denied, verification completed, and terminal state. It should
not encode UI concepts.

### 2. Add explicit tool effects and an approval dispatcher

Use an enum, not tool-name conditionals spread through the CLI:

```python
class ToolEffect(StrEnum):
    READ = "read"
    MODIFY = "modify"
    EXECUTE = "execute"


class ApprovalRequest(ContractModel):
    request_id: str
    run_id: str
    action_id: str
    effect: ToolEffect
    tool_call: ToolCall | None
    command: VerificationCommand | None
    reason: Literal["model_tool", "final_verification"]
    preview: str


class ApprovalDecision(StrEnum):
    ALLOW_ONCE = "allow_once"
    ALLOW_SESSION = "allow_session"
    DENY = "deny"
    CANCEL = "cancel"
```

Classification for the current six tools:

| Tool/path | Effect | Approval |
|---|---|---|
| `list_files`, `read_file`, `search_text`, `git_diff` | `READ` | automatic |
| `apply_patch` | `MODIFY` | prompt before execution; preview bounded patch and paths |
| `run_check` | `EXECUTE` | prompt before executing exact argv |
| harness final `_verify_all()` | `EXECUTE` | same dispatcher, reason=`final_verification`; never bypass approval |

`run_check` deserves execute approval even though argv is allowlisted: it runs repository code and
can mutate the disposable workspace or host-visible resources. `apply_patch` approval does not
replace `SafePathPolicy` or patch-size checks; validation should happen before the prompt when it is
side-effect free so the user sees normalized paths, and all validation must run again immediately
before execution.

On `DENY`, append a failed `ToolObservation` for a model tool call (so the model can adapt) and an
`approval.denied` event. On denied final verification, finish as cancelled or remain paused based on
the explicit interactive command; never claim verified completion. `CANCEL` should yield a terminal
cancelled result with a clear reason.

Headless `run` uses a non-prompting policy:

- modification: allow, matching current patch behavior;
- execution: allow only when current `--unsafe-local-exec` is supplied;
- otherwise fail before provider calls, preserving the current fail-closed behavior and exit code;
- do not make headless runs hang waiting for stdin.

### 3. Generalize event delivery for immediate trace

Keep `RunEvent` and `EventWriter`, but inject a sink into the runner:

```python
JsonlEventSink(path)  # wraps current EventWriter
ConsoleEventSink(renderer, stream)  # interactive, stderr by default
CompositeEventSink((jsonl, console))
```

Emit only after the persistent JSONL append succeeds, then render, so what the user sees has an
audit record. If console rendering fails, the durable session should remain authoritative.

Enrich events with bounded, redaction-aware data sufficient for live and resume replay:

- `model.completed`: include bounded assistant text when present;
- `tool.requested` (or retain `tool.started` after approval): name, call ID, effect, safe argument
  preview;
- `approval.requested` / `approval.resolved`: action ID, effect, decision, reason; never secrets;
- `tool.completed`: status plus bounded observation preview/output byte count/hash;
- `verification.started` / `.completed`: name, exact argv, status, duration, bounded output preview;
- `session.resumed`: prior checkpoint sequence and recovery mode.

Do not render the final `RunResult` JSON intermixed with a machine-readable headless stream. Use
stderr for optional headless traces and stdout for the existing final JSON.

### 4. Implement real session persistence and resume

Add a versioned `session.json` manifest beside the existing artifacts. Keep `request.json` as the
immutable effective task. A minimum manifest/state includes:

- schema version, run/task IDs, provider name and model ID (never credentials), base SHA;
- lifecycle phase, step, messages, cumulative usage, final summary;
- last fingerprint **and repeat count**;
- last event sequence, last verification outcomes, terminal marker;
- pending action record, approval audit, grant scope, and accumulated active wall time;
- writer lease identity and timestamps.

Resume algorithm:

1. Resolve `--resume` as one safe run ID under `run_root` (or an explicit validated run directory).
2. Acquire an exclusive per-session writer lock before reading mutable state. A token merely written
   into JSON is not fencing; use an OS lock or atomic lock file with stale-owner handling.
3. Strictly load `request.json`, session/checkpoint, and events; require matching run ID, task ID,
   base SHA, monotonic event sequence, and supported schema version.
4. Validate existing `workspace/` is a Git worktree pinned to the recorded base commit and that its
   reviewable diff stays inside `allowed_paths` and limits. Never clone or reset it on resume.
5. Recreate `SafePathPolicy` and `ToolExecutor`; hydrate messages, usage, step, repetition guard,
   verification/test-log state and event sequence. Persist grant history for audit, but do not
   reactivate process-local `ALLOW_SESSION` grants after a resume unless a future explicit option
   says to do so.
6. Reconcile any pending action before another model call.
7. Append `session.resumed`, continue events in sequence, and release the writer lock in `finally`.

Crash semantics must be explicit:

- `REQUESTED` or `APPROVED` but not executing: prompt again after process restart.
- `EXECUTING apply_patch`: inspect the workspace and action fingerprint. Do not auto-reapply unless
  the implementation can prove whether the exact patch landed. If ambiguous, pause for operator
  recovery.
- `EXECUTING run_check`: never assume success and never auto-rerun repository code without a fresh
  execution approval.
- `COMPLETED` action: restore its observation and continue; never execute it again.
- Terminal session: `resume` should report the existing result, not start another run.

The existing `Checkpoint` can be extended into this state or kept as a compact compatibility view,
but there must be exactly one authoritative resumable state document. Avoid two independently
writable truths.

The time budget also needs a declared interactive meaning. Persist accumulated **active engine
time**, stop that clock while waiting for human input/approval, and restore only the remaining
budget on resume. Resetting the full wall-time budget on every resume enables unbounded execution;
counting time spent at an unattended approval prompt makes normal interactive use fail spuriously.

### 5. Make interactive the default without breaking `run`

Recommended command contract:

```text
coding-agent                         # interactive: new session, prompts for missing config/task
coding-agent --resume RUN_ID         # interactive: reopen existing run
coding-agent run ...                 # current non-interactive JSON contract
coding-agent run ... --trace         # optional live trace to stderr; still never prompts
```

Typer changes:

- replace `no_args_is_help=True` with `invoke_without_command=True`;
- callback receives `typer.Context`; if `ctx.invoked_subcommand is None`, launch interactive;
- keep `run` as an explicit command with current required options and exit codes;
- extract duplicated parsing/building from `run()` into pure functions (`parse_checks`,
  `build_task`, `build_model`) shared by both modes;
- require a TTY for interactive prompting and return a clear usage error on piped/non-TTY input;
- catch `KeyboardInterrupt`/EOF, checkpoint, close the model exactly once, and return exit 130/0 as
  documented;
- use only stdlib/Typer initially; a full TUI dependency is not required for a correct first cut.

Interactive prompts should show repository, base commit, allowed paths, exact checks, provider/model,
and the no-sandbox warning before starting. Credentials remain environment-only and must never be
persisted or echoed. A resumed session may reconstruct provider/model identity, but still obtains
fresh credentials from environment.

## Module / file / API map

| File | Change | Public/internal API |
|---|---|---|
| `src/coding_agent/contracts.py` | Extend | `ToolEffect`, `ApprovalRequest`, `ApprovalDecision`, versioned `SessionState`/`PendingAction`; keep current contracts compatible |
| `src/coding_agent/approvals.py` (new) | Add | `ApprovalPolicy` protocol, `HeadlessApprovalPolicy`, session grant logic; no Typer imports |
| `src/coding_agent/events.py` | Extend | `EventSink` protocol, `JsonlEventSink`, `CompositeEventSink`; retain `EventWriter` aliases |
| `src/coding_agent/session.py` (new) | Add | `SessionStore.create/load/save`, exclusive `SessionLease`, workspace/event validation and hydration |
| `src/coding_agent/loop.py` | Refactor | approval-aware action dispatch, safe-point checkpointing, create/resume paths; preserve `AgentRunner.run()` facade |
| `src/coding_agent/tools.py` | Small extension | central tool-effect metadata and side-effect-free preview/validation; keep actual handlers and `execute()` bounds |
| `src/coding_agent/runtime.py` | Extend | `LocalGitWorkspace.open_existing()`/`validate_existing()`; no resetting a resumed workspace |
| `src/coding_agent/console.py` (new) | Add | `ConsoleEventSink`, `InteractiveApprovalPolicy`, prompt/render functions; all terminal I/O lives here |
| `src/coding_agent/cli.py` | Refactor | default interactive callback, `--resume`, shared builders, unchanged explicit `run` output/exit semantics |
| `src/coding_agent/__init__.py` | Extend carefully | export stable approval/session/event interfaces only; avoid exporting console internals |
| `README.md`, `docs/stages/*`, `docs/progress.md` | Update after implementation | command examples, approval semantics, resume guarantees and crash limitations |

Suggested dependency direction:

```text
cli -> console -> approvals contracts
cli -> session/loop -> models, tools, runtime, events, contracts
tools/runtime/models -> contracts (never cli/console/session UI)
```

## Test map

Keep the existing 55 tests as regression coverage. Add tests at the boundary where each new failure
could cause unauthorized or duplicate side effects.

| Test file | Reuse / new cases |
|---|---|
| `tests/test_cli.py` (new) | `coding-agent` with no subcommand enters interactive mode; `--resume`; non-TTY refusal; EOF/Ctrl-C; `run` remains non-interactive, emits one final JSON document, and preserves exit 0/1/2; traces go to stderr |
| `tests/test_cli_tty.py` (new, small) | stdlib `pty` + subprocess smoke test for real TTY detection, prompt visibility and Ctrl-C; keep most command cases in fast `CliRunner` tests |
| `tests/test_approvals.py` (new) | read tools auto-allow; patch prompts; checks prompt with exact argv; allow-once vs session grant; deny becomes observation; cancel terminalizes; headless never calls prompt; unsafe flag gates execute |
| `tests/test_events.py` (new) | JSONL-first fan-out ordering; console render for model/tool/approval/verification; bounded previews; renderer failure does not corrupt JSONL; resumed sequence remains monotonic |
| `tests/test_session.py` (new) | create/load round trip; strict schema/ID/base-SHA validation; exclusive writer contention; stale/invalid state refusal; existing workspace validation; terminal resume returns existing result |
| `tests/test_resume_e2e.py` (new) | resume after read, denied edit, approved/completed edit, and failed verification; same run/workspace; no duplicate patch/check; usage/step/repetition restored; fresh model credentials not persisted |
| `tests/test_resume_recovery.py` (new) | crash at requested/approved/executing/completed action phases; ambiguous patch pauses; executing check demands fresh approval; corrupt/truncated last event handling is deterministic |
| `tests/test_loop_e2e.py` | Parameterize injected approval/event sinks; assert final verification also requests execute permission; keep current artifact/source-unchanged/repetition/time-budget cases |
| `tests/test_tools.py` | Effect classification completeness for every definition; preview and execution revalidation; denied action never calls handler |
| `tests/test_runtime.py` | `open_existing()` accepts valid pinned workspace; rejects missing `.git`, wrong HEAD/base, symlink/path escape, or out-of-policy/oversized diff |
| `tests/test_models.py` | Existing provider round trips are sufficient; add only if resume serialization exposes provider metadata loss |

Critical acceptance scenarios:

1. **No unauthorized mutation:** deny `apply_patch`; workspace bytes and diff are unchanged.
2. **No unauthorized execution:** deny both model `run_check` and automatic final verification; a
   sentinel command proves no subprocess ran.
3. **No duplicate side effect on resume:** crash after a completed edit/check, resume, and prove its
   handler invocation count remains one.
4. **Single writer:** two resume attempts against one run; exactly one acquires the lease.
5. **Headless compatibility:** the existing scripted E2E flow and JSON result remain byte/schema
   compatible aside from intentionally added artifacts/events.
6. **Trace/audit agreement:** every displayed action/decision has the same action ID and ordering in
   `events.jsonl`.

Use `typer.testing.CliRunner` for command parsing and isolated terminal flows, injected fake
prompt/renderer objects for deterministic approvals, and `ScriptedModel` for all resume E2E tests.
Do not require real provider network calls.

## Recommended implementation sequence

1. Add tool effects, approval contracts/policies, and tests. Route model tools **and final
   verification** through one dispatcher while keeping `AgentRunner` behavior via a headless policy.
2. Add event sink fan-out and console trace, retaining JSONL format and sequence guarantees.
3. Extract versioned session state/store and writer lease; checkpoint before/after actions and add
   recovery tests.
4. Add existing-workspace validation and `AgentSession.resume()`; prove no duplicate side effects.
5. Refactor CLI builders, make the no-subcommand path interactive, add `--resume`, and keep `run`
   compatibility tests.
6. Update docs only after tests define the exact approval and crash/recovery contract.

## Decisions to keep explicit

- Approval and sandboxing are independent. A user approving execution does not make hostile code
  safe; retain the strong local-execution warning.
- Resume should continue a run, not copy its conversation into a new run ID. “Fork session” can be a
  later feature.
- Provider streaming is not needed for “live tool trace.” Current adapters are explicitly
  non-streaming; trace model/tool lifecycle immediately now and add token streaming separately.
- A full-screen TUI is unnecessary for this milestone. A line-oriented Typer console is easier to
  test and preserves the headless boundary.
- Never persist API keys, auth headers, or raw provider client configuration. Persist provider/model
  identity only.
- Never claim exact-once recovery for an action that crashed mid-execution unless reconciliation can
  prove its effect. Pause and ask is safer than replay.

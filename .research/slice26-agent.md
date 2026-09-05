# Slice 2.6 production handoff

Status: production source applied, not validated or declared runnable. Main retains
integration and goal completion authority. No staging or commit was performed.

## Preserved input and ownership

Before production edits, `cp -p` preserved the delivered Slice 2.5 coordinator at
`.research/slice25-frozen/loop.py`. Inputs were the Slice 2.5 implementation and defect
reports, the Slice 2.6 design, the current coordinator, and necessary existing port,
state, model-accounting, lifecycle, context-blocking, runtime and event/sink contracts.
Initial oversized tool output was truncated; missing coordinator sections were
retrieved in bounded ranges before edits. No written output was subsequently reviewed.

Owned production files:

- `src/looplane/agent/verification.py`: explicit VerificationInputs/VerificationPorts,
  CheckExecutor/VerificationApproval/JsonWriter contracts; ephemeral VerificationCache,
  CheckedCommandEvidence and ManualCheck; declared-check pass, verification-state
  fingerprints, evidence publication/artifacts, manual-check cache admission and the
  bounded advisory reviewer lane.
- `src/looplane/agent/completion.py`: CompletionInputs/CompletionRequest/CompletionPorts,
  typed ResultAccounting, patch-source and checkpoint contracts; patch collection,
  terminal artifact/result assembly and ordered checkpoint/event/result persistence.
- `src/looplane/agent/runner.py`: canonical composition and visible engine/final-state
  decisions, using the extracted services and existing 2.4/2.5 owners.
- `src/looplane/loop.py`: explicit compatibility entry and late-bound constructor,
  executor/workspace, event writer, configuration, process/environment, persistence,
  retry-delay, session-claim and run-ID dependency seams.

No additional agent state or port files were changed. In particular,
`agent/subagent_dispatch.py`, `agent/tool_scheduler.py`, `agent/model_calls.py`,
`agent/ports.py`, TurnState, RunPersistence and BoundedRunLifecycle are untouched.
No tests, CLI, terminal, tooling, console, SDK or main-plan files were edited.
The external mechanical application script is `/tmp/looplane-slice26-extract.rb`;
its successful execution is a file-writing result, not a source correctness gate.

## Responsibility and ordering

VerificationService receives only typed inputs, explicit durable TurnState, its own
separate ephemeral cache, an executor protocol, cancellation event, optional review
model and narrow callbacks. It never receives a runner or proxies runner attributes.
The runner creates a current service view on each entry so restored tasks, rebuilt
executors and rebound run locations do not leave stale service references. TurnState
remains the sole persisted verification/usage authority; cache and check log are not
restored as trusted durable evidence.

The implementation extraction retains these intended boundaries and ordering:

- Hash the same ordered check configuration plus workspace stamp, including argv and
  timeout, with the same JSON/hash representation and fixed fingerprint timeout.
- Invalidate manual check cache and executor outcome before execution; capture the
  start stamp; let the existing scheduler emit started/completed and finish the
  post-tool hook; only then capture the end stamp and admit successful matching
  evidence. Manual and final-check command-log quoting remain distinct.
- Keep final verification phase/budget decisions visible in the runner. Reuse requires
  cached/start/current stamps, passing outcome and matching argv. Approval denial,
  cancellation before/after execution and partial-evidence persistence keep their
  existing distinct branches. Started event precedes pending-action clearing.
- Publish outcomes before computing the ending stamp; publish verified/invalidated
  stamp before verification.json and test.log; reject detected drift after artifacts.
  Existing unreadable-fingerprint exception ordering is not broadened.
- Reviewer remains optional, read-only, single-call and advisory. Usage accounting,
  cache trace, review.md and reviewer completion event retain their original order;
  existing timeout/provider/I/O/value errors are handled without swallowing cancellation.
- Proposed summary persistence, no-change decision, verification repair feedback,
  review, token gate, final unreadable/drift gate and terminal reason selection stay
  visible in `_run_turns`. Success passes the already-collected patch to completion.
- Patch collection uses ordinary `asyncio.to_thread`, not the cancellation-draining
  tool wrapper. Patch failure catches only the former ToolExecutionError/TimeoutError
  cases and retains the former summary/reason/error behavior.
- Completion captures default verification before patch collection and accounting
  after patch collection, then persists checkpoint, terminal event, result.json and
  executor close in that order. Optional cache/review artifact keys remain conditional;
  no new verification.json result artifact key or transaction/retry is introduced.
- Existing persistence still owns sequence advancement, writer tokens, manifest saves
  and checkpoint details. Existing lifecycle still owns active-time settlement and
  lease release after the engine exits; completion does not acquire a second lease
  or own session/turn/approval/context/compaction lifecycle.

## Canonical imports and compatibility transition

Canonical runner imports ToolExecutor from `looplane.tooling.executor`, matching
Dirac's announced same-signature canonical executor, and ToolExecutionError from
`looplane.tooling.types`. It has no direct import of tools.py, loop.py, runtime.py,
CLI or TUI facades. Execution/environment/workspace helpers use canonical modules.
Existing console sink classes are still imported from their current owning module;
this task does not relocate or edit that independent surface.

The legacy AgentRunner subclasses the canonical runner and inherits its constructor
and classmethod resume. Explicit static dependency overrides resolve old facade
symbols when invoked rather than copying mutable globals into another module.
Canonical child dispatch is given `type(self)` as the existing runner-factory port,
so the compatibility subclass retains its dependency seams in child construction.
Native leaves do not import the compatibility subclass. Former private method entry
points delegate to named services; no App/private-field mixin, new session model,
broad getattr proxy, sys.modules alias or mutable cross-module registry is introduced.

Compatibility coverage still needs an authorized test/contract pass. The facade
explicitly retains selected established runner dependencies and retry constants,
not an unrestricted star export of every transitive import. Pure policy details now
belong to canonical services; external code inspecting ephemeral private cache tuple
representations is not a confirmed supported contract. Any additional established
patch/import contracts discovered by main must be reconciled before declaring gates.

## Known blockers and pending gates

The documented Slice 2.5 defects remain untouched and uncorrected:

1. Inferred malformed transaction-preparation indentation in subagent_dispatch.py.
2. Required child runner factory not forwarded by the extracted child-launch call.
3. Residual undefined-self transaction execution reference instead of the typed port.

See `.research/slice25-agent-defect.md`. Correction requires the pending explicit
approval; this task does not supply it. Canonical runner imports that dependency,
so this source cannot yet be called runnable, even independently of unrun 2.6 gates.
Dirac's canonical executor availability/signature integration is another handoff
assumption, not an import result verified by this worker.

No tests were edited or run. No Ruff, formatter, build, AST/compile/import smoke,
architecture graph, git command, validation or post-write source review was run.
Historical Slice 2.4/1.4/1.5 gates do not establish anything about this extraction.

When separately authorized, main should resolve the known 2.5 defects and run the
production syntax/import/Ruff gates, followed by focused evidence reuse, manual
post-hook drift, denied/cancelled checks, partial artifacts, reviewer accounting and
failure, final drift/unreadable workspace, patch failure, terminal ordering, resume,
continuation/rebind, writer lease, retries, subagent factory and facade patch/import
contract tests. Whole-suite, dependency-boundary, startup and packaging gates remain
main's responsibility. Slice 2.6 is source delivered, not a completed verified slice.

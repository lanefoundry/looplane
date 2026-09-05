# Final Wave 1/2 architecture acceptance audit

Date: 2026-09-05. Verdict: **substantial extraction proven, unconditional final acceptance not yet supported**.

Scope: current shared production tree, plan/tracker, selected source/test contracts, bounded AST graph analysis and import/factory probes. Only this report was written. No production/test/plan edits, pytest suite, lint/build, Git or commit was performed. Import probes used the existing interpreter with `-B`; they did not construct a runtime process or contact a provider.

Classification:

- **Proven**: observed source ownership or a specific bounded check in this audit. This is not automatic proof of unchanged runtime behavior.
- **Missing**: required evidence or an explicitly required interface is not established by the inspected material.
- **Contradicted**: a concrete source path or bounded probe conflicts with the stated requirement.

## Findings, ordered by acceptance significance

### F1. Contradicted: the live Codex registry path still selects a compatibility facade

`src/looplane/runtime_registry.py:100` declares `native_session="looplane.codex_conversation.IsolatedCodexConversation"`. `_resolve_class` uses the nonliteral `module_name` in `importlib.import_module` at `runtime_registry.py:59`. Command construction consumes that path at `commands/bootstrap.py:108` and `commands/bootstrap.py:393`.

The bounded resolution probe observed:

```json
{"runtime":"codex-cli","path":"looplane.codex_conversation.IsolatedCodexConversation","resolved_module":"looplane.codex_conversation","canonical_identity":false,"canonical_subclass":true}
```

The implementation really is inherited from the canonical workspace/audit host, so this is not evidence that its audit logic was lost. It does contradict the blanket claim that internal default composition always selects canonical owners without facade dependencies. The canonical destination exists at `runtimes/codex/conversation.py:26`.

The current boundary test does not catch this route: its import collector recognizes only literal importlib arguments (`tests/test_modularization_boundaries.py:29`), and its FACADES set also omits `looplane.codex_conversation`. A source graph with zero forbidden edges is therefore insufficient to accept registry-selected factories.

Acceptance action: main should reconcile the registry's canonical default path and add/check resolved-factory ownership coverage. Keep the old public facade available for callers. No fix was made here.

### F2. Contradicted: CLI replacement-factory compatibility is conditional on __module__

`src/looplane/cli.py:120` selects compatibility overrides by implementation origin. At `cli.py:126`, a replacement with the legacy or canonical implementation's `__module__` is silently replaced by the canonical default, even when it is a distinct explicitly assigned class.

A bounded in-memory probe assigned a distinct AgentRunner subclass to `looplane.loop.AgentRunner`, called `_native_runtime()`, and restored the original symbol in a finally block:

- Distinct replacement with `__module__="looplane.agent.runner"`: **not honored**.
- Distinct replacement with `__module__="audit_fake"`: **honored**.
- Unpatched canonical defaults: **correct** for both native runner/error and terminal App.

This supports the limitation already disclosed in `.research/cli-canonical-integration.md`; it is not a hypothetical failure inferred only from an absent test. It does not invalidate explicit RuntimePorts injection or prove that existing tests fail. It contradicts unrestricted replacement/monkeypatch compatibility: class origin is not an explicit-override signal.

Acceptance action: either preserve the actual override contract without origin guessing, or explicitly narrow and accept the compatibility contract with supporting tests. No policy decision or correction was made here.

### F3. Contradicted at the remaining public-port seam: agent scheduling still uses an executor private method

At `src/looplane/agent/runner.py:1032`, `_prepare_scheduled_call` supplies `self._executor._validate_unified_diff` to the scheduler. The scheduler has a narrow callable boundary, but its composition still depends on the canonical coordinator's private compatibility helper instead of a public validation contract/owner.

This is one concrete residual private cross-owner dependency, not a pervasive mixin or proxy architecture. Two other AST hits are compatibility inspection/delegation:

- `terminal/app.py:3174` exposes `self._terminal_projection._projection` for compatibility inspection.
- `tooling/executor.py:480` forwards to `self.git._reviewable_patch_pinned`.

Those two should be recorded as explicit exceptions or moved behind public inspection/delegation APIs. They do not by themselves show that projection/Git implementation still lives in the root coordinator. Do not claim literally zero cross-owner private access until these exceptions are reconciled.

### F4. Literal state-machine requirement is not met by the public run method; conceptual extraction is present

`src/looplane/agent/runner.py:1162` implements `AgentRunner.run()` as a lifecycle call. The actual visible transitions are in `_run_turns` at `agent/runner.py:1167`, a 602-line method that covers initialization/resume, context updates, model calls, tool decisions/batches, verification and terminal conditions.

This differs from Slice 2.6's explicit wording that **AgentRunner.run()** remains the visible state machine. It does not demonstrate a failed extraction merely because the method is long: the loop really delegates retry, scheduling, checkpoint/context, verification and completion to separate services. The broader Wave 2 requirement that AgentRunner describe the loop is substantially satisfied.

Acceptance action: reconcile the literal public-method requirement with the deliberately split lifecycle/turn-engine design. If the named private turn engine is accepted as the visible state machine, document that bounded interpretation; do not represent run() itself as containing transitions. No runtime defect is asserted from this naming/delegation discrepancy.

## Fresh bounded evidence

### AST/import graph

One bounded production graph pass parsed 162 modules and found 650 recognized module edges. It included regular/relative imports and literal import_module/__import__ calls, including imports within functions and type-checking blocks.

Results:

- Syntax parsing of the scanned production Python modules completed.
- Strongly connected components with cycles: **0**.
- Recognized feature-to-facade edges: **0**, with codex_conversation included in this audit's forbidden set.
- No feature-class `__getattr__`, `__getattribute__` or `__setattr__` proxy was found. The CLI's module-level lazy __getattr__ is an intentional compatibility export.
- Three simple nested cross-owner private attribute hits were found and classified above.

Limits: this is not a complete Python data-flow analysis. Registry strings, arbitrary dynamic imports, closures and reflective operations are not all modeled. F1 demonstrates an actual blind spot; zero SCCs/edges do not prove canonical factory resolution for every runtime.

### Canonical/legacy import and identity probes

The following checks returned true in this audit:

- CLI native defaults resolve canonical AgentRunner and UnsafeLocalExecutionError.
- CLI terminal default resolves canonical terminal.app.looplaneApp.
- Legacy loop.AgentRunner, tools.ToolExecutor, tui.looplaneApp and codex_app_server.CodexAppServerSession subclass their canonical implementations.
- console.EventSink and sdk.EventSink are the shared events.EventSink object.
- tools.ToolExecutor._tool_definitions is the canonical tooling.definitions.tool_definitions function.

These probes establish import/default/identity facts. They do not establish behavioral equivalence of every legacy patch target, lifecycle or provider.

## Requirement matrix

| Requirement | Classification | Evidence and qualification |
|---|---|---|
| 1.1 canonical leaf definitions/types and public imports | Proven, bounded | `tooling/definitions.py`, `tooling/types.py`; `tests/tooling/test_leaf_contracts.py`; shared function identity probe passed. External aliases have contract tests at `tests/contracts/test_external_agent_compatibility.py:32`. |
| 1.2 actual correlation/event/approval extraction | Proven, structural | `runtimes/codex/correlation.py:19`, `event_mapper.py:89`, `approval_mapper.py:90`; canonical session shell at `session.py:71`; owner replay tests at `tests/runtimes/codex/test_protocol_owners.py`. |
| 1.2 process/RPC versus workspace audit boundary | Proven, structural | Session owns process/RPC shell; `runtimes/codex/conversation.py:26` owns workspace/audit host. Registry routing still contradicts a blanket no-facade-default claim, F1. |
| 1.3 CLI use cases/bootstrap/injection | Proven, with compatibility exception | `commands/bootstrap.py:349`, `:378`, `:393`; native resume/construction and registry session selection remain injected. `cli.py:131` and `:142` lazily choose canonical defaults. F2 limits blanket compatibility acceptance. |
| 1.4 terminal feature owners | Proven, structural | Separate approvals/composer/scroll/transcript/tool_widgets/selectors/onboarding/clipboard/links modules under terminal; widget and leaf compatibility tests remain. This audit did not rerun keyboard/PTY/UI tests. |
| 1.5 explicit projection state/view commands | Proven | `terminal/projection.py` defines immutable view-command values and TerminalProjection; no App import/proxy. `tests/terminal/test_projection_binding.py:84` asserts immutable tool commands. |
| 1.5 binding versus controller/store lifecycle | Proven structurally and by existing contract assertions | `terminal/conversation_binding.py:46` owns attachment tokens, leases, subscriptions and resource cleanup; append/checkpoint serialize and fence writes, then call the existing Store. It does not create a new runtime session. Old-writer drain/lease tests at `tests/terminal/test_projection_binding.py:149`, `:327`; approval detach at `:357`. |
| 1.5 App composition/input ownership | Proven, structural | `terminal/app.py:239` owns App; command/input routing remains there (`:1541`), teardown at `:597`, view application at `:3176`. Large App size alone is not a failed boundary. Private compatibility inspection remains F3. |
| 2.1 MCP independent owner | Proven, structural | Canonical executor constructs McpBridge at `tooling/executor.py:111`; bridge owns clients/discovery/routes. New bridge tests cover metadata/refresh/errors; no executor object is stored by bridge. |
| 2.2 policy/version/file/search/patch/snapshot owners | Proven, structural; detailed runtime evidence limited | Separate policy and tooling read_versions/filesystem/search/patch_validation/patching/snapshots owners exist. Shared records and dependencies are wired in executor constructor. Existing root tools tests cover exact replacement/fsync rollback and limits, but no dedicated 2.2 owner test files were found. |
| 2.3 concrete Git/check/transaction ownership | Proven, structural | WorkspaceGit at `tooling/git.py:69`, AuthorizedChecks at `verification.py:59`, StructuredPrograms at `transactions.py:39`. Executor passes atomic_writer.replace, git.reset_paths/run/reviewable_patch directly (`executor.py:168` onward), replacing prior transitional self-capturing callbacks. |
| 2.3 ToolExecutor thinness | Proven by responsibility, not merely line count | Class is composition plus compatibility delegates, definition assembly and dispatch. Largest methods: constructor 143 lines, execute 173 lines; other methods <=18 lines. No filesystem/Git/verification/transaction implementation remains in it. |
| 2.4 owned state/checkpoints/context/lifecycle | Proven, structural with direct tests | TurnState/ContextState in agent/state.py; RunPersistence `checkpoints.py:73`; ContextUpdate `context.py:79`; BoundedRunLifecycle `run_lifecycle.py:42`. Direct tests cover persisted sequencing, restoration, additions rather than history mutation and active-time cancellation. |
| 2.5 model/tool/subagent services | Proven, structural | `agent/model_calls.py:220`, `tool_scheduler.py:130`, `:246`, `:288`; explicit PreparedToolCall/ports in agent/ports.py. Subagent construction requires runner_factory at `subagent_dispatch.py:342`, forwards it at `:579`; graph has no loop/subagents SCC. F3 is a remaining private validation seam. |
| 2.6 verification decisions versus already-authorized execution | Proven, structural | Agent VerificationService at `agent/verification.py:100` decides approvals/check evidence/review. Tooling AuthorizedChecks executes named commands. Completion inputs/ports/finish at `agent/completion.py:49`, `:68`, `:96` own final assembly/persistence. |
| 2.6 AgentRunner.run visible state machine | Contradicted literally; broader intent proven | See F4. Actual transition coordinator is `_run_turns`, not public run(). |
| No feature private proxies/mixins | Proven for observed AST patterns; exceptions noted | No generic class attribute proxy found. Three private access sites remain; this audit is not a universal reflective-data-flow proof. |
| Wave 1 new-runtime change locality | Missing complete demonstration | Generic bootstrap/registry construction supports localization, but no synthetic adapter-locality test was established. F1 also shows registry defaults need canonical-owner validation. |
| Scoped slice commits and exact final acceptance | Missing in this audit | Tracker explicitly retains audit/commit obligations. No Git commands were permitted or used. |

## Test and historical Gate reconciliation

The corrected shared Gate report is `docs/plans/runs/modularization-corrected-gate-2026-09-05.md`. Its raw pytest log ends with **1475 passed, 2 skipped in 247.75s**; this audit read that raw result. The report also records passing repository Ruff, canonical imports/SDK, paired startup and bounded archives containing all 162 production modules.

These are stronger and later evidence than the old isolated CLI/worker counts. They still do not prove every acceptance invariant: F1 and F2 can coexist with the passing suite. This audit did not rerun the Gate or compare source hashes to its execution snapshot, so it does not certify byte-for-byte candidate identity.

Existing contract evidence located:

- `tests/agent/test_state_context.py:92`, `:117`, `:135`: manifest/event ordering and checkpoints.
- `tests/agent/test_state_context.py:216`: context additions do not mutate history.
- `tests/agent/test_state_context.py:255`, `:280`, `:311`: cancellation/active time and resume identity.
- `tests/terminal/test_projection_binding.py:149`, `:188`, `:214`, `:327`: stale writes/messages, close retries and checkpoint cancellation.
- `tests/commands/test_composition.py:163`, `:199`, `:242`: injected construction/controller reuse/resource cleanup.
- `tests/contracts/test_discovery_capabilities.py:20`: constructed live capability mapping; this is not a canonical import-path identity check.
- `tests/test_tools.py:969`, `:1004`, `:1017`, `:1044`, `:1109`, `:1174`, `:1223`: fsync rollback, output bounds, secret isolation, sandbox failure, process timeout, pinned review and cumulative limits.
- `tests/test_loop_e2e.py:675`, `:762`, `:1007`, `:1073`, `:2628`, `:2657`, `:2722`: scripted sequence, continuation, failed side effects, verification drift, retries/fallback and review lane.

Missing evidence is narrower than saying these features are untested. Dedicated direct-owner tests for 2.2/2.3 and extracted 2.5/2.6 service-port behavior were not found in the respective feature directories; root integration tests remain. The planned independent sequence fixtures and low-dependency owner assertions should be reconciled explicitly with those existing root tests before claiming the proposed test layout/coverage is complete.

## Remaining acceptance gates

1. Resolve or explicitly accept F1-F4, separating architectural contract decisions from runtime regressions.
2. Cover registry-resolved canonical ownership and the CLI origin-filtered override case; current static graph checks do not establish either invariant.
3. Reconcile independent service/sequence-fixture evidence for scheduling, completion and file/snapshot/Git owners. Do not remove meaningful root integration coverage just to match directory names.
4. Record final candidate identity and scoped commits after any accepted corrections. Reuse the corrected Gate only to the extent its snapshot still applies; changed paths require their authorized gates.
5. Keep process-contract obligations separate: the remaining-gates document still lists UTF-8/output bounds, materialized stdin, callback blocking, deadline/cancellation/process-group limits and non-macOS platform evidence. The authorized literal-root macOS exception is documented, not evidence of general permission expansion by these module extractions.
6. Retain the explicit Python/no-Rust decision. Conditional Rust implementation is not a missing Wave 1/2 deliverable.

This audit does not mark the persistent modularization goal complete. It establishes real owner extraction and passing bounded graph/import facts, while recording concrete composition/compatibility exceptions and remaining acceptance evidence.

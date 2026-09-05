# Slice 1.5 design: terminal application, projection, and binding

Status: analysis only, 2026-09-05. Slice 1.4 is frozen. This document is the only
file written for this task; no production/test edits, test runs, staging, or commits.
Implementation requires main's authorization after its Slice 1.4 scoped commit.

The recommended split is a canonical `terminal/app.py` coordinating explicit
`TerminalProjection`, `ConversationBinding`, and `TerminalView` instances. Projection
returns typed view commands and owns semantic display state. Binding fences delivery,
tracks UI attachment and borrowed resources, and sequences lease handoff. View owns
Textual widget references. Existing `ConversationController` and `ConversationStore`
retain runtime and durable lifecycle authority. No new agent session or App mixins.

## Source anchors and concrete move boundaries

Line numbers refer to the Slice 1.4 snapshot inspected for this analysis; use method
names as anchors when integrating other slices. The remaining `tui.py` is 3,630 lines.

| Current source | Proposed owner | Extraction boundary |
|---|---|---|
| `tui.py:277` App declaration, bindings, CSS, constructor/composition/mount/resize | `terminal/app.py` | Canonical `looplaneApp`, same constructor keywords and run/export surface; compose canonical widgets and owned services |
| `tui.py:300` through input handlers starting at 343; 900, 1043, 1402, 1471, 1532, 3422, 3524 | `terminal/app.py` | One explicit input precedence decision; Textual decorators, command dispatch, FIFO draft queue, idle confirmation and stop/exit UX stay together |
| `tui.py:801`, 1090, 1150, 1188, 1251, 1714, 1775, 1811 | `terminal/app.py` initially | Configuration and selector orchestration; call existing onboarding/catalog/config APIs. Fence async continuations; do not reopen Slice 1.4 workers |
| `tui.py:3000` `event_received` | `terminal/projection.py` | Entire native audit-event mapping, loading changes, tool/verification presentation, best-effort log projection |
| `tui.py:3176` `external_event_received` | `terminal/projection.py` | Entire bounded external-event mapping; retain ask/agent display differences and ephemeral activity semantics |
| `tui.py:3218` `conversation_runtime_event_received` | `terminal/projection.py` | Entire canonical runtime-event mapping, streaming buffers, tool correlation, approval presentation, telemetry/model/compaction observations |
| `tui.py:2664`, 2772-2952 | `terminal/projection.py` and `terminal/view.py` | Stream flushing and tool/verification semantic transformations belong to projection; widget mounting and widget alias lookup belong to view |
| `tui.py:2360` result rendering within `_run_agent` | `terminal/projection.py` | Extract result summary fallback, usage/elapsed display, failure detail, verification fallback/reuse, changed-file notice and final patch view commands together; not just the three event handlers |
| `tui.py:2646`, 2692-2771, 2806, 2953-2998, 884, 1440-1470 | `terminal/view.py` | Transcript reset/mount/update, tool groups, visibility/loading controls, unseen-item/scroll state, widget verbosity; retain App command entry methods as narrow delegates if tests need them |
| `tui.py:679`, 708, 716` | `terminal/projection.py` plus App context snapshot | Produce metrics/status data using explicit time, mode, config, runtime identity and queue inputs; App initiates updates |
| `tui.py:3612`, 3617` and reducer writes | `TerminalProjection` | Own existing `TranscriptReducer` instance and semantic finalization. App snapshots final text before invalidating/unmounting |
| `tui.py:229`, 249` | `terminal/conversation_binding.py` | `TextualEventSink` becomes a captured binding event sink with typed post/record ports; compaction checkpoint collector moves with it, typed with the canonical checkpoint type |
| `tui.py:2529`, 2548` | `terminal/conversation_binding.py` | Capture immutable append target, sequence authorized text recording, then dispatch through a live attachment fence |
| `tui.py:2471`, 2638`, 634` | `terminal/conversation_binding.py` | Track attached resource identities, cleanup failures for retry, detach/release lease only after in-flight writes drain; do not implement controller shutdown internals |
| `tui.py:2505`, 2567`, 2606` | App calling Store through a binding write gate | USER_MESSAGE and terminal-event lifecycle still uses existing Store methods/validation. Binding supplies captured identity and serialization, not new lifecycle policy |
| `tui.py:1865-2190` compaction/new/resume/rewind/clear | App calling existing controller/resource and Store ports | These are UI use-case requests. Acquire/read/close/replace order remains explicit; binding handles attachment swap, view projector handles replay. Do not hide all this in a new binding session class |
| `tui.py:2190` `_run_agent` | `terminal/app.py` | Keep visible bounded-run coordination, startup recovery, cooperative shielding, result/persistence and next-prompt dispatch; replace display bodies with projector commands and resource bookkeeping with binding calls |
| `tui.py:3382` `request_approval` | App async presentation adapter plus `TerminalView` | Eager pending tool command, mount-after ordering, focus, await existing decision future, remove exact prompt; policy remains `terminal/approvals.py` |
| `tui.py:746`, 2625` | App async adapters initially | Statusline subprocess and bounded patch read remain separate I/O operations; return captured results for fenced application, not I/O inside projection |
| `tui.py:196`, 222` | Small canonical helper or `terminal/app.py` export | Rewind label helper and version lookup retain compatibility exports; no reason to add lifecycle ownership here |

Keep `terminal/status.py`, `types.py`, and `events.py` as the existing canonical leaf
owners. Add narrowly named `view_commands.py`, `projection.py`, `view.py`,
`conversation_binding.py`, and `app.py`; do not put all new state into leaf `types.py`.
Existing `transcript_export.py` remains the sole implementation of export bounds and
redaction. `console.LiveEventProjection` can remain a reused audit-line formatter;
console is not a forbidden facade in the current boundary test. Do not copy its
algorithm into a terminal-specific duplicate during this slice.

## Explicit state ownership

| Owner | Typed state | Existing fields absorbed |
|---|---|---|
| `TerminalProjection` | `ProjectionState`, `ToolPresentation`, `StreamPresentation`, metrics snapshot | audit projection/errors; tool title/status/detail records and logical aliases; completed verification IDs; pending reuse payloads; rendered-git-diff flag; approval-to-action IDs; per-turn stream text/visible offsets/flush times; telemetry, reported model, elapsed/stream count; existing export reducer |
| `TerminalView` | `ViewIndex`, `ScrollState` | actual `ToolActionBlock`/`ToolGroupBlock`/`MessageBlock` objects, active tool group, unseen IDs, anchored/follow state. No runtime session or lease |
| `ConversationBinding` | `AttachmentState`, immutable `WriteTarget`, tracked resource records and pending UI tasks | generation/attachment identity, conversation ID/lease/local turn binding, received-chunk and received-message evidence, persistent resource identity list, detach/close status |
| App | `InputState`, `RunPresentationContext`, configuration and current handles | draft/history/queue, selector, permission mode/process grants, active runner, result/error, interrupt/exit confirmations, config/catalog UI; requested context ID and orchestration policy |
| `ConversationController` | Existing locks, session, pending context, history, turn handle | Remains unchanged: turn ordering, consume runtime stream, reply approval, interrupt deadline, native/local compaction and hooks, context-provider/injection work |
| `ConversationStore` | Existing manifest/event/lease types | Remains unchanged: append lifecycle validation, writer token, claim/resume/fork/clear and atomic log/manifest completion |

Do not store widgets in projection state or expose App private fields through a
Protocol. Define tool status/detail-kind literals or reuse a compatible canonical
union without narrowing currently rendered values accidentally. Convert raw audit
verification mappings once into a `VerificationPresentation` record; preserve the
current fallbacks for strings/unknown/missing data.

The binding's received-message evidence has a distinct purpose from reducer state:
`TextualEventSink.emit()` currently records streamed output before the queued Textual
message is processed. Result fallback must see accepted/persisted message evidence
even when the final runtime message is still in the UI queue. Do not replace this
with a flag updated only by the renderer, or summaries will be printed twice.

Do not reproduce native session history in binding. `_ask_history` is UI semantic
replay for recreating a runtime and has current bounds (last 12 entries, 8,000 chars
per entry, bounded 48,000-char prompt); it is not a new durable store. Keep this
presentation/replay policy explicit in App for now.

## Projection API and view commands

Proposed conceptual signatures (names are design, not implemented code):

```python
class TerminalProjection:
    def begin_turn(self, context: RunPresentationContext) -> tuple[ViewCommand, ...]: ...
    def project_run(self, event: RunEvent, context: ProjectionContext) -> ProjectionOutcome: ...
    def project_external(self, event: ExternalAgentEvent,
                         context: ProjectionContext) -> ProjectionOutcome: ...
    def project_runtime(self, event: ConversationRuntimeEvent,
                        context: ProjectionContext) -> ProjectionOutcome: ...
    def finish_result(self, result: RunResult,
                      context: ResultPresentationContext) -> tuple[ViewCommand, ...]: ...
    def present_patch(self, run_id: str, filename: str,
                      preview: str) -> tuple[ViewCommand, ...]: ...
    def export(self, conversation_id: str | None) -> str: ...
```

`ProjectionContext` is frozen input: mode, current time, running/stop status, queue
length, accepted-stream evidence and optional result, plus the necessary identity
labels. Inject a clock into the caller and formatter into the projector instead of
reading module globals or looking at a widget to compute state. `ProjectionOutcome`
contains an immutable ordered command tuple and typed observations where App needs
to update existing orchestration policy; no function closures, `setattr`, widget
names carrying arbitrary mutations, or generic `dict[str, Any]` command language.

| Command | Payload and renderer effect |
|---|---|
| `ResetTranscript` / `ClearActivity` | Reset only the targeted view state; semantic reducer reset is an explicit projector operation |
| `AppendMessage` | Stable view item ID, role and text; creates one message widget |
| `AppendAssistantText` | Turn/item ID and incremental suffix; creates or appends to the correlated assistant block |
| `AppendTimeline` | Item ID, title, detail, severity |
| `UpsertTool` | Logical action ID plus complete immutable presentation snapshot; no UI object returned to projection |
| `AliasTool` | Alias ID and original ID; maps both IDs to one widget; preserve verification reuse ordering |
| `SetLoading` | Label, `LoadingPhase | None`, indicator visibility |
| `SetStatus` / `AppendActivity` | Plain text or an explicitly typed dim-log style; retain current escaping/markup settings |
| `SetMetrics` / `SetContextLabel` | Frozen widget input fields; reported model stays distinct from selected model override |
| `SetActivityVisible` / `TrackTranscriptItem` | Visibility or stable ID; scroll decisions use live view state |
| `SetSelectorOptions` | Selector instance token plus option tuple, rejected if selector instance changed |
| `RequestComposerFocus` | Expected attachment/input revision; only applies if focus precedence still permits it |

Use stable aliases instead of comparing Python `id(widget)` to deduplicate orphan
verification settlement. Preserve IDs already relied on by the App and tests;
new message/timeline item IDs can come from an injected ID supplier.

`TerminalView.apply(commands)` is the only general widget mutation interpreter. Its
constructor accepts concrete widget references or a small typed view host containing
only widget lookup, deferred refresh scheduling, and current input/fence queries.
It must not receive an arbitrary App to discover runner, result, permission or lease
state. Widget refs may be installed at mount; do not force querying before compose.
Synchronous public App adapters such as `_ensure_tool_action` can project, apply, then
return the renderer's indexed widget for existing tests. They are delegators, not
parallel semantic implementations.

Projector must preserve these non-obvious existing rules:

- Runtime text flush occurs at newline, 96 pending chars, 80 ms, or explicit final
  flush before tool start and turn completion. Preserve exact condition ordering.
- Tool output over 48,000 chars retains first/last 24,000 and the truncation marker;
  failed edits render plain errors rather than diff syntax.
- Verification reuse may arrive before its original tool; final result fallback
  must update the same card and avoid duplicate semantic export rows.
- A successful edit after a successful git diff invalidates the final-preview flag.
- Native `LiveEventProjection` duplicate/out-of-order suppression currently governs
  audit log lines; remaining widget branches still run. Do not silently add stronger
  event deduplication under the claim of a pure extraction.
- Streaming text enters export on semantic completion; creating the initially empty
  assistant widget must not create a duplicate export row. Keep final-result and
  cancelled-turn transcript behavior characterized.
- The metrics footer uses input tokens for context percentage; `/context` and the
  auto-compaction rearm branch use total tokens. Preserve rather than normalize.
- Runtime telemetry/compaction observations may inform App's existing rearm/reminder
  state. Projection must never start compaction, inject context, or decide approvals.

## Binding, generation, and persistence write fence

There is no current independent UI subscription to `session.events()`. The controller
already consumes that iterator and calls an event sink. "Binding" means attachment
of this sink and its UI message delivery, not a second runtime stream reader.

Use two identities rather than overloading the current integer generation:

```python
@dataclass(frozen=True)
class ViewToken:
    attachment_epoch: int
    run_generation: int

@dataclass(frozen=True)
class WriteTarget:
    token: ViewToken
    conversation_id: str
    local_turn_id: str
    lease: ConversationWriterLease
    lease_token: str
```

The local Store turn ID is not necessarily the runtime event's `turn_id`.
`_begin_conversation_turn()` allocates a local UUID, whereas runtime events use the
controller/session turn ID. Preserve that distinction; checking equality would
reject valid events. A compaction operation also has a different runtime turn ID.

Generation write rules:

1. Capture a token when constructing the turn sink, scheduling delayed work, starting
   a configuration/catalog operation, or starting a patch/statusline read. Never
   retrieve "the current generation" when a stale continuation wakes up.
2. Reject stale delivery before changing binding evidence or projector state. After
   any awaited record operation, check again before setting received-message flags
   or posting a Textual message. The receiving App handler checks the token again.
3. Project and apply a command batch synchronously within one event-loop callback
   after admission. If a view action awaits a mount/remove, recheck after the await
   before focus, scroll or status changes. Deferred callbacks capture the token and
   recheck at execution, not only scheduling.
4. Advance attachment epoch on successful conversation/context replacement, reset,
   detach and unmount; advance run generation for a new bounded turn. Preserve the
   old attachment when candidate resume/cleanup fails. Do not clear current state
   merely because a candidate lease was acquired.
5. Keep admitted events from a just-finished turn deliverable until the existing
   event queue has drained or the next run/attachment invalidates them. Invalidating
   on `runner.run()` completion alone can discard its queued final events.
6. Revocation blocks future UI writes immediately. It does not cancel a Store
   append already admitted. `ConversationStore.append()` shields its inner task and
   waits for the log and manifest to agree before propagating cancellation
   (`conversation.py:643`). Preserve this contract.
7. Route App-issued turn appends and sink-issued text appends through the same
   binding write gate. Capture the lease and local turn ID before awaiting and hold
   an operation lock through the append. Rotation first denies new writes to the old
   target, drains admitted writes, then releases its lease. This is a linearization
   boundary for UI attachment, not a new Store lifecycle engine.
8. `_finish_conversation_turn` and `_fail_conversation_turn` use the captured target;
   after await, clear chunk/turn state only if that target is still active. A stale
   finally block must not clear a new turn's binding or runner/resource fields.
9. Public lease `active`/token checks support admission, but the Store remains the
   writer-token authority. A pre-await boolean check cannot fence an in-flight
   `to_thread` write; do not release a lease while such an operation is pending.

Same-generation callbacks need finer tokens too. Use a statusline refresh revision
so an older command result cannot replace a newer one during the same turn. Capture
selector instance identity, provider and configuration revision for catalog refresh;
checking only `kind == 'model'` can target a replacement selector. Focus requests
also check input precedence so a late callback cannot steal focus from an approval.

Keep failure/rollback ordering reviewable. `_resume_conversation` currently acquires
and loads a candidate, closes the old resources, then releases/swaps the old lease;
a failed candidate must release only its own lease. `_new_conversation` must retain
current context when cleanup fails. Resource cleanup runs on the owning event loop,
closes in reverse order, and retains failures for retry, as `aclose_resources()` does.
Cancellation still calls the existing runner/controller paths; do not change the
5-second escalation/grace policy or invent another interrupt deadline in binding.

Background task bookkeeping can move into binding as UI attachment work, including
warmup/statusline tasks. Any additional subprocess termination policy on statusline
timeout is a separate behavior fix and should not be smuggled into this extraction.

## Canonical App and legacy dependency injection

`terminal/app.py` must import `ExternalAgentEvent` from `looplane.external_agents`,
not the old `looplane.backends` import still present in `tui.py:40`. Do not import
`tui`, `cli`, `loop`, or any other forbidden facade from a terminal feature, including
inside TYPE_CHECKING, dynamic imports, or a runtime `sys.modules` lookup.

Add a small frozen `TerminalDependencies` (in `terminal/app.py` or a dedicated leaf
ports module) with actual substitutions used by the App: copy function, selection
reader, token formatter, monotonic clock, version supplier and async config saver.
Canonical defaults point directly to canonical modules. Keep runtime construction
in the existing `RunnerFactory` constructor input. Avoid a broad mutable service
locator or callbacks for every App method.

The facade's `looplaneApp` should be a narrow subclass of canonical `looplaneApp`
that passes late-bound forwarding callbacks. Its body has no rendering, turn,
permission or persistence algorithms. Example design:

```python
# In compatibility tui.py only:
class looplaneApp(_CanonicalApp):
    def __init__(self, **kwargs):
        super().__init__(
            **kwargs,
            dependencies=TerminalDependencies(
                copy_native=lambda text: copy_with_native_command(text),
                token_formatter=lambda count: format_token_count(count),
                # Equivalent late-bound forwarding for other retained seams.
            ),
        )
```

Implement the full current explicit constructor signature in the real adapter, or
preserve introspection deliberately; the abbreviated signature above illustrates
injection only. Canonical App feeds the formatter to canonical RuntimeMetrics and
projection. Never capture a patched function as a constructor default at import
 time: tests or users may patch after constructing the App.

| Contract observed | Preservation strategy |
|---|---|
| `tests/test_tui.py:2094`, 2120 patch `tui.copy_with_native_command` | Facade's late-bound copy callback; canonical direct users use canonical dependency defaults |
| `tests/terminal/test_leaf_compatibility.py:132` patches `tui.format_token_count` | Retain Slice 1.4 RuntimeMetrics compatibility adapter, and route facade App metrics/result formatting through the same late-bound callback |
| `tests/test_cli.py:2270` and related tests replace `tui.looplaneApp` | Keep root CLI's lazy terminal factory injection; do not make `commands/chat.py` read tui or try to mirror module assignments |
| Legacy `TextualApprovalPolicy(app, grants)` | Keep Slice 1.4 adapter; canonical App constructs the callback-based canonical policy directly |
| Legacy event sinks imported from tui | Reexport a canonical sink if its constructor becomes compatible via a public binding port; otherwise retain a thin facade constructor adapter |
| Feature widget/type import identity | Retain direct reexports for Slice 1.4 widgets and leaf types; only App and existing compatibility adapters need subclassing |
| `final_transcript_text`, `last_error`, `run(inline=...)` | Unchanged public App interface; snapshot export before teardown invalidates view/binding |
| Tests mutate `_generation`, telemetry, stream count, resource list, etc. | Keep temporary narrow forwarding properties or update tightly scoped implementation tests to owned state. No duplicate shadow dictionaries, `__getattr__` proxy, or generic private-field port |

A facade subclass is intentionally not identical to canonical App; document/test
`issubclass` and behavioral compatibility instead of enforcing identity here.
Textual decorators stay on canonical App, inherited once. Do not add duplicate
message handlers to the facade subclass.

### CLI worker handoff

Current CLI worker code already has the appropriate seam:
`commands/ports.py:65` defines `RuntimePorts.terminal_app`, `commands/chat.py:143`
obtains its factory through that callback, and root `cli.py:126` `_terminal_app()`
performs the lazy legacy lookup. Preserve that callback for old CLI tests that patch
`tui.looplaneApp`. New direct canonical integrations can lazily return
`terminal.app.looplaneApp`. Merely replacing root `_terminal_app` with a canonical
lookup would break those existing tests, even if tui still reexports the class.

Expose a read-only `runtime_context_id` property on canonical App. CLI's existing
`terminal_context_id` callback can use that public property; its root compatibility
adapter may retain fallback for existing fake Apps. This removes the temporary
private `_runtime_context_id` dependency from the canonical path without making App
import commands. Keep all terminal imports inside the actual TUI route/factory to
preserve CLI import/help/config laziness. Coordinate these CLI edits with its owner;
this analysis does not edit those files.

## Implementation order after authorization

1. Add command/state records and projector with pure sequence fixtures; extract all
   three event maps AND result/verification/stream finalization together. Reuse the
   existing export reducer and audit formatter.
2. Add view interpreter with explicit widget refs and owned indices, then replace
   current App display helpers with short delegates. Keep legacy helper returns
   where tests depend on widget lookup.
3. Add binding with captured tokens/write targets, admitted-write drain, received
   output evidence and resource tracking. Rewire sinks and every asynchronous UI
   continuation; keep Store/Controller algorithms unchanged.
4. Move the reduced App to `terminal/app.py`, inject dependencies and leave explicit
   facade exports/adapters. Coordinate canonical factory/property readiness with
   CLI worker and check that canonical import cannot load tui or cli.
5. Run focused sequence, interaction, cancellation, persistence, facade, import and
   startup tests, then main runs full gates and package checks before scoped commit.

## Required characterization and acceptance

- Pure projector tests for native/external/canonical event sequences, malformed
  audit projection, stream flush boundaries, missing tool references, verification
  before/after reuse, cancelled/failing result fallback, and semantic export parity.
- Deterministic barriers around sink Store append, patch preview read, statusline
  output, selector refresh, approval mount/remove, and deferred focus. Rotate or
  unmount while paused, then resume: no old mutation of new projection/view/state.
- Store admission test: an in-flight append drains against its captured old lease;
  new target does not receive its chunk, old lease is not released early, and stale
  finally cannot clear a new turn. Test admitted cancellation and append failure.
- Ensure queued final runtime events are rendered and exported once even when
  runner completion and event delivery interleave. Result fallback uses binding
  evidence, not solely Textual handler progress.
- Existing tests named `test_force_stop_during_conversation_startup_restores_ui_and_draft`,
  `test_failed_resume_preserves_current_conversation_and_lease`,
  `test_new_conversation_does_not_rotate_when_runtime_cleanup_fails`,
  `test_delayed_event_from_previous_generation_is_ignored`,
  `test_provider_close_failure_still_cleans_worker_state`,
  `test_native_compaction_completed_reinjects_workspace_context_on_next_turn`, and
  persistent-resource/verification/queue/export tests remain acceptance anchors.
- Repeat Slice 1.4 focus/Enter/arrows/numbers/Escape/copy/resize/unmount and PTY tests.
  Preserve approval eager-tool ordering and the scroll freeze while choices are open.
- Canonical import subprocess tests for `terminal.app`, projection and binding;
  enforce no facade imports and no new production import cycle. Add canonical
  `terminal.app` to lazy-import exclusions where appropriate.
- Explicit tests for copy and formatter patches after App construction; CLI tests
  replacing `tui.looplaneApp` must continue to launch their fake via root injection.
- No projection code calls Store, session, runner or provider. No binding code
  consumes session events or makes approval/compaction policy decisions. App event
  handlers are admission plus projection/application, not relocated event trees.

## Observed issues to keep separate from extraction

Static reading, not reproduced in this analysis: `_run_configuration` appears to
continue to `selection.config` after a `None` selection (`tui.py:819` onward), and
some background callbacks currently check widget existence rather than attachment
identity. The latter is the explicit write-fence scope; any configuration-cancel bug
fix should be separately characterized and agreed with main, not bundled silently.
`is_mounted` alone is not a teardown signal, as Slice 1.4's modal characterization
already established; use explicit binding closed/epoch state and actual unmount.

This design does not assert new runtime or gate evidence. Current source ownership
and callback paths were inspected directly. Whole-suite validation and Slice 1.4
commit remain main's work against its frozen snapshot.

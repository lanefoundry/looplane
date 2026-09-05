# Slice 1.5 terminal controllers

Status: production implementation in progress; latest source changes await main's
validation and integration. No staging or commits. Slice/goal remains active.
Implementation began only after main authorized it following the Slice 1.4 snapshot
 gate. This report does not claim repository-wide completion or package validation.

## Delivered owners

- `src/looplane/terminal/app.py`: canonical `looplaneApp`, Textual composition,
  input precedence, UI use-case coordination and one typed view-command interpreter.
  Keeps the existing Store/Controller call sequence, configuration/command flows,
  queue behavior, cooperative cancellation, and semantic export-before-unmount.
- `src/looplane/terminal/projection.py`: `TerminalProjection`, typed projection
  context/state, owned logical tool/stream/verification records and frozen view
  command dataclasses. Owns all three native/external/canonical event maps, result
  fallback presentation, patch presentation, stream finalization, verification
  correlation/reuse and existing semantic export reducer.
- `src/looplane/terminal/conversation_binding.py`: `ConversationBinding`, immutable
  `ViewToken` and `WriteTarget`, captured event sink, canonical checkpoint collector,
  admitted-write gate, resource identity tracking/cleanup, UI task tracking, pending
  approval cancellation on detach, attachment epochs and same-turn refresh revisions.
- `src/looplane/tui.py`: 202-line compatibility facade with old imports and narrow
  App/metrics/approval/event-sink constructor adapters. No runtime event trees or
  App implementation remain here.
- `src/looplane/terminal/events.py`: retains the same canonical message classes,
  positional constructor arguments and handler names; adds optional keyword-only
  `attachment_epoch` metadata. Production binding sinks always attach an epoch;
  old manual message constructors remain compatible.
- `tests/terminal/test_projection_binding.py`: 15 deterministic ownership,
  compatibility, projection and attachment-race tests. Existing TUI tests were not
  rewritten to hide behavior regressions.

No Slice 1.4 widget files, CLI/commands, console, SDK, Controller, Store, provider,
wire-protocol, execution, permission or sandbox implementation files were edited.

## Explicit boundaries

Projection consumes canonical event values, not widgets or an App. Its `project`,
`finish_result`, `prepare_approval` and `present_patch` methods return immutable typed
view-command tuples. Tool state updates emit immutable snapshots; verification
aliases map to the same logical action and renderer widget. Other projector entry
methods enqueue explicit commands drained by the App, never opaque mutation closures.
The projector imports neither Textual nor a compatibility facade.

The App interprets message/stream/tool/timeline/loading/status/activity commands.
Typed refresh commands request chrome recomputation from the explicit App-owned
configuration/queue inputs. Typed context observations update the existing UI
compaction rearm/reminder flags; projection never starts compaction or decides an
approval. Widget references and scroll/focus remain renderer-owned in canonical App,
not in the semantic projector. A separate `view.py` was unnecessary for the assigned
file scope; this is an interpreter with explicit command inputs, not an App mixin.

`ConversationController` still consumes runtime events, orders turns, responds to
runtime approvals, handles interruption, injected context and compaction. Binding
subscribes through the existing sink callback; it does not open a second runtime
iterator or create another Session model. `ConversationStore` still validates and
persists all durable lifecycle events and writer tokens.

Named compatibility properties forward to the single owned projection/binding
state, preserving existing state-inspection tests. There is no broad getattr proxy,
private-field mixin, duplicated session history owner, or global facade resolver.

## Generation and cleanup behavior

- Event sinks capture epoch, run generation and the original Store lease/local turn
  identity. Local persisted turn IDs remain distinct from runtime event turn IDs.
- Admission rejects stale events before recording; binding checks again after
  awaited persistence before marking received output or posting a UI envelope.
  App checks the envelope at delivery before mutating projection or applying views.
- Received-message evidence belongs to binding so result fallback works even when
  queued Textual event delivery has not caught up. Final events remain deliverable
  after runner completion until a new generation/attachment invalidates them.
- Admitted Store appends finish against their captured old target before a retired
  lease is released. New attachment state is not updated by old append completion.
  Store's shielded append contract remains authoritative. Checkpoint operations also
  drain before cancellation releases an in-use lease.
- Deferred refresh callbacks check the captured attachment token. Patch, catalog,
  configuration and compaction continuations check the token before applying old
  results. Catalog refresh also checks selector identity; statusline results carry
  a same-turn revision so an older result cannot replace a newer one.
- Resource cleanup works against captured identities, closes in reverse order,
  retains failures for retry, and preserves resources attached during an earlier
  close. Unmount invalidates delivery, cancels tracked UI tasks/approval futures,
  drains admitted writes and closes retained resources on the owning event loop.
- Approval presentation retains eager pending-tool creation, mount-after ordering
  and the existing decision future; detach resolves pending UI futures as CANCEL.
  Delayed composer focus respects current approval/selector/command-menu precedence.

## Compatibility / CLI handoff

Canonical entry: `from looplane.terminal.app import looplaneApp`.
The constructor keeps the old keyword interface and accepts optional
`dependencies: TerminalDependencies | None`. It exposes public read-only
`runtime_context_id`, `conversation_binding`, `final_transcript_text`, and the
existing `last_error`, `run(inline=...)`, export and resource-close surfaces.

`TerminalDependencies` supplies copy, selected-text, token formatting, clock,
version, config persistence and metrics construction callbacks/types. Canonical
App defaults point directly to canonical dependencies. The legacy App subclass
injects late-bound facade callbacks, preserving patches made after construction.
The legacy metrics type remains usable in `query_one(..., tui.RuntimeMetrics)`.

Old `TextualApprovalPolicy(app, grants)` and `TextualEventSink(app, generation)`
constructors remain narrow facade adapters. Canonical App directly uses the
callback-based approval policy and binding-based sink. Canonical imports use
`external_agents`, never `backends`, `tui`, `cli`, or `loop`.

CLI worker can pass/use the canonical constructor and the public context ID.
Its existing root factory callback should remain the compatibility route for
callers/tests replacing `tui.looplaneApp`; importing the canonical constructor
inside feature code must not rediscover the old facade. The six selected existing
CLI route tests still pass with the current worker integration. No CLI edits were
made by this slice.

## Historical validation evidence, before the latest production changes

The following commands ran under the earlier explicit testing authorization. They
do not validate the latest source snapshot. Following main's newer instruction,
no tests, lint/build/validation, git, or post-edit review were run.

1. Existing TUI and terminal tests after initial extraction:
   `uv run pytest tests/test_tui.py tests/terminal -q -o addopts=''`
   -> 201 passed in 50.25s. Log: `.research/slice15-focused.log`.
2. Final combined scoped gate after production fence changes:
   `uv run pytest tests/test_tui.py tests/terminal tests/test_tui_pty.py
   tests/test_lazy_imports.py tests/test_modularization_boundaries.py
   tests/test_startup_cache.py tests/test_startup_trace.py -q -o addopts=''`
   -> 249 passed in 61.40s. Log: `.research/slice15-final-focused.log`.
   This collected the first 13 new service cases.
3. Final service suite including the two subsequently added checkpoint-cancellation
   and pending-approval tests:
   `uv run pytest tests/terminal/test_projection_binding.py -q -o addopts=''`
   -> 15 passed in 1.62s. Log: `.research/slice15-services.log`.
4. Legacy CLI TUI route/factory compatibility:
   `uv run pytest tests/test_cli.py -k 'tui or alt_screen or real_tty' -q -o addopts=''`
   -> 6 passed, 73 deselected in 1.61s.
   Log: `.research/slice15-cli-compatibility.log`.
5. `uv run ruff check src/looplane/tui.py src/looplane/terminal tests/terminal
   tests/test_tui.py tests/test_tui_pty.py`
   -> All checks passed. Log: `.research/slice15-ruff.log`.

The new service tests include subprocess import isolation, exact stream flush/export
behavior, immutable tool/verification alias commands, received-output result fallback,
a real Store append suspended across attachment rotation, stale queued events,
resource-close failure/retry with a newly attached resource, stale deferred callback,
out-of-order same-turn statusline results, late-bound facade patches, checkpoint
cancellation draining, and pending approval cancellation on detach.

## Latest production-only continuation

- `_run_agent` now checks its captured attachment token after asynchronous native
  context preparation and immediately before constructing `TuiRunRequest`/runner.
  An old context-preparation continuation cannot start a runner for a replacement
  attachment.
- Initial turn statusline refresh now occurs after incrementing the run generation
  and capturing the new token. The new turn's own refresh is no longer admitted
  under the previous generation and then immediately invalidated.
- These changes were applied without running or modifying tests, lint/build checks,
  git operations, or a post-edit review.

## Remaining work / limits

The latest production changes are unvalidated. Main owns focused regression checks
on the current snapshot, repository-wide Ruff/pytest gates, startup regression
script, package build/archive validation, canonical plan update and scoped commit.
Source implementation and historical passing gates do not establish Slice 1.5
completion. No new full-suite or package claim is made here. CLI constructor routing
remains its worker's integration scope; the overarching goal remains active.

The analysis document recorded a pre-existing apparent configuration-cancel
fallthrough in `_run_configuration`; this slice did not bundle an unrelated
configuration behavior fix. Statusline subprocess termination policy is also
unchanged; task/UI-result fencing is not a claim that an external shell is killed
on timeout. These should be separately characterized if main elects to address them.

Pre-continuation size observations: facade 202 lines; canonical App 3,368; projection 935;
binding 297. The App retains explicit input/configuration/use-case coordination;
line count is not the acceptance criterion for the extracted owned services.

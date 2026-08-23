# M11 final release review

Date: 2026-08-22  
Verdict: **GO**

This is the final narrow re-review of the latest shared worktree. No production or test file was
changed, no credential store was read, and no real model/runtime request was made during this
review. Only this report was updated.

## Release conclusion

All previously reproducible release blockers are fixed and covered by local fake/runtime
regressions. The final interrupt-exception cleanup gap is also closed: the pending event read is
cancelled and awaited before the original exception is re-raised, then bounded outer cleanup closes
the session and the iterator finalizer runs.

No new release blocker was found in the final narrow scope. M11 is GO based on the reviewed unified
conversation, typed transcript/correlation, approval, workspace invariant, cancellation, and
resource-lifecycle boundaries.

## Final blocker closure

### Cancellation and iterator lifecycle

- Cooperative cancellation bounds the `interrupt()` call itself and includes terminal waiting in
  the same grace budget (`conversation_controller.py:185-220`).
- Outer exception cleanup retries interrupt with a timeout and always awaits controller/session
  close (`conversation_controller.py:135-150`).
- If interrupt raises a non-timeout exception, the pending `anext()` task is immediately cancelled
  and awaited before re-raise (`conversation_controller.py:190-205`).
- The event iterator is explicitly closed in the outer `finally`
  (`conversation_controller.py:257-261`).

Exact regression and independent fake reproduction both verified:

```text
original interrupt error re-raised
session closed = 1
iterator finalized = 1
pending tasks = 0
```

Blocking interrupt and interrupt-with-no-terminal regressions also return bounded cancellation and
close the native session.

### Controller and host lifecycle fencing

- `ConversationController` serializes start/close and concurrent close callers with one lifecycle
  lock, while turns remain sequential (`conversation_controller.py:65-109,135-150`).
- Claude and Codex isolated hosts hold their lifecycle lock across the full workspace/session start
  and full close (`claude_conversation.py:44,50-65,142-166`;
  `codex_conversation.py:45,51-69,136-160`).
- Constructor failure closes the just-created workspace. Parallel start creates only one
  workspace/session; the competing start fails without leaking. Close waits for in-progress
  cleanup rather than returning early.
- Persistent resources close during Textual `on_unmount()` on the same event loop; the CLI no
  longer creates a second loop for native cleanup (`tui.py:782-787`; `cli.py:623-629`).

### Codex typed correlation and current protocol drift

- Completed actions are removed from `_started_actions`; duplicate completion/delta fails closed
  (`codex_app_server.py:585-635`).
- `turn/completed` rejects any still-active action belonging to the native turn
  (`codex_app_server.py:646-669`).
- Warning notifications with a thread ID must match the PCA-owned native thread; foreign-thread
  warnings fail closed (`codex_app_server.py:564-581`).
- Multi-file `fileChange` preserves all paths in the neutral event and the isolated host audits the
  full claimed set against the real patch.
- Initialize advertises `experimentalApi`; known rate-limit, remote-control, MCP startup, account,
  and thread status notifications are handled as observational drift while unknown notifications
  remain fatal.
- Hooks, plugins, and remote plugins are disabled; hook state and MCP server configuration are
  explicitly emptied before app-server use.
- Warning is a bounded typed `NoticeEvent` rendered as secondary activity, not confused with
  assistant text or tool output.

### Claude correlation and workspace invariants

- Claude claims paths only for typed file-change actions, never for Read/Search tools, and exact
  cumulative claims must match the audited workspace patch.
- `ConversationWorkspace` continues to pass dirty-source HEAD-only behavior, full source invariant
  including ignored files, source/index/HEAD tamper detection, origin/hook isolation, alternate-index
  patch collection, changed-path bounds, and idempotent cleanup.

### Unified TUI behavior

- Claude/Codex requests reuse one provider-native, long-lived controller per
  runtime/repository/model/context identity.
- There is no visible Ask/Agent split; the composer presents one conversation flow.
- Assistant text is consolidated per turn, tool blocks are keyed and updated by action ID, typed
  notices remain secondary, and approval is placed at the bottom with only supported decisions.

## Verification evidence

All independently executed commands used fake/local fixtures only.

```text
.venv/bin/pytest -q tests/test_conversation_controller.py
7 passed

.venv/bin/pytest -q \
  tests/test_conversation_controller.py \
  tests/test_claude_conversation.py \
  tests/test_codex_conversation.py \
  tests/test_codex_app_server.py
31 passed

.venv/bin/pytest -q
330 tests, exit 0

.venv/bin/ruff check src tests scripts
All checks passed
```

The separately reported lock, diff, build, and installed-CLI refresh gates were also green. This
review did not repeat the live fixed-response Codex smoke, because the task explicitly prohibited
credential access and real runtime/model calls.

## Final verdict

**GO.** The release-blocking failure modes from all prior M11 review rounds are closed, the exact
regressions pass, the full 330-test suite is green, and no blocker remains in the final reviewed
scope.

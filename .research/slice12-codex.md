# Slice 1.2 Codex runtime protocol

Status: complete within assigned scope; ready for main integration.

## Changed paths

- `src/looplane/codex_app_server.py`
- `src/looplane/codex_conversation.py`
- `src/looplane/runtimes/codex/session.py`
- `src/looplane/runtimes/codex/conversation.py`
- `src/looplane/runtimes/codex/correlation.py`
- `src/looplane/runtimes/codex/event_mapper.py`
- `src/looplane/runtimes/codex/approval_mapper.py`
- `tests/runtimes/codex/test_protocol_owners.py`
- `.research/slice12-codex.md`
- `.research/slice12-focused.log`

## Responsibility and dependency boundaries

`runtimes/codex/session.py` is the canonical public runtime session and transport-owned shell. It retains subprocess launch, controlled environment and MCP configuration, initialization/thread startup, RPC routing/futures, bounded JSONL reading/writing, stderr retention, failure fan-out, interruption and process shutdown. It composes protocol owners directly. A second transport wrapper was unnecessary for this slice: the plan explicitly leaves process/RPC/read/close in the session shell.

`CodexCorrelation` owns native/local thread, turn and action identity maps, starting/active/terminal turn state and compaction binding/lifecycle state. Explicit ID and stderr callbacks replace access to session internals. Strict binding still rejects rebinding; replacement-native-turn adoption retains the original interrupt target.

`CodexEventMapper` owns item lifecycle, action approval descriptions, file previews/change IDs and aggregate turn diffs. It maps notifications/items, telemetry, skills, warnings, compaction and terminal events using a typed event emitter, bounded-text/ID/stderr callbacks and the explicit correlation owner. It never holds or accesses a session object. The session retains event sequence/queue ownership.

`CodexApprovalMapper` owns pending approvals and active wire IDs, validates request correlation, converts available decisions and permissions, builds canonical approval requests, and converts response decisions to existing wire shapes. It receives a typed action-context callback rather than accessing another component's private state. The session still performs response writes before removing pending state and emitting resolution, preserving the existing I/O ordering.

The Slice 1.1 bounded parser/tool leaf helpers remain the parsing and tool conversion owners. No vendor frame implementation imports a compatibility facade, CLI, TUI, controller, or disposable-workspace audit host.

`runtimes/codex/conversation.py` is the canonical `IsolatedCodexConversation` host. It retains disposable clone creation, changed-path normalization, action-path tracking, claimed/actual patch reconciliation, failed audit terminal conversion and workspace cleanup. Only session creation becomes an injected factory. Audit logic remains outside protocol/transport owners.

## Compatibility

The two original modules are explicit compatibility facades. Their public constructors retain named parameter signatures. The app-server facade supplies a dynamically resolved UUID callback so existing `looplane.codex_app_server.uuid4` patches still affect generated IDs. The conversation facade supplies a dynamically resolved session factory so existing `looplane.codex_conversation.CodexAppServerSession` patches still work. Workspace class re-export keeps its existing create patch target valid.

Legacy session correlation properties and event/helper methods explicitly delegate to owners. They do not maintain duplicate state, use mixins, or make owners reach back into private session fields. The bounded callback resolves `session._bounded` dynamically, preserving the tested helper monkeypatch. `_PendingApproval`, decision/status/error imports and the shared JSON module compatibility names remain available. Existing tests were not rewritten to weaken these contracts.

## Validation evidence

Initial existing focused suite: 109 tests passed.

Final command:

```sh
uv run pytest -o addopts='' -q tests/runtimes/codex tests/test_codex_app_server.py tests/test_codex_conversation.py
```

Result: **120 passed in 2.53s**, exit 0. Output is `.research/slice12-focused.log`.

This includes 11 new owner-level cases covering recorded JSON frame replay through command/start/approval/output/completion/text/terminal, sequence and action identity, replacement native turns and interrupt reverse mapping, strict rebinding, compaction binding futures and terminal deduplication, unknown/foreign/out-of-order fail-closed frames, duplicate approval IDs, explicit canonical ID injection and isolated canonical imports. Existing subprocess, shutdown, permission conversion, preview, compatibility and workspace-audit tests also pass.

Final lint command:

```sh
uv run ruff check src/looplane/runtimes/codex src/looplane/codex_app_server.py src/looplane/codex_conversation.py tests/runtimes/codex tests/test_codex_app_server.py tests/test_codex_conversation.py
```

Result: **All checks passed**, exit 0. Changed Python implementations and the new test were formatted with Ruff.

## Remaining integration work

No known focused-test or lint regressions remain. Main owns the full-suite, architecture/package gates, main plan updates and any scoped staging/commits. No stage/commit commands were run. No CLI/TUI/console/SDK, runner/backend, sandbox, provider, or main-plan files were edited. No web research or live Codex/provider execution was performed; the evidence here is deterministic local testing, not a production runtime claim.

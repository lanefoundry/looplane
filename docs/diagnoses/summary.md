## Goal
- Dependency-ordered milestone delivery: M10/M11 closed → M12 measured startup performance (DONE) → M13 external coding CLI runtimes (OpenCode, Pi, OMP) + runtime picker via registry/capability matrix.

## Constraints & Preferences
- Dependency order held: M10/M11 → M12 → M13.
- M13 boundary: looplane Agent = native harness; external CLIs (Claude Code, Codex, OpenCode, Pi, OMP) = sibling runtimes, never a `ModelProvider` transport or wrapped in second model-driven loop.
- M12 acceptance: lightweight routes must NOT import heavy SDKs; every change has paired before/after evidence.
- External CLIs own login/credentials; looplane never reads/proxies another CLI's OAuth into native `ModelProvider`.
- hyperfine not installed → bench script built-in fallback timer.
- `.artifacts/`, `.agent-work/` gitignored.
- Registry must lazy-import backend/session classes (store as `"module.ClassName"` strings) to preserve M12 lazy startup.

## Progress
### Done
- M10/M11 committed (`a8c711e`, `bf50d9b`, `b0801b2`, `66a05a0`).
- **M12 fully complete (Slices 1–5)**, all committed: `4a1e063`, `6cab782`, `3ffe0f0`, `ece3552`, `4f4a4f6`, `61570a9`.
- Baseline captured & committed `61570a9`: `benchmarks/startup-baseline.json` (help 0.492s, config 0.3804s); CI gate armed.
- Full suite 352 pass; ruff pass; lazy-load intact.
- **M13 Slice 1a DONE**: `src/looplane/runtime_registry.py` — `RuntimeKind`, `RuntimeCapability`, `RuntimeAdapter`, `RUNTIME_REGISTRY` (claude-code, codex-cli, looplane-agent), `runtime_options()`, `runtime_model_options()`, `external_runtimes()`; backend classes stored as lazy import paths.
- **M13 Slice 1b DONE (behavior-preserving generalization)**:
  - Registry extended: `RuntimeAdapter.native_session` (import path to in-process `Isolated*Conversation` for native-driven runtimes), generic `_resolve_class()` (renamed from `_resolve_backend`, returns plain `type`), `runtime_model_map()`.
  - `cli.py` `make_runner` + `_acquire_native_controller` now data-driven from `RUNTIME_REGISTRY` adapter (native_session → native controller; `EXTERNAL`+backend → `ExternalCodingRunner`; else → `AgentRunner`). Removed dead per-runtime branch and `EXTERNAL_RUNTIME_MODELS` dict.
  - TUI picker wired to registry: `runtimes=runtime_registry.runtime_options()`, `runtime_models=runtime_registry.runtime_model_map()`; removed `_tui_runtime_options`.
  - `conversation.py` durable `runtime` literal widened to `str` with registry-backed `_validate_runtime_slug` on `ConversationManifest.runtime` and `ConversationEvent.runtime` (future runtimes need no further schema edit).
  - Tests: updated `test_cli.py` `_acquire_native_controller` call sites; added `tests/test_runtime_registry.py` (picker filtering, model-map coverage, dispatch classification, lazy resolve). Full suite + ruff green; `looplane --help` ≈0.34–0.43s (no M12 regression).

### In Progress
- (none)

### Blocked
- (none)

## Key Decisions
- M12 closed; next milestone M13 (Slice 1a/1b done; Slice 2 open).
- CI gate armed: fails >10% median regression vs baseline; skips when baseline missing.
- M13 Slice 1 = reuse/generalize existing `ConversationRuntimeSession` (NOT parallel `CodingCliAdapter` hierarchy).
- Dispatch model: `RuntimeAdapter.native_session` (in-process native controller) vs `backend` (subprocess `ExternalCodingRunner`) vs `NATIVE`/no-backend (looplane `AgentRunner`). Adding a runtime = one registry `RuntimeAdapter` + its impl, no `cli.py` branching.
- TUI mode-selection branching on `{"claude-code","codex-cli"}` (ask vs agent) left intact for Slice 1b (only these three runtimes are selectable today, so behavior preserved). Generalizing that to registry-driven `native_session` becomes part of adding the first external runtime without a native controller (Slice 2).
- Machine-protocol preference order (M13 plan): versioned SDK/RPC > ACP > JSON/JSONL stream > bounded stdin/stdout > PTY-only.

## Next Steps
1. M13 Slice 2: add OpenCode runtime — register `RuntimeAdapter` (executable `opencode`, backend import path, no native_session) + its `ExternalAgentBackend` impl (machine protocol: discover CLI, spawn, parse events) + TUI mode-selection generalization so external-without-native-session runtimes route to agent/external path. Repeat for Pi, then OMP.
2. Wire runtime picker into TUI + `chat` end-to-end for each new runtime; add per-runtime capability-driven UX (e.g., hide model switch if unsupported).
3. Re-run startup-perf bench + CI gate after each slice to protect M12.

## Critical Context
- `runtime_registry.py` is lightweight (stdlib only); imported eagerly at `cli.py` top without pulling heavy SDKs (verified: no openai/anthropic/textual/uvicorn loaded). Preserves M12 lazy startup.
- `cli.py` `make_runner` (now data-driven): `adapter = RUNTIME_REGISTRY.get(request.runtime)`; native_session set → `_acquire_native_controller`; `EXTERNAL`+backend → `ExternalCodingRunner(backend_cls(...), run_root, ...)`; else `AgentRunner`. `wall_time=300` if `EXTERNAL` else `900`.
- `conversation.py`: runtime fields are `str` (was `Literal["claude-code","codex-cli"]`); `_validate_runtime_slug` (before-mode validator) allows `None` and rejects any slug not in `RUNTIME_REGISTRY`.
- `_acquire_native_controller(cache, identity, *, adapter, repository, model)` builds session via `runtime_registry._resolve_class(adapter.native_session)`.
- `ConversationStore` is only used for runtimes in `{"claude-code","codex-cli"}` (tui.py:2771 guard); looplane-agent/others don't persist via it today.
- M12 commits: `4a1e063`, `66a05a0`, `6cab782`, `3ffe0f0`, `ece3552`, `4f4a4f6`, `61570a9`.

## Relevant Files
- `docs/plans/m13-external-coding-cli-adapters-plan.md`: M13 outcome, contract, slices.
- `src/looplane/runtime_registry.py`: M13 registry + capability matrix; `runtime_options()`, `runtime_model_map()`, `native_session`, `_resolve_class`.
- `src/looplane/conversation_runtime.py`: `ConversationRuntimeSession` port (Slice 1 base).
- `src/looplane/conversation.py`: runtime schema widened + validator (Slice 1b).
- `src/looplane/cli.py`: `make_runner` + `_acquire_native_controller` generalized (Slice 1b); removed `EXTERNAL_RUNTIME_MODELS`/`_tui_runtime_options`.
- `src/looplane/external_runner.py`: `ExternalCodingRunner` (backend path).
- `src/looplane/claude_backend.py`, `src/looplane/codex_backend.py`: existing backends (registry import paths).
- `src/looplane/claude_conversation.py`, `src/looplane/codex_conversation.py`: native `Isolated*Conversation` (registry `native_session` paths).
- `tests/test_runtime_registry.py`: NEW — registry contract tests (Slice 1b).
- `tests/test_cli.py`: updated `_acquire_native_controller` call sites (Slice 1b).
- `benchmarks/startup-baseline.json`: CI gate baseline (help 0.492s, config 0.3804s).
- `scripts/bench_startup.sh`, `scripts/check_startup_regression.sh`, `.github/workflows/startup-perf.yml`: M12 harness + CI gate.
- `src/looplane/startup_cache.py`, `src/looplane/startup_trace.py`: M12 instrumentation.
- `docs/startup-performance-playbook.md`: M12 principles.

# 2026-08-29 Parallel Next Execution

Source: follow-up after A2 visibility, B9 context pressure reminder, and B4 replay reducer seed.

## Active Tracks

- [x] B4 replay CLI: wire `session_replay.py` into a user-facing CLI path without breaking `sessions --show`.
- [x] A2 policy layering: add user/project policy source merge with deny-before-allow semantics.
- [x] B9 summarizing fallback seed: add a small pure/native fallback summarization slice, avoiding TUI.
- [x] Integration: review worker outputs, run focused tests, update report/status.

## Constraints

- Dirty worktree is expected; do not revert unrelated changes.
- Keep write scopes disjoint:
  - B4 owns `src/rivumi/cli.py`, `src/rivumi/session_replay.py`, and CLI/replay tests.
  - A2 owns policy/config modules and tests; avoid `loop.py` unless required.
  - B9 owns `runtime_semantics.py`, `prompts.py`, `loop.py`, and focused tests.

## Results

- B4 added `rivumi sessions --replay <run-id-or-prefix>` with deterministic reducer output,
  invalid-log rejection, and `--show` mutual exclusion.
- A2 added `AllowRule`, `PermissionRuleSet`, `merge_permission_rule_sources()`, config-backed
  `allow_rules`, and deny/critical-floor-before-allow tests.
- B9 added versioned deterministic history-summary fallback policy and native loop wiring that
  replaces older messages once under task-token pressure while retaining the seed and recent tail.
- `docs/agent-diff-report.md` now records completion items 29-31 and updates B4/B9 gaps.

## Verification

- `uv run pytest -q tests/test_runtime_semantics.py tests/test_prompts.py tests/test_loop_e2e.py`
  passed: 57 tests.
- `uv run pytest -q tests/test_permissions.py tests/test_cli_config.py
  tests/test_cli.py::test_exec_alias_wires_configured_permission_guard
  tests/test_cli.py::test_sessions_replay_renders_deterministic_state_and_timeline
  tests/test_cli.py::test_sessions_replay_rejects_invalid_events_jsonl
  tests/test_cli.py::test_sessions_show_and_replay_are_mutually_exclusive
  tests/test_session_replay.py` passed: 77 tests.
- `uv run ruff check` over affected Python files and focused tests passed.
- `uv run python -m py_compile` over affected runtime files passed.

## Residual Risks

- Worktree contains many pre-existing or parallel changes outside this pass; they were not reverted.
- B4 still lacks replay API/fork-from-event.
- A2 merge helper supports user/project sources, but current CLI only feeds the existing config
  source and CLI deny overrides.
- B9 fallback is deterministic and lossy; it is not a model-quality summary and does not reduce
  recorded usage totals.

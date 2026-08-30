# 2026-08-29 Next High ROI Goal

Goal: implement the next parallel high-ROI looplane improvements.

## Tracks

- [x] B4 replay API / fork-from-event baseline
  - Owner files: `src/looplane/session_replay.py`, `src/looplane/cli.py`, replay/session tests.
  - Target: expose deterministic replay JSON and add a safe fork-from-event baseline that does
    not replay side effects.
- [x] A2 project/org policy discovery baseline
  - Owner files: `src/looplane/permissions.py`, `src/looplane/cli_config.py`, optional new
    policy config module, focused config/permission/CLI tests.
  - Target: discover user/project policy sources with explicit precedence while preserving
    user deny and critical command floor authority.
- [x] B9 post-compact workspace/context reinjection
  - Owner files: `src/looplane/prompts.py`, `src/looplane/runtime_semantics.py`, `src/looplane/loop.py`,
    focused prompt/runtime/loop tests.
  - Target: inject bounded workspace/file/check context after native compaction or fallback,
    once per pressure cycle.
- [x] Integration
  - Review worker outputs, resolve overlaps, run focused tests, update `docs/agent-diff-report.md`,
    and record verification here.

## Constraints

- Keep write scopes disjoint unless integration requires a small connector edit.
- Do not weaken deny-first permission semantics.
- Fork-from-event must not pretend side-effect replay is safe.
- Reinjection must be bounded, deterministic, and tested for one-shot behavior.

## Results

- B4 added canonical replay JSON output and a side-effect-free fork seed artifact:
  `looplane sessions --replay-json <id>` and
  `looplane sessions --fork-from-event <id> --sequence <n>`.
- A2 added `src/looplane/policy_config.py`, repository-local `.looplane/policy.json` discovery,
  optional `LOOPLANE_ORG_POLICY`, and repository-aware CLI permission guard construction.
- B9 added `b9-post-compact-workspace-context-v1`, one-shot native-loop workspace/context
  reminder injection after deterministic history fallback, and resume marker detection.
- `docs/agent-diff-report.md` records completion items 32-34 and updates A2/B4/B9 gaps.

## Verification

- `uv run pytest -q tests/test_session_replay.py
  tests/test_cli.py::test_sessions_replay_json_prints_deterministic_json
  tests/test_cli.py::test_sessions_fork_from_event_prints_side_effect_free_seed
  tests/test_cli.py::test_sessions_fork_from_event_rejects_invalid_sequence` passed: 21 tests.
- `uv run pytest -q tests/test_permissions.py tests/test_cli_config.py
  tests/test_cli.py::test_cli_fails_closed_with_clear_invalid_project_policy_error
  tests/test_cli.py::test_exec_alias_wires_configured_permission_guard` passed: 63 tests.
- `uv run pytest -q tests/test_prompts.py tests/test_runtime_semantics.py tests/test_loop_e2e.py`
  passed: 61 tests.

## Residual Risks

- Fork-from-event currently emits a reviewable seed only; it does not create a new workspace or run.
- Project/org policy discovery is local-file/env based; there is no managed remote policy channel
  or live reload.
- Workspace/context reinjection is tied to deterministic native-loop fallback, not provider-native
  compaction events yet.

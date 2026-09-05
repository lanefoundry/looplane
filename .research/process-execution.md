# Process-execution extraction handoff

Date: 2026-09-05
Workspace: `/Users/xiaoxu/Projects/looplane`
Scope owner: Python process execution, OS sandbox implementation, local Git
workspace preparation, related tests, and Conditional Wave 3 ADR.

## Result

Actual implementation bodies were extracted from `runtime.py`; it is now a
compatibility facade. The required Python ownership change is implemented.
Conditional Wave 3 decision: keep Python; no measured justification for Rust,
`ProcessRunner` Protocol, native dependency or a new wire protocol. Full process
contract stabilization still has the explicit gaps below.

Plan consulted: `docs/plans/repository-modularization-plan.md`, especially
Dependency rule and Conditional Wave 3. Canonical execution/sandbox/local-Git
modules do not import the runtime facade or agent/vendor/TUI/command layers.

## Production paths

- `src/looplane/execution/__init__.py`
- `src/looplane/execution/types.py`: `CommandResult`.
- `src/looplane/execution/capture.py`: bounded capture/text, pipe/line readers,
  stdin writer. Original algorithms and decoding behavior retained.
- `src/looplane/execution/environment.py`: safe environment/task paths.
- `src/looplane/execution/local_process.py`: `run_local_process`, spawn, wait,
  deadlines, cancellation, process-group termination, result construction.
- `src/looplane/sandbox/__init__.py`
- `src/looplane/sandbox/policy.py`: request model and root normalization.
- `src/looplane/sandbox/launcher.py`: OS/backend selection and fail-closed errors.
- `src/looplane/sandbox/macos.py`: Seatbelt policy rendering.
- `src/looplane/sandbox/linux.py`: bubblewrap/Landlock argv construction.
- `src/looplane/sandbox/landlock_run.py`: standalone enforcement implementation.
- `src/looplane/workspace/__init__.py`: created only if absent during extraction.
- `src/looplane/workspace/local_git.py`: exact-commit disposable Git preparation.
- `src/looplane/runtime.py`: explicit compatibility forwarding and aliases.
- `src/looplane/landlock_run.py`: compatibility exports/entry point; this additional
  old path was updated because its implementation moved under `sandbox/`.

The legacy `run_bounded_command` signature is retained. The compatibility surface
includes public data/error types, private capture/sandbox/process helpers, shared
OS/subprocess/time module objects, `runtime.landlock_available` interception,
`runtime.sanitized_subprocess_env`, process-stop/signal forwarding and
`runtime.run_bounded_command` interception for the legacy Git workspace class.
Canonical modules use their own dependencies without consulting the facade.

The mechanical extraction script is preserved as the one-time audit artifact
`.research/extract-process-execution.txt`; it is not a reusable migration command.
It selected AST-delimited declarations, preserved function bodies, and added
explicit dependency/compatibility forwarding. Formatting was scoped to owned code.

## Tests and commands

Existing runtime tests were moved by responsibility, retaining facade monkeypatch
tests in `tests/test_runtime.py`:

- `tests/execution/test_process_lines.py`
- `tests/sandbox/test_sandbox_policy.py`
- `tests/workspace/test_git_preparation.py`

New coverage:

- `tests/execution/test_local_process.py`: canonical and legacy execution,
  1.2 MB stdin and dual output draining, blocked/early-closed stdin, EOF, line
  bounds, split/malformed UTF-8, callback errors/finite slow consumers, deadlines,
  cancellation ordering, POSIX descendant cleanup, environment isolation, literal
  argv/cwd, compatibility hooks and architecture constraints.
- `tests/sandbox/test_launcher.py`: OS/backend matrix, unavailable refusal before
  spawn, profile escaping, Linux backend selection/root handling, setup failure
  refusal, plus the real macOS enforcement test with its recorded expected failure.
- `tests/workspace/test_local_git.py`: nested/existing workspace refusal and the
  legacy Git runner monkeypatch path, using existing repository fixtures.

Final focused validation:

```sh
uv run pytest -o addopts='' -q -rxXs \
  tests/test_runtime.py tests/execution tests/sandbox tests/workspace \
  tests/test_modularization_boundaries.py tests/test_tools.py \
  tests/contracts/test_external_agent_compatibility.py
```

Result: **154 passed, 2 skipped, 1 xfailed in 23.92s**.

```sh
uv run ruff check \
  src/looplane/runtime.py src/looplane/landlock_run.py \
  src/looplane/execution src/looplane/sandbox src/looplane/workspace/local_git.py \
  tests/test_runtime.py tests/execution tests/sandbox tests/workspace \
  .research/process_execution_baseline.py
```

Result: **All checks passed**.

The first test collection exposed identical migrated basenames; these were renamed
to the distinct paths above. A subsequent run had 154 passed, 2 skipped and a real
macOS sandbox failure, which was investigated and recorded rather than changing
policy. The final strict expected-failure marker is limited to the measured OS and
architecture. No existing passing sandbox test was removed to hide that finding.

## Measurements and decision artifact

- ADR: `docs/plans/process-execution-decision.md`.
- Driver: `.research/process_execution_baseline.py`.
- Raw original runtime samples: `.research/process-execution-before.json`.
- Raw extracted canonical samples: `.research/process-execution-after.json`.

Host: macOS 26.5.2 arm64, CPython 3.11.14. Two warmups and 15 measured repetitions
per case; five-second deadline and 4,096-byte output retention per stream. No
sandbox/provider/network in the benchmark.

| Case | Original median ms | Canonical median ms |
|---|---:|---:|
| Python no-op | 21.769 | 20.260 |
| 1 MiB on each output pipe | 20.133 | 20.497 |
| 1 MiB stdin echo | 21.305 | 21.020 |

Large streams were fully counted and drained, retaining 4,096 bytes each. Results
include interpreter startup, scheduler noise, threads and cleanup. They neither
prove a speedup nor isolate a Python bottleneck. No Rust comparison was performed.

To measure a future canonical run without replacing the recorded evidence:

```sh
uv run python .research/process_execution_baseline.py \
  --module looplane.execution.local_process \
  --output /tmp/looplane-process-execution-new.json
```

The before artifact was captured before extraction. Re-running the driver against
`looplane.runtime` now measures the compatibility wrapper, not the old monolith.

## Remaining contract gaps and gate ownership

1. Real macOS restriction is not demonstrated: the unchanged verification profile
   returns `-6` (SIGABRT), no stdout/stderr, for both Python `print('ready')` and
   `/bin/sh`. The control command
   `/usr/bin/sandbox-exec -p '(version 1) (allow default)' /bin/sh -c 'printf ready'`
   returns 0 and `ready`. The restrictive-profile failure is represented by a
   strict xfail on macOS 26.5.2 arm64. It needs a separately scoped platform/policy
   diagnosis; this extraction intentionally retains the existing policy.
2. Two Landlock tests skip on this macOS host. Mocked Linux setup/filter tests
   passed, but Linux kernel enforcement and bubblewrap operation remain unproven
   here. Existing live Linux fixtures were retained.
3. Capture raw storage is bounded; replacement decoding at UTF-8 truncation cuts
   can expand re-encoded text beyond the byte cap. Tests preserve and expose this
   distinction. No new hard stdin-size limit or streaming stdin API was invented.
4. Callbacks execute in the drain thread. Exceptions and finite backpressure are
   covered, indefinitely blocked consumers are not. Total process memory and
   callback-owned accumulation are outside the retained-byte bound.
5. POSIX inherited-group cleanup is tested for exit/timeout/cancel, including
   descendants ignoring SIGTERM. Escaped sessions and Windows descendant cleanup
   are not proven. Exhaustive races and PTY behavior remain outside this suite.
6. Main worker owns repository-wide pytest/lint, package/archive, startup gates,
   tracker updates and integration. No claim of a completed repository gate or
   full cross-platform process contract is made in this handoff.

No `conversation_workspace.py`, `codex_conversation.py`, CLI, Codex host, TUI or
tracker edits were made by this worker. No public web research or live network was
needed. No stage, commit, deployment or Rust addition was performed.

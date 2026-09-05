# Wave1 Slice1.1 runner naming compatibility

## Scope and implementation

- Canonical declarations remain in the existing implementation modules:
  `src/looplane/codex_backend.py` (CodexCliRunner), `claude_backend.py`
  (ClaudeCodeRunner), `opencode_backend.py` (OpenCodeRunner), `pi_backend.py`
  (PiRunner), `omp_backend.py` (OmpRunner), and `external_cli_base.py`
  (StructuredCliRunner).
- Every old class name is a direct alias to the canonical class, preserving
  identity, inheritance, old imports, and legacy module globals used by monkeypatches.
- Temporary flat entry modules: `codex_runner.py`, `claude_runner.py`,
  `opencode_runner.py`, `pi_runner.py`, `omp_runner.py`, `structured_cli_runner.py`.
  These only re-export the canonical classes from the existing implementations.
- `tests/contracts/test_external_agent_compatibility.py` covers canonical/legacy
  identity, old pickle class references, runtime protocol checks, inheritance,
  and command monkeypatch interception through both import surfaces.
- No execution, permissions, wire, CLI, or implementation-location changes.
  No changes to external_runner.py, ask_runner.py, runtimes/codex, terminal,
  tooling, or main documentation. No staging or commits.

## Validation

- `uv run pytest tests/test_codex_backend.py tests/test_claude_backend.py
  tests/test_external_cli_backends.py tests/contracts/test_external_agent_compatibility.py`:
  final run **54 passed in 11.51s**, including 22 new compatibility cases.
- Initial run: 53 passed, one timeout in the existing Codex streaming test's
  one-second message wait. The complete focused rerun passed without execution
  changes or altered timing assertions; the initial timeout's cause is unconfirmed.
- `uv run ruff check` scoped to the six implementation modules, six canonical
  entry modules, and compatibility test: **All checks passed**. The reported E501
  in the new test has been fixed.
- Full repository gate and baseline lint debt remain with main.

## Remaining migration

The flat runner entry modules and old class aliases are compatibility scaffolding.
Implementation moves and consumer import migration belong to later dedicated
slices, including an explicit decision on preserving legacy monkeypatch targets.
Class __name__/__qualname__ now expose canonical names; __module__ continues to
identify the legacy implementation module. The ExternalAgentRunner contract stays
in external_agents.py. Main owns the full gate, main docs, and commit.

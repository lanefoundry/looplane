# Wave 1 Slice 1.1: tooling extraction

Status: tooling extraction complete; focused tooling Gate passed.

## Ownership and behavior invariants

- Own only src/looplane/tools.py, src/looplane/tooling/{__init__,types,definitions}.py,
  tests/tooling/, and this report. No staging or commits.
- Preserve exception/dataclass object identity through old facade reexports.
- Preserve all built-in descriptions, schemas, flags, and JSON insertion/output order.
- Preserve fresh definitions per call and the existing sorted verification allowlist.
- Keep ToolExecutor mechanics, MCP discovery/refresh/dispatch in tools.py.
- Canonical tooling leaves must load without importing the facade or higher layers.

## Implementation

Use an AST-bounded extraction script to move the original class/function text without
rewriting declarative data. Capture the original factory output as a committed-test
fixture before changing the source. contracts.py remains the existing domain owner.
Local AGENTS.md/Codex-omc.md were not present at inspected locations; supplied session
instructions apply. Modularization plan and uncertainties were consulted. No web use.

## Completed paths

- src/looplane/tooling/__init__.py: lightweight package marker.
- src/looplane/tooling/types.py: original ToolExecutionError, ReviewablePatch,
  and private _PathSnapshot definitions, moved verbatim.
- src/looplane/tooling/definitions.py: original ten built-in definitions, moved
  verbatim into tool_definitions().
- src/looplane/tools.py: explicit object reexports; _tool_definitions remains a
  staticmethod referencing the canonical factory itself. Executor/MCP mechanics
  and original domain imports remain in place.
- tests/tooling/builtin_definitions.json: pre-extraction JSON snapshot, retaining
  description text, all fields, schema/key order, and definition order.
- tests/tooling/test_leaf_contracts.py: 16 compatibility/behavior cases.

## Focused Gate results

Independent pytest and Ruff commands ran concurrently:

- `uv run pytest -q tests/tooling tests/test_tools.py tests/test_mcp_client.py tests/test_lazy_imports.py`
  exited 0, all 85 cases passed (100% progress; project quiet settings suppress
  the textual summary). Covers tool policy/limits, transactions/rollback,
  verification/sandbox boundaries, MCP, and lazy imports.
- `uv run ruff check src/looplane/tools.py src/looplane/tooling tests/tooling`
  exited 0: `All checks passed!`

New tests assert canonical/facade object identity (including existing domain
reexports), staticmethod/factory identity, exact ordered JSON equality, fresh
mutable schemas, per-executor sorted allowlists, frozen values, pickle round trips,
legacy pickle GLOBAL resolution, canonical executor exceptions, and clean-process
leaf imports without the facade, runtime, MCP, agent, CLI, or TUI stack.

No loop.py/prompts.py source inspection or edits, unrelated repairs, staging,
commits, or web requests were performed.

## Scope limits

This report covers the tooling portion only. Repository-wide Gate and other parallel
owners' changes are not certified by focused feature results.

Moved classes now report looplane.tooling.types as __module__; old import and
pickle lookup paths still resolve to those exact objects. Consumers that compare
__module__ strings may observe the canonical relocation. No facade dependency was
introduced in the tooling package. No live MCP/provider verification was attempted.

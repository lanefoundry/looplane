# Looplane

A Python-first coding agent that produces verified patches in disposable workspaces.

## Module Boundary Constraints

These rules are enforced by `import-linter` in CI. Violating them will fail the build.

### `agent/runner.py` — loop coordinator, not implementer

- Describes the agent loop; does NOT implement tool handlers, context assembly, or persistence mechanisms.
- New tool handler → extract to a dedicated `agent/<feature>_dispatch.py` module (see `agent/subagent_dispatch.py`, `agent/skill_dispatch.py` as examples).
- Runner communicates with dispatchers through protocol callbacks defined in `agent/ports.py`.
- Runner does NOT directly import domain modules (`skills.py`, `instructions.py`, `plugins.py`). Use `agent/context.py` as the assembly point.

### `agent/context.py` — the assembly point

- Sole entry point for loading and composing skills, instructions, plugins, and memory into prompts.
- Runner and other `agent/` modules call `context.*` functions, not the underlying domain loaders.

### `tooling/definitions.py` — pure tool schemas

- Contains static `ToolDefinition` factories for executor-owned tools only.
- Dynamic tool definitions that depend on runtime state (skills, subagents) belong in their respective `agent/*_dispatch.py` modules.
- Accepts structured data (tuples, sequences), never pre-rendered prompt strings.

### `tooling/executor.py` — workspace tools only

- Owns filesystem, search, patch, verification, and MCP tool execution.
- Does NOT handle runner-level tools (`invoke_skill`, `dispatch_subagents`). Those are routed by `tool_scheduler.py` before reaching the executor.

### General rules

- Each slice is a behavior-preserving commit. Do not combine module moves with new features.
- Before adding a new import to `runner.py`, ask: "Does this belong in `context.py` or a dispatch module?"
- `ports.py` defines narrow Protocol types for cross-module callbacks. Prefer adding a Protocol over a direct import.

## Test Commands

```bash
# Unit tests (exclude sandbox tests which need platform-specific setup)
python -m pytest tests/ --ignore=tests/sandbox -x -q

# Lint
uv run ruff check src/ tests/

# Import boundary check
uv run import-linter
```

## Commit Convention

Use the `format-commit` skill flow when available, or conventional commits with scope:

```
feat(agent): add invoke_skill tool for on-demand skill loading
refactor(tooling): extract skill dispatch to dedicated module
fix(tui): correct terminal rendering in dark mode
```

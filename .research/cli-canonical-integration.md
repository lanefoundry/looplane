# CLI canonical default composition

Status: bounded production application delivered; unvalidated.

## Pre-change preservation

Before reading or editing, explicit cp commands preserved:

- `src/looplane/cli.py` to `.research/cli-canonical-frozen/cli.py`
- `src/looplane/commands/bootstrap.py` to `.research/cli-canonical-frozen/commands/bootstrap.py`
- `src/looplane/commands/ports.py` to `.research/cli-canonical-frozen/commands/ports.py`

The copy command completed successfully; no verification was performed.

## Initial source findings

The CLI root still imported its default native runner/error from `looplane.loop` and its default App from `looplane.tui`. Bootstrap already constructed and resumed native runners through `services.runtime.native_runtime()`. RuntimePorts already expressed explicit factory callbacks, so no command redesign, removed service, or new bootstrap default was needed.

The canonical terminal module was reported delivered by main. Canonical agent.runner was reported in progress with the same public runner API. Those implementation files were outside the read/edit scope; their availability and exports were not independently checked.

## Exact applied scope

- `src/looplane/cli.py`: native defaults now lazily import AgentRunner and UnsafeLocalExecutionError from `looplane.agent.runner`; terminal defaults lazily import looplaneApp from `looplane.terminal.app`.
- `src/looplane/cli.py`: a root-only compatibility selector checks already-loaded legacy modules for replacement symbols without importing those facades. Identity-equal symbols and symbols originating in the legacy/canonical implementation modules keep the canonical default. Replacements from other origins remain usable.
- `src/looplane/commands/ports.py`: updated the RuntimePorts docstring to describe explicit lazy composition rather than temporary adapters awaiting canonical owners.
- `src/looplane/commands/bootstrap.py`: read and preserved, but unchanged because it already uses the injected runtime factory.

CommandServices, RuntimePorts fields, explicit runtime injection, provider/model services, registry-based external/session selection, native resume construction, Typer declarations and the public lazy export table remain intact. No agent, terminal, tooling, tests or other production files were edited.

The compatibility selector is origin-based rather than a universal monkeypatch detector: a replacement deliberately reporting the same __module__ as an implementation is treated as an implementation default. Existing factory/legacy monkeypatch contracts need validation in the full shared snapshot. Explicit RuntimePorts and CLI factory-function injection remain unchanged.

## Snapshot evidence boundary

The earlier Slice 1.3 results belong to its fixed snapshot, not this application or the final shared tree. In particular, the reported worker 146 passing tests, main 92 passing tests, paired startup measurements and archive results cannot establish correctness, timing or packaging for this new composition. The previously reported full-suite retry status is also historical and is not updated by this task.

## Pending gates

The patch application completed. Per the active constraints, no tests were modified/run, no syntax/import/behavior validation, lint, build, Git or post-write review was performed.

Main still needs to validate:

- The final canonical runner exports both AgentRunner and UnsafeLocalExecutionError and the terminal module exports looplaneApp.
- Default CLI execution and resume use the canonical runtime/App, while explicit injected factories and established monkeypatches still work.
- Help/config/public lazy imports retain the intended startup dependency boundary.
- Focused CLI/commands/lazy tests, Ruff, shared import/SCC gates, full suite and packaging/startup gates on the actual final snapshot.

No further source changes are pending within this bounded task. No whole-goal or validated-completion claim is made, and no staging or commit was performed.

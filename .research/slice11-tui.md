# Wave 1 Slice 1.1: terminal leaf extraction

Status: terminal Slice 1.1 implementation and focused validation complete.

## Scope and behavior contract

Owned paths: `src/looplane/tui.py`, `src/looplane/terminal/{__init__,types,events,status}.py`,
`tests/terminal/`, and this report. No staging or commits.

- Extract request/selection aliases, frozen dataclasses, runner/resource Protocols, and enums.
- Extract the three Textual event envelopes without changing class names, payloads, or generation.
- Extract token formatting, usage arithmetic, and bounded usage bars verbatim.
- Preserve exact object reexports from `looplane.tui`; do not subclass or wrap them.
- Keep widgets/App/controllers, status rendering, monotonic clock, version lookup,
  input/focus/cancellation behavior, and generation fencing in their original owner.
- Keep facade function lookups in widgets so monkeypatching old formatting names still works.
- Canonical terminal modules never import the TUI facade/App; external events use
  `looplane.external_agents`, not the former `backends` facade.

## Implementation

Used bounded AST-guided bulk extraction to transfer exact original source segments,
including decorators, and preserve all unassigned code. The current package layout
was inspected before creation. No loop.py/prompts.py source was read or edited.
Repository plan and uncertainty documents were read; main owns global Gate/docs/naming.
The imported `Codex-omc.md` instruction file was not present at the repository root.

## Changed paths

- `src/looplane/tui.py`: direct explicit canonical reexports; original consumers stay here.
- `src/looplane/terminal/__init__.py`: inert package marker, no eager App imports.
- `src/looplane/terminal/types.py`: three option aliases, two enums, two Protocols,
  runner factory alias, and four frozen request/selection dataclasses.
- `src/looplane/terminal/events.py`: three original Textual message envelope classes.
- `src/looplane/terminal/status.py`: `format_token_count`, `_add_usage`, `_usage_bar`.
- `tests/terminal/test_leaf_compatibility.py`: 31 parametrized test cases.
- `.research/slice11-tui.md`: work progress and handoff evidence.

## Validation results

- `uv run ruff check src/looplane/tui.py src/looplane/terminal tests/terminal`
  passed, exit 0, `All checks passed!`.
- `uv run pytest -q tests/terminal tests/test_tui.py tests/test_lazy_imports.py`
  passed, exit 0, all collected tests reached 100%, no failures or skips shown.
  Project addopts plus explicit `-q` suppress the numeric summary.
- Focused coverage checks exact facade identity for all 18 moved names, frozen
  request/default fields, canonical and pre-extraction pickle resolution, enum values,
  token thresholds, usage-bar clamping/rounding, usage-total fallback and nonmutation,
  all three Textual message handlers/payloads/generations, facade formatter monkeypatch,
  and separate-process canonical imports without loading tui/cli/backends/textual.app.
- Existing TUI tests exercise the unchanged UI behavior; existing lazy-import tests
  also passed. This is local deterministic evidence, not a live-provider or PTY claim.
- Scoped Ruff autofix sorted imports and removed now-unused imports. Its initial
  remaining test line-length finding was corrected before the successful lint run.

## Risks and handoff

- Moved definitions naturally report their canonical `__module__`; old facade pickle
  globals resolve successfully, while newly written pickles use canonical paths.
- No provider/protocol behavior, widget ownership, controller state, clock/version
  dependency, cancellation flow, focus handling, or event reduction was changed.
- Global architecture/full-suite/startup benchmark/package-build Gate belongs to main;
  this report does not declare the repository-wide Gate complete.
- No git staging or commits, unrelated repairs, or top-level test edits were performed.

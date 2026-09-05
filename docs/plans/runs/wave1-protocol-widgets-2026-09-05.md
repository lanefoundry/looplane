# Wave 0 contracts / Slice 1.2 / Slice 1.4 integration gate

Date: 2026-09-05
Status: passed
Baseline revision: `deda523`

## Validated scope

This gate used a fixed local checkout of the baseline plus only the completed
Wave 0 contract, Codex protocol and terminal-widget changes. It excluded the
concurrent CLI composition, MCP bridge and process-execution work. The snapshot
prevents an ongoing worker from changing subprocess/import inputs during a gate.

- `e9e3d46`: one native EventSink owner with console/SDK aliases; TurnLimiter
  compatibility; registry runner names; actual session capability mapping;
  dependency direction, canonical event ownership and no-new-cycle tests.
- `a90c3c3`: Codex correlation, event and approval state owners; session transport
  shell; isolated workspace host; explicit compatibility constructors/callbacks.
- Slice 1.4: approval, composer, scroll, transcript, tool, selector, status,
  onboarding, clipboard and link modules; focused feature tests and old facades.

The capability check compares token usage directly and verifies that structured
approvals/previews imply their broader discovery categories. Other capability
fields have no equivalent and are not fabricated from discovery flags.

## Results

- Full pytest: **1326 passed, 2 skipped in 231.31 seconds**. The sandbox skips
  retain their platform-dependent meaning. No test was removed to satisfy a gate.
- Repository Ruff: **All checks passed**.
- Scoped Wave 0 controller/SDK/console/registry/lazy tests: 43 passed.
- Scoped Codex protocol/conversation tests: 120 passed, including 11 new composed
  owner tests and recorded frame replay/fail-closed ordering cases.
- Terminal characterization, existing TUI/PTY tests, and added widget contracts
  passed, including focus, Enter/arrows/numbers/Escape, copy, resize and Unmount.
- Source distribution: 586155 bytes, 217 entries.
- Wheel: 381027 bytes, 110 entries.
- Both archives include all 106 production modules and pass bounded archive
  checks with no local research/cache/dependency directories.

Startup used alternating baseline/candidate order, 3 warmups and 15 samples with
one interpreter/dependency set. All scenarios passed the 10% regression threshold:

| Scenario | Before median | After median | Change |
| --- | --- | --- | --- |
| CLI help | 0.2085 s | 0.2144 s | +2.8% |
| CLI config | 0.1676 s | 0.1624 s | -3.1% |
| TUI import | 0.3335 s | 0.3409 s | +2.2% |

## Portable evidence

- [Full pytest log](wave1-protocol-widgets-pytest.txt)
- [Ruff log](wave1-protocol-widgets-ruff.txt)
- [Build and archive listing check](wave1-protocol-widgets-build.txt)
- [Startup regression output](wave1-protocol-widgets-startup.txt)
- [Baseline samples](wave1-protocol-widgets-startup-before.json)
- [Candidate samples](wave1-protocol-widgets-startup-after.json)

Reproduction commands: `ruff check .`; `python -m pytest -q -o addopts=''`;
`uv build --wheel --sdist --out-dir <artifact-dir>`;
`python scripts/check_distribution.py <artifact-dir>`; and
`scripts/check_startup_regression.sh <before.json> <after.json> 10`.
Tests use the repository virtual environment with the fixed checkout's `src`
on PYTHONPATH. Snapshot details and worker reports remain in `.research/`.

## Remaining scope

This is not the whole Wave 1 or plan completion. CLI composition, App projection
and binding, native tool/agent decomposition, and the conditional process decision
remain tracked separately. The existing literal dynamic loop/subagents cycle is
still permitted only until Slice 2.5. No live vendor or unsupported-platform
execution is claimed by this deterministic local gate.

# Slice 1.3 CLI composition takeover

Status: implementation complete; full integration gate retry running.

The worker reached a model-capacity error after saving its implementation and
focused evidence. Main took over validation; no worker changes were discarded.

## Owners

- `cli.py`: Typer registration, named arguments, lazy compatibility wrappers and
  narrow composition callbacks. Existing `looplane.cli:app` remains the entry.
- `commands/bootstrap.py`: provider/native/external factories and controller cache.
- `commands/chat.py`, `external.py`, `sessions.py`, `auth.py`, `plugins.py`,
  `policy.py`, `serve.py`, `settings.py`, `onboarding.py`: scoped CLI use cases.
- `commands/ports.py`: per-invocation explicit construction/terminal/IO callbacks.
  No feature imports the CLI/TUI/runner compatibility facades. Concrete App/runner
  factories stay injected by the CLI root until their canonical owners are ready.
- `commands/session_index.py`, `terminal_io.py`, `paths.py`, `common.py`: independent
  discovery, IO and validation helpers.

## Evidence

- Worker focused log: `.research/slice13/pytest-final.log`, 146 passed.
- Main takeover commands/CLI/lazy suite: 92 passed in 10.56 seconds.
- Scoped and fixed-snapshot repository Ruff: passed.
- Fixed-snapshot full suite first run: 1 failed, 1335 passed, 2 skipped. Failure
  was the existing Claude streaming test's one-second first-message wait. The
  same test passed independently in both baseline and candidate checkouts. A
  complete rerun is required before closing this slice; assertions/timeouts are
  unchanged and the failure cause is not proven.
- Worker startup samples exceeded the threshold (+36.8% help, +44.8% config).
  Main's alternating paired snapshot measurement passed: help +2.0%, config
  +3.8%, TUI import -10.0%. Different load/timing samples do not isolate code
  effects; no absolute speedup is claimed. Raw samples for both runs are retained.
- Snapshot archives passed: sdist 595538 bytes / 234 entries; wheel 395701 bytes /
  126 entries. Both include all 122 production modules.

Snapshot base: `b5205fb`. Integration artifacts: `.research/slice13-integration/`.
The snapshot contains only CLI composition changes, excluding active process,
MCP, terminal binding and native state work.

One existing CLI test now patches registry resolution rather than the vendor's
old class attribute, matching registry-based factory selection while preserving
the test's external-modification approval and result assertions.

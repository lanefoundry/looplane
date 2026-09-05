# Wave 1 Slice 1.1 execution record

Status: complete; integrated Slice 1.1 gate passed
Base revision: `7bff7a52c3e50f91b9215e68bfe6753f3a98ba1c`

## Scope and ownership

| Owner | Scope | State |
| --- | --- | --- |
| Terminal worker | TUI request/event types and pure status formatting | focused gate passed |
| Codex worker | Safe IDs, bounded parsing, status/summary, decision mapping | focused gate passed |
| Tooling worker | Tool value types and declarative definitions | focused gate passed |
| Runner worker | Canonical runner names with existing import aliases | focused gate passed |
| Integration | Dependency constraints, full gate, build/startup, scoped commits | gate passed |

External-agent contracts were extracted in `91304e2`. Runner naming is a separate
compatibility commit from the leaf implementation extractions, as required by the plan.

- `7c68bd0`: prerequisite lint/test repairs and bounded release archives.
- `d1687fa`: canonical runner names and legacy import compatibility.
- The leaf extraction commit contains this final gate record and its raw evidence.

## Behavior invariants

- Existing public imports resolve to the same canonical types/functions.
- TUI routing, focus, input precedence, cancellation, and generation fences retain
  their existing implementation; only types and pure formatting move.
- Codex frame limits, ID validation, status mapping, approval decisions, and method
  signatures retain their behavior. Transport and session state remain in place.
- Tool definitions retain schemas, order, metadata, and effect classification;
  dispatch, permissions, path checks, rollback, and verification stay unchanged.
- Runner construction, CLI arguments, credential handling, and delegation retain
  their behavior; legacy names remain aliases.
- Extracted leaf packages cannot import compatibility facades or introduce cycles.

## Gate

- [x] Focused feature tests and alias identity coverage
- [x] Import boundaries and cycle checks
- [x] `uv run ruff check .`
- [x] `uv run pytest -q` (used `-o addopts=''` to retain the result summary)
- [x] Existing lazy-import tests and paired startup regression check (10% threshold)
- [x] Wheel and sdist build; clean archive paths and canonical modules included
- [x] Scoped prerequisite, naming, and extraction commits

Raw integration logs: `.research/slice11-gate/`.
Worker reports: `.research/slice11-{tui,codex,tools,runners}.md`.
Live providers are outside this pure-refactor gate. No dedicated type checker is
configured in `pyproject.toml`; do not describe Ruff as a static type check.

## Integration results

- Full suite: `uv run pytest -q -o addopts=''` passed, 1,273 passed and 2 skipped
  in 161.99 seconds. Skips are platform-dependent runtime sandbox tests.
- Portable raw results: [pytest](wave1-slice1.1-pytest.txt),
  [Ruff](wave1-slice1.1-ruff.txt), [build/archive](wave1-slice1.1-build.txt),
  [startup comparison](wave1-slice1.1-startup.txt),
  [before samples](wave1-slice1.1-startup-before.json),
  [after samples](wave1-slice1.1-startup-after.json), and
  [import graph comparison](wave1-slice1.1-import-graph.json).
- Focused tooling/MCP/lazy-import tests: 85 passed.
- Focused runner tests: 54 passed, including 22 compatibility cases. The existing
  Codex one-second streaming wait timed out on the first concurrent run and passed
  on the focused rerun; no timeout or behavior assertions were relaxed.
- Focused terminal tests and 31 new compatibility cases passed.
- Focused Codex helpers/app-server/conversation tests passed.
- Prerequisite runtime/cache tests: 31 passed, 2 platform-dependent skips.
- Repository Ruff: passed after correcting existing import ordering, test line
  lengths, a loop-variable capture, and a missing bubblewrap test assertion.
- Package build and `uv run python scripts/check_distribution.py
  /tmp/looplane-slice11-build-clean-20260905`: passed. Source archive: 576,098 bytes,
  199 entries; wheel: 368,685 bytes, 96 entries. Both contain all 92 production
  modules. The initial source archive was 97,746,414 bytes and included local
  research/cache material; the sdist allowlist now excludes it.

Paired startup measurements used the same interpreter/dependencies, alternated
baseline/candidate order, and recorded 15 samples after 3 warmups per scenario.
The baseline is a local archive of the base revision, not the older Wave 0 timings.

| Scenario | Before median | After median | Change |
| --- | --- | --- | --- |
| CLI help | 0.1506 s | 0.1536 s | +2.0% |
| CLI config | 0.1152 s | 0.1167 s | +1.4% |
| TUI import | 0.2157 s | 0.2156 s | -0.1% |

`scripts/check_startup_regression.sh` passed with a 10% threshold for all three
scenarios. These are local startup proxies, not interactive time-to-composer or
live provider performance measurements.

## Remaining architecture work

The graph grew from 74 to 92 modules with no new strongly connected components.
Unlike the older static-only Wave 0 record, this check also recognizes literal
`importlib.import_module(...)` calls. It finds the existing `looplane.loop` /
`looplane.subagents` cycle in both baseline and candidate. Delayed loading does
not remove that dependency; removing it remains Slice 2.5 work. Extracted feature
packages do not participate in cycles and cannot import compatibility facades.

Canonical runner declarations remain in their existing implementation files with
direct legacy aliases; flat `*_runner.py` modules are temporary entry points.
Moving those implementations into runtime packages remains a later extraction.
Moved leaf types report canonical `__module__` values; legacy imports and pickle
lookups remain supported. Session formats and execution policy are unchanged.

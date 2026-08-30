# M12: Measured startup performance

## Outcome

Make looplane feel immediately interactive while preserving every runtime, security, and verification
boundary. Optimize the lifecycle rather than changing language or hiding work. Every optimization
must have paired before/after evidence for user-visible time to first editable composer.

Source playbook: `docs/startup-performance-playbook.md`.

## Metrics and scenarios

Primary metric:

- median elapsed time from process entry to the first editable TUI composer.

Guardrail scenarios:

- `looplane --help`;
- `looplane config` with an existing config;
- bare `looplane` with real `~/.looplane`/XDG state and an ordinary repository;
- bare `looplane` with Claude Code and Codex installed;
- one representative `looplane exec` preparation path;
- import graph for `import looplane.cli`.

Record median, p90, min/max, sample count, command, revision, Python version, machine/OS metadata,
and whether the filesystem cache was warm. Do not compare absolute results across unlike machines.

## Measurement harness

- Add `scripts/bench_startup.sh` using `hyperfine --warmup 3 --min-runs 10 --export-json`.
- Support paired baseline/candidate commands so runs alternate rather than measuring one revision in
  a systematically warmer state.
- Capture `python -X importtime` separately; import time diagnoses causes but is not the product KPI.
- Store generated benchmark output below `.artifacts/startup/`, not in config or conversation state.
- Fail with an actionable installation message when hyperfine is unavailable.
- Add a small parser/checker that compares equivalent benchmark JSON and reports percentage change.

## Startup telemetry

- Accept `LOOPLANE_STARTUP_LOG` only as an explicit output path; telemetry is disabled otherwise.
- Record monotonic timestamps for process entry, CLI routing, config loaded, runtime discovery,
  application mounted, and composer editable.
- Write a bounded machine-readable record without prompts, repository paths, model credentials,
  environment values, vendor session IDs, or user content.
- Create private files atomically, reject unsafe symlink targets, and never let telemetry failure
  prevent normal startup.
- Test event ordering, bounds, privacy, disabled behavior, and write failure behavior.

## Slice 1: Freeze the baseline

- Run the complete scenario matrix on the current implementation.
- Retain raw hyperfine JSON and import-time reports.
- Identify the critical path and top cumulative imports before editing code.
- Treat the playbook's historical ~0.70 second import measurement as background, not the current
  baseline; the 2026-08-22 spot check of ~0.45–0.52 seconds is also not a paired benchmark.

## Slice 2: Lazy-load path-specific dependencies

- Move Codex OAuth/OpenAI SDK, Claude/Codex backends, conversation implementations, gateway,
  uvicorn, and other route-specific modules behind narrow loader functions.
- Use `TYPE_CHECKING` guards and local imports without weakening runtime validation.
- Ensure `looplane --help`, shell completion, and unrelated config commands do not load provider SDKs,
  Textual, external runtime implementations, or gateway server dependencies.
- Cache an imported factory only where repeated lookup is measured and safe; do not cache live
  clients, credentials, or sessions globally.
- Add subprocess tests that inspect imported module sets for representative routes.

## Slice 3: Parallelize independent startup work

- Write the dependency graph before introducing concurrency.
- Measure config loading, executable discovery, model/readiness discovery, state loading, and TUI
  construction separately.
- Use structured concurrency only for independent work whose cancellation and error semantics are
  defined. Preserve deterministic ordering at the UI boundary.
- Keep MCP/tool-server connection, workspace creation, auth refresh, and model client creation off
  the critical path until the selected runtime or first turn actually needs them.
- Do not parallelize cheap work when task scheduling overhead is larger than the measured saving.

## Slice 4: Cache and single-flight repeated discovery

- Cache only discovery proven expensive across launches, such as versioned runtime capability
  probes. Plain executable presence checks may remain uncached if they are already cheap.
- Key cache entries by adapter/protocol version, executable identity, and the smallest relevant
  non-secret config hash.
- Use a versioned strict schema, atomic replacement, private permissions, bounded entry size/count,
  and explicit expiry/invalidation.
- Never store OAuth/API credentials, environment values, repository content, prompts, or vendor
  session identifiers.
- Never cache failed, cancelled, partial, or protocol-invalid discovery.
- Deduplicate concurrent in-process probes with single-flight and propagate cancellation safely.

## Slice 5: Regression gate

- First run the benchmark in reporting-only CI mode long enough to establish runner variance.
- Compare paired medians on equivalent runners and publish raw JSON as an artifact.
- Fail a greater-than-10% median regression only after confirming the noise floor supports that
  threshold; retain a documented override for intentional, reviewed lifecycle tradeoffs.
- Keep functional tests, Ruff, lock/build, Cloudflare tests/types, CLI help, and TUI pilot tests as
  mandatory guardrails. Performance improvement cannot justify a behavior or security regression.

## Acceptance criteria

- The benchmark and telemetry measure time to editable composer, not import time alone.
- Common lightweight CLI routes do not import OpenAI SDK, vendor runtimes, Textual, or uvicorn.
- Every lazy/parallel/cache change has paired before/after evidence and functional regression tests.
- Startup logs and caches contain no secrets or user/repository content and fail safely.
- The final report explains median/p90 changes per scenario and names any regression explicitly.
- M13 external-runtime work begins only after this startup contract is stable, so new adapters plug
  into lazy discovery instead of enlarging global import cost.

## Explicitly out of scope

- Rewriting looplane in another language.
- Fabricating an absolute cross-machine startup SLA.
- Eager authentication or MCP/tool-server initialization to make readiness badges look immediate.
- Caching credentials, live clients, workspace state, prompts, repository content, or failures.
- Reducing safety checks after a task starts; this milestone optimizes startup lifecycle only.

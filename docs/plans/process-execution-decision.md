# Conditional Wave 3: retain the Python process implementation

Date: 2026-09-05
Status: Python extraction implemented; no Rust sidecar justified. Contract
stabilization has explicit outstanding platform and bounded-I/O gaps below.

The Wave 3 plan requires extraction of actual Python process behavior before a
language decision. This change gives that behavior a canonical owner without
adding a `ProcessRunner` Protocol, event hierarchy, wire format, or Rust binary.
`looplane.execution.local_process.run_local_process()` remains a synchronous,
bounded local operation. There is one implementation, with explicit compatibility
hooks rather than a second process abstraction.

## Ownership and dependency direction

- `execution/types.py`: immutable `CommandResult`.
- `execution/capture.py`: retained head/tail bytes, UTF-8 rendering, complete-line
  delivery, full pipe draining, stdin writer.
- `execution/environment.py`: allowlisted environment and task-local paths.
- `execution/local_process.py`: spawn, deadlines, cancellation, group termination,
  pipe-reader lifecycle, result construction.
- `sandbox/policy.py`: immutable `CommandSandbox`, normalized roots and trusted
  Python runtime roots.
- `sandbox/launcher.py`: fail-closed platform/backend selection.
- `sandbox/macos.py`, `sandbox/linux.py`: Seatbelt profile and Linux launch argv.
- `sandbox/landlock_run.py`: standalone Landlock/seccomp enforcement entry point,
  using platform primitives only. Its location is resolved relative to the Linux
  launcher, so launch does not depend on importing the application in the child.
- `workspace/local_git.py`: disposable clone preparation and exact commit pinning,
  consuming canonical process/environment code.

`runtime.py` retains public names and important private aliases. Its bounded-run
wrapper forwards legacy sandbox-probe, environment and process-stop hooks; its
workspace subclass forwards the old runner interception point. `landlock_run.py`
retains the old entry point, private exports and ABI-probe patching. Neither facade
is imported by the canonical execution, sandbox or local-Git modules. There are no
agent, vendor, TUI or command dependencies in those modules, and no dynamic facade
backimports or private-state mixins. Conversation workspace ownership is unchanged.

## Measured local baseline

Host: macOS 26.5.2 arm64, CPython 3.11.14. Each workload had two warmups followed by
15 sequential samples. These runs use no sandbox or network, a five-second process
deadline and a 4,096-byte retention limit per output stream. Measurements include
Python child startup, OS scheduling, polling, threads, capture and shutdown.

| Workload | Before median ms | Extracted median ms | Output fully drained |
|---|---:|---:|---|
| Python `pass` | 21.769 | 20.260 | no output |
| 1 MiB stdout and 1 MiB stderr | 20.133 | 20.497 | 1,048,576 bytes per stream |
| 1 MiB stdin echoed to stdout | 21.305 | 21.020 | 1,048,576 stdout bytes |

The large-output cases retain 4,096 bytes per populated stream, with original byte
counts and truncation flags. Raw samples and extrema are in
`.research/process-execution-before.json` and
`.research/process-execution-after.json`; the reproducible driver is
`.research/process_execution_baseline.py`.

The samples establish a small local baseline, not a speedup or a bottleneck. The
largest extracted dual-pipe sample was 65.439 ms, versus a 20.497 ms median, so
small median differences should not be attributed to extraction. There is no Rust
comparison, CPU profile, concurrent-load study, RSS measurement, or independently
audited launcher in this evidence. Nothing here demonstrates a benefit requiring
another implementation language.

## Contract evidence and unresolved limits

The focused suite exercises canonical and legacy runners, complete and bounded
lines, split and malformed UTF-8, large stdin/dual output, blocked/closed stdin,
callback errors and bounded slow consumers, environment profiles, literal argv,
timeouts, cancellation priority, and POSIX descendants that ignore SIGTERM. A
child readiness handshake prevents a startup race in group-cleanup tests.
Existing disposable-Git fixtures prove source isolation and exact commit pinning.
Platform tests cover unavailable-backend refusal, explicit Linux backend choice,
macOS profile escaping/default deny, Linux write-root handling, seccomp architecture
filters, and refusal to exec after Landlock setup errors. Consumer and import-boundary
tests are included; the main worker owns repository-wide gates.

The following are not claimed complete:

- The existing restrictive macOS profile aborts Python and `/bin/sh` with SIGABRT
  (`returncode=-6`) on this macOS 26.5.2 arm64 host, before producing output. A direct
  `sandbox-exec` control with `(version 1) (allow default)` successfully prints
  `ready`. Thus command availability alone does not prove usable verification
  sandboxing. The new real write-denial contract has a strict expected-failure
  marker limited to this measured OS/architecture, so an unexpected pass requires
  reconciliation. This is a recorded policy/runtime gap, not an enforcement pass;
  the extraction does not loosen the policy to make it pass.
- Linux Landlock execution is skipped on this non-Linux host. Simulated launcher
  and setup failures plus seccomp filter construction do not prove live kernel
  isolation. Linux CI and real bubblewrap/Landlock smokes remain necessary.
- Capture retention is byte-bounded, but existing `errors="replace"` decoding at a
  truncated UTF-8 cut point can expand the re-encoded result beyond that bound.
  Complete lines reassemble split code points before decoding; truncated lines may
  contain replacement characters. Tests characterize this without silently changing
  behavior. `bounded_text()` separately uses boundary-safe truncation.
- Stdin accepts an already-materialized string and encodes it in its writer thread.
  It has no runner-level input-size rejection or streaming input contract. Large
  input and blocked-write cleanup are tested, not an invented hard stdin limit.
- Line callbacks run synchronously on the reader thread. Faulty callbacks do not
  stop draining, and finite slow consumers are tested, but indefinitely blocked
  callbacks, consumer-owned accumulation, and total heap usage are not bounded by
  the output-retention setting.
- POSIX cleanup covers descendants in the inherited process group. Deliberate
  session escape is not covered. The existing Windows fallback terminates the
  direct process and has no proven Job Object/process-tree guarantee here.
- Cancellation/deadline priority and live cancellation are covered. Exhaustive
  scheduling races, PTYs, interactive transport and multi-platform load behavior
  are not established by this synchronous command suite.

## Decision and future entry criteria

Retain Python and defer the Rust sidecar. Python extraction is complete for these
owners; complete cross-platform contract stabilization is not. The current issues
call for separate behavior/policy decisions and platform evidence, not an automatic
language rewrite.

Reopen the decision only when a concrete workload or security requirement supplies
evidence that cannot be addressed cleanly in Python:

1. Define the missing contract and acceptance threshold, then reproduce it on the
   affected platforms. Resolve UTF-8/input/callback bounds explicitly, preserving
   compatibility through a deliberate migration if behavior changes.
2. Profile representative concurrency and process-control work separately from
   child startup and tool execution. Compare a Python improvement with a candidate
   alternative under identical workloads, bounds and cleanup assertions.
3. For security, require a threat model and independent evidence that the launcher
   boundary is stronger; Rust memory safety alone does not repair OS policy. For
   portability, identify an actual PTY/Windows requirement and supported OS matrix.
4. Introduce a narrow `ProcessRunner`/`RunningProcess` contract only when a real
   second implementation, substitution boundary or cross-process lifecycle needs
   it. Keep model, conversation, approval and vendor semantics outside that port.
5. Before shipping a sidecar, write a separate ADR for protocol negotiation,
   request/event schemas, bounded frames, correlation, cancellation, crash recovery,
   binary distribution, support matrix and fail-closed Python fallback. Require
   both implementations to pass the same process contracts and platform smokes.

No Rust crate, sidecar, protocol package or native dependency is added by this
decision. Full modularization tracking and repository release gates remain with
the main worker.

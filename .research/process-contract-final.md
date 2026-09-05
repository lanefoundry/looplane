# Process contract implementation handoff

Date: 2026-09-05

## Scope and status

Implemented portable process-contract changes and focused tests. This is not a
full repository gate or a claim of complete cross-platform execution containment.
No plan/progress documents, Git state, tooling, agent, CLI, or unrelated worker
sources were changed. No commit was made. Sandbox policy was not broadened.
The previously approved macOS `(allow file-read-data (literal "/"))` exception
remains the only approved root-read exception; this work adds no permissions.

Inputs included the current process ADR, progress/remaining-gate records,
canonical process implementation, runtime facade, and existing process/sandbox
tests. Historical ADR observations about the macOS xfail and byte-bound gaps
must not be confused with this implementation's current evidence. Those shared
documents were deliberately left untouched.

## Implemented paths

- `src/looplane/execution/types.py`: exported 8 MiB default stdin byte cap;
  additive `output_incomplete` and `stdout_callback_incomplete` result fields.
  Incomplete results are not successful through `CommandResult.ok`.
- `src/looplane/execution/capture.py`: bounded UTF-8 rendering, incremental stdin
  encoding/validation, and cancellable nonblocking POSIX pipe reads/writes.
- `src/looplane/execution/local_process.py`: input preflight, finite deadlines,
  callback capacity, completion accounting, and bounded cleanup waits.
- `src/looplane/runtime.py`: retained compatibility entry points and private
  injection surfaces; forwarded the additive `max_stdin_bytes` option and
  exported its default. Canonical execution does not import this facade.
- `tests/execution/test_local_process.py`: replaced the previous UTF-8 overflow
  characterization with assertions enforcing the actual byte budget.
- `tests/execution/test_process_contracts.py`: new focused process contracts.

Existing sandbox, workspace, and runtime compatibility tests were executed
without changing their source. No sandbox or Landlock source changes were
needed for this implementation.

## Contract decisions

### Output and lines

Returned stdout/stderr text, including truncation markers, fits its configured
UTF-8 byte budget. Malformed input and multibyte cut points cannot enlarge the
rendered result beyond that budget. Raw byte counters continue to describe the
bytes captured from the process, not the size of replacement-decoded text.
Replacement expansion itself can mark output truncated. Callback lines have
the same encoded-byte bound, retaining existing newline/CRLF handling.

### Stdin

`max_stdin_bytes` defaults to 8 MiB and is configurable through both canonical
and compatibility runners. Oversized UTF-8 input is rejected before spawning;
it is not silently truncated. Encoding proceeds in bounded character slices,
with encoded chunks no larger than 64 KiB, rather than allocating a second
whole-input byte string. This bounds accepted encoded input and encoding chunks,
not the caller's already allocated Python string.

The default is an intentional new resource contract: callers needing more than
8 MiB must explicitly supply a larger positive integer cap. Constructor/runtime
validation rejects invalid caps and nonfinite timeouts before launch.

### Cancellation, deadlines, and callbacks

The deadline starts before stdin validation and sandbox resolution. Preexisting
cancellation and a deadline exhausted during preflight prevent process launch.
Cancellation takes precedence over timeout when both are observed together.
The deadline remains active after the process leader exits, through stdin,
stdout/stderr draining, and callback completion.

Callbacks remain ordered on the stdout reader, with bounded OS-pipe backpressure
rather than an unbounded delivery queue. An in-flight Python callback cannot be
forcibly terminated. Cancellation/deadline stops further delivery and allows
the runner to return; incomplete output/callback delivery is explicitly exposed
in the result. A process-wide semaphore limits outstanding callback readers to
eight. Capacity exhaustion refuses a new callback-bearing invocation before
spawn with return code 125; it does not bypass the cap or discard queued work.
A slot remains occupied until its reader actually exits.

### Process and pipe cleanup

POSIX pipe workers use nonblocking descriptors and short readiness waits so a
blocked stdin writer or inherited pipe does not force an unbounded main-thread
join. The main thread does not close a live worker's pipe behind its back.
Started workers receive one shared 0.25-second shutdown-join allowance.

The existing process-group TERM/KILL strategy is retained, including killing
same-group descendants after leader exit. Reaping waits are finite: 0.5 seconds,
then 1 second, and a final 1-second wait after direct kill if necessary. A process
that cannot be reaped raises rather than pretending successful cleanup.

The configured deadline is therefore not an absolute wall-clock upper bound:
OS process creation, synchronous preflight, scheduling, and cleanup allowance
can add latency. Deliberate session/process-group escape is not contained by
group signaling. The escaped-session test demonstrates bounded pipe shutdown
and explicitly cleans up the escaped fixture; it does not claim the runner
killed that escaped process.

## Validation evidence

Environment: local macOS 26.5.2 arm64, Python 3.11 through `uv`.

Focused command:

```sh
uv run pytest -o addopts='' -q tests/execution tests/sandbox tests/workspace tests/test_runtime.py
```

Result: **140 passed, 2 skipped in 17.46 seconds**. This includes the existing
local sandbox scenarios; skips are not counted as platform evidence.

New cases exercise both canonical and legacy runners where applicable:

- UTF-8 and malformed-byte stdout/stderr bounds, raw counters, and callback lines.
- Oversized ASCII/multibyte stdin rejection before spawn and exact-limit stdin.
- Chunked encoding without whole-string encoding.
- Invalid limits/nonfinite timeouts and pre-cancel/preflight deadline refusal.
- Blocking callbacks under cancellation, deadline, and early leader exit.
- Callback-capacity refusal, eventual release, and spawn-failure lease release.
- POSIX escaped-session inherited pipes and bounded runner completion.
- Finite reaping waits when a simulated process cannot be reaped.

Existing focused tests also cover normal execution, timeout/cancellation,
process-group cleanup, line delivery, sandbox launch/fail-closed behavior,
workspace use, and runtime compatibility.

Ruff commands:

```sh
uv run ruff check src/looplane/execution src/looplane/sandbox src/looplane/runtime.py src/looplane/landlock_run.py tests/execution tests/sandbox tests/workspace
uv run ruff format --check src/looplane/execution src/looplane/sandbox src/looplane/runtime.py src/looplane/landlock_run.py tests/execution tests/sandbox tests/workspace
```

Result: **All checks passed; 20 files already formatted**. Before the focused
test run, scoped Ruff autofix corrected one lint issue and formatting updated
three files in the execution/runtime test scope. No production imports were
used as a substitute for tests. No network tests or web research were used.

## Remaining evidence and contract limits

- Linux: no live Linux execution, Landlock syscall, or Linux sandbox-denial
  evidence was produced in this macOS run. Local fixtures do not establish
  kernel/filesystem enforcement on Linux. Run focused tests on a supported
  Linux host before claiming that platform's gate.
- Windows: no live Windows execution or descendant-containment evidence was
  produced. Non-POSIX pipe workers retain the blocking-I/O fallback; a blocked
  pipe may outlive runner return. POSIX process groups and cancellable descriptor
  behavior do not prove Windows cleanup. Windows-specific I/O cancellation and
  descendant lifecycle remain separate work, not completed contracts here.
- Blocking callbacks: at most eight callback-reader slots can remain held, but
  application code must release/return from a blocked callback to recover those
  slots and its thread/pipe resources. There is no claim of forced callback
  termination or resource recovery from arbitrary nonreturning Python code.
- Group cleanup: intentionally escaped sessions require a stronger separately
  designed containment boundary; this change does not expand sandbox privileges
  or claim to supply one.
- Repository integration: the full repository gate was not run by this worker.
  The focused result does not replace main's gate or authorize shared tracker
  completion.
- No Rust sidecar was introduced. The existing conservative ADR decision stands;
  this work adds correctness evidence, not a new comparative performance result.

The implementation is ready for main's integration review with the above limits
explicit. No permanent xfail was introduced to conceal a process-contract failure.

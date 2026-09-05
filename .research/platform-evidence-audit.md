# Platform/process evidence audit

Date: 2026-09-05. Scope: read-only inspection of plans, decision record, tests,
source, workflow configuration, and retained local evidence. Only this audit file
was written. No production/test/documentation changes, test runs, imports of the
application, sandbox executions, lint/builds, Git commands, or commits were made.
All CI commands below are proposals, not commands executed by this audit.

## Findings

1. The current recorded macOS gate establishes more than the process ADR says.
   The approved literal-root read rule is present in `sandbox/macos.py`, the real
   macOS test has no xfail, and the corrected gate records 1475 passed / 2 skipped.
   The old ADR and process handoff still describe the pre-fix SIGABRT/strict xfail.
   Those statements need historical qualification; they are not current blockers.
2. Linux launcher selection, argument construction, setup-failure refusal, and BPF
   construction tests are mostly simulated. Their passing on macOS does not
   establish Linux kernel enforcement. One Linux-only test actually executes a
   sandboxed Python process; the other Landlock availability-gated test only builds
   argv. Do not describe both as live enforcement tests.
3. The existing Linux smoke is a real filesystem enforcement probe when executed
   successfully on Linux, but its name overstates direct seccomp evidence: no
   denied syscall is attempted. It does not exercise bubblewrap or network denial.
4. Windows process-tree cleanup is not implemented by the inspected fallback.
   The launcher sets CREATE_NEW_PROCESS_GROUP, but cleanup calls terminate/kill
   on the immediate Popen object. There is no Job Object, descendant traversal,
   taskkill, or console-group signal dispatch in this process implementation.
5. The plan explicitly requires macOS/Linux sandbox fail-closed contracts and
   process-group termination. It does not explicitly require a new Windows Job
   Object implementation in Waves 0-2. Define the support boundary without
   claiming Windows parity or silently turning that omission into a new feature.

## Evidence matrix

| Surface | Inspected evidence | What it establishes | What it does not establish |
| --- | --- | --- | --- |
| macOS startup and writes | `tests/sandbox/test_launcher.py::test_macos_real_sandbox_allows_workspace_and_denies_outside_write`; corrected gate report and raw pytest summary | On the recorded macOS candidate, real Python startup, successful workspace write, PermissionError for outside write, and no outside artifact; xfail removed | Every macOS release/architecture/toolchain, outside-read regression coverage, live network isolation, Linux/Windows behavior |
| macOS policy rendering | `test_macos_profile_escapes_paths_and_keeps_default_deny` | Quoting, deny-default, literal root read, absence of recursive `(subpath "/")` and network allow rules in the rendered profile | Enforcement merely from inspecting profile text |
| macOS diagnostic boundaries | `.research/macos-sandbox-evidence/boundary-probes.json` and diagnosis | Recorded actual filesystem probes: allowed workspace access; EPERM for outside read/write, symlink escape and root-fd openat escape; root enumeration allowed | Fresh execution of the current candidate; a permanent automated test for each diagnostic probe |
| macOS network | Same diagnostic artifact | OS policy queries return denied for inbound/outbound; no added network permission in source | Real connection denial: the probes deliberately sent no packets and opened no sockets |
| POSIX subprocesses | `tests/execution/test_local_process.py`, parametrized canonical/legacy runner and exit/timeout/cancel | Real subprocess fixtures, readiness handshake, inherited descendant ignoring SIGTERM, expected result code and absence of delayed-write marker | Windows, deliberate setsid/process-group escape, every scheduling race, comprehensive OS-level process census |
| Linux selection and refusal | `tests/test_runtime.py`; `tests/sandbox/test_launcher.py` | Mocked platform/probe/executable selection, explicit unavailable-backend errors, no marker created when launch is refused; normalized argv | Working bwrap/user namespaces or Landlock restrictions on a Linux kernel |
| Linux seccomp construction | `tests/sandbox/test_sandbox_policy.py` | Expected x86_64/aarch64 denied numbers, BPF fields and unsupported-architecture refusal | Installation or actual syscall rejection on either architecture |
| Linux setup failure | `test_landlock_setup_failure_never_executes_command` | Stubbed ABI/ruleset/seccomp failures return 126 without reaching the mocked exec | A real kernel rejecting setup and the actual exec boundary under that failure |
| Linux Landlock argv test | `tests/test_runtime.py::test_sandboxed_command_wraps_linux_with_landlock_backend` | Requires a successful real availability probe, then checks serialized launch arguments | Child execution or filesystem denial; a successful ABI probe alone is insufficient |
| Linux live positive test | `tests/sandbox/test_sandbox_policy.py::test_landlock_sandbox_allows_dev_null` | On Linux, runs Python through forced Landlock and reads/writes /dev/null | Outside-path denial, bwrap, denied syscalls, network isolation |
| Linux live smoke | `scripts/smoke_linux_sandbox.sh` | On Linux: wrapper setup succeeds, workspace input is readable, task-home write and /dev/null access succeed; outside-write command fails and artifact is absent | Bubblewrap, denial errno specifically, a forbidden syscall attempt, network policy, or the whole canonical process API |
| Windows refusal | `test_unavailable_sandbox_never_starts_process[win32-...]` | Simulated unsupported-platform request returns 126 without starting the requested command | A native Windows run, Windows sandboxing, descendant termination |
| Windows subprocess fallback | `execution/local_process.py` | Static direct-process termination path and CREATE_NEW_PROCESS_GROUP spawn flag | Any proven Windows process-tree guarantee |

The corrected raw pytest log ends with `1475 passed, 2 skipped in 247.75s`.
It does not print skip reasons or individual passing node IDs, so this audit does
not reconstruct exact skip IDs from that summary. The retained earlier process
handoff attributes its two skips to Landlock tests on macOS; source predicates
explain that distinction, but the final summary itself is not per-test proof.

## Current source and historical evidence reconciliation

The primary current record is
`docs/plans/runs/modularization-corrected-gate-2026-09-05.md`, with raw summary in
`docs/plans/runs/modularization-corrected-gate-pytest.txt`, plus the current tracker
and real-test source. This audit read those artifacts; it did not rerun their gates.
A passing recorded candidate is not automatically evidence for later worktree edits.

`docs/plans/process-execution-decision.md` and `.research/process-execution.md`
retain the older 154-pass/2-skip/1-xfail extraction result and macOS startup failure.
`.research/macos-sandbox-diagnosis.md` describes the exception as proposed because
it predates authorization. Preserve these experiments as historical evidence; update
current-facing ADR wording separately rather than rewriting diagnostic artifacts.

The approved exception is `(allow file-read-data (literal "/"))`. It allows reads
of the root directory inode, including immediate directory enumeration. It is not
metadata-only or a zero-permission-change fix. The source still denies by default;
the exception does not grant recursive root reads. The diagnostic network results
are policy queries, while filesystem denial results are actual syscall probes.

Historical memory reports Linux CI run 33347933700 and a Docker Landlock smoke
passing before this modularization candidate. That is useful context, not current
candidate proof: remote logs and revision identity were not refreshed in this audit.
Memory source: `MEMORY.md:72-90`, rollout `01a0539b-8081-72b1-b09d-bac54333f78b`.

## Linux backend and CI boundaries

`.github/workflows/python-ci.yml` currently declares one `ubuntu-latest` job:
`uv sync --extra dev --extra sandbox`, full pytest, a WebSocket smoke, then
`bash scripts/smoke_linux_sandbox.sh`. This establishes configured intent, not an
observed current run. It has no explicit macOS/Windows matrix or dedicated forced
bubblewrap smoke. Whether the hosted image happens to contain usable bwrap is not
recorded by the workflow and was not checked here.

`auto` prefers an installed bwrap executable. If bwrap exists but launch fails,
the inspected code does not retry with Landlock. A forced-backend test must retain
that failure instead of replacing it with a pass from another backend.

The shell smoke exits 0 with a skip message on non-Linux hosts. A successful shell
exit on macOS therefore cannot be counted as Linux evidence. On Linux it invokes
the legacy standalone Landlock entry point directly, installs restrictions before
exec, and checks a positive command before testing an outside write. Its negative
case accepts any command failure plus no artifact, which is weaker than asserting
the intended denied operation and errno inside an otherwise-successful process.

The Landlock implementation handles filesystem access and installs a seccomp deny
list. That list does not include socket/connect/bind operations, and the ruleset
contains filesystem access fields only. There is no implemented network-denial
contract in this backend from those mechanisms. Do not infer network parity from
macOS deny-default or bubblewrap's `--unshare-all`. External container/network
restrictions are a separate boundary, not evidence supplied by this wrapper.

## Windows behavior and plan interpretation

Relevant owner: `src/looplane/execution/local_process.py`, functions
`_signal_process_group`, `_stop_process_tree`, and `run_local_process`.

On POSIX, spawn starts a new session and cleanup signals the inherited group,
including after a normally exiting leader. On non-POSIX systems, cleanup acts on
the immediate child only while it is alive. The normal-exit descendant cleanup
branch is POSIX-only. CREATE_NEW_PROCESS_GROUP does not cause this code to enumerate
or kill Windows descendants. If descendants retain output handles, reader joining
and pipe cleanup may also behave differently; no Windows liveness guarantee follows
from the POSIX fixture. This is a static risk boundary, not a reproduced Windows hang.

The strongest descendant fixture uses `os.fork`, is skipped when `os.name != "posix"`,
and checks exit/timeout/cancel for both canonical and legacy runners. Its cleanup
also directly kills recorded PIDs. Do not count a Windows suite pass with this test
skipped as proof of descendant cleanup.

`repository-modularization-plan.md`, Conditional Wave 3, requires process-group
termination, I/O bounds, cancellation and macOS/Linux fail-closed behavior before
considering Rust. It requires a platform-support ADR before a sidecar ships; it does
not name Windows Job Objects as a mandatory Wave 0-2 implementation task. The process
decision explicitly defers identifying a concrete PTY/Windows requirement and OS matrix.

Recommended acceptance interpretation: require actual macOS/Linux evidence for the
supported process/sandbox claims, and explicitly record Windows direct-process-only,
unsandboxed compatibility as unproven until a native job runs. Mark Windows descendant
cleanup unsupported/unproven, not complete. If Windows tree cleanup is a product
requirement, approve it separately with native acceptance tests and an implementation
choice; do not silently broaden a behavior-preserving modularization slice.

## Exact proposed checklist wording

These are proposals for the main owner, not edits to the plan or checklist. Keep
candidate-specific gates unchecked until their evidence is attached.

```markdown
- [ ] Record the candidate identity, OS release, architecture, Python version,
  backend selection, command, raw output and skip/xfail reasons for each process
  gate. Historical and simulated results are labeled separately from native runs.
- [ ] On the supported macOS candidate, the real sandbox test passes without
  xfail or skip: Python starts, workspace writes succeed, outside writes raise
  PermissionError and create no artifact. Preserve the approved literal-root
  enumeration caveat and the regression rejecting recursive root read access.
- [ ] Reconcile the process ADR's pre-fix macOS SIGABRT/xfail statement with the
  approved exception and its post-fix evidence; preserve historical probes.
- [ ] Reproduce outside-read, symlink and root-fd escape denials on the current
  macOS candidate, or label the diagnostic artifact as earlier-candidate evidence.
  Keep network-policy queries distinct from live network enforcement.
- [ ] Run process exit/timeout/cancellation and inherited-group descendant tests
  on both supported POSIX platforms. State that intentional session escape and
  exhaustive scheduling races are outside the established guarantee.
- [ ] On Linux, force Landlock and prove successful allowed operations, /dev/null
  compatibility and denied outside access through the current canonical launcher.
  Kernel/backend unavailability is a blocked gate, not an enforcement pass.
- [ ] On Linux, force bubblewrap in a separate native job and prove successful
  allowed operations and denied outside access. Record namespace/LSM constraints;
  an argv test, installed binary, auto-selection or Landlock pass is insufficient.
- [ ] Record real seccomp setup and a discriminating denied-syscall probe on each
  claimed Linux architecture. Distinguish inherited host/container restrictions
  from restrictions added by Looplane; filter-construction tests are simulated.
- [ ] State each backend's network contract explicitly. Do not claim Landlock
  denies network access from its filesystem rules and current syscall deny list.
- [ ] Record the Windows support decision: direct-process fallback and unavailable
  OS sandbox are distinct from descendant cleanup. If Windows tree cleanup is
  required, add native exit/timeout/cancel descendant tests and an implementation;
  otherwise explicitly exclude that guarantee instead of marking it passed.
- [ ] Keep UTF-8 decoded-output expansion, materialized stdin, indefinitely blocked
  callbacks, and total-memory limits separate from platform enforcement evidence.
  Resolve required invariants explicitly; passing characterization is not repair.
```

Suggested current-facing ADR replacement for its obsolete macOS bullet:

> The pre-fix macOS 26.5.2 arm64 startup abort was diagnosed as a denied read of the
> root directory inode. The separately approved literal-root read exception is
> implemented, and the recorded corrected candidate passed the real startup,
> workspace-write and outside-write-denial test without xfail. Root-directory
> enumeration is an explicit permission increase; recursive root reads remain
> excluded. Other macOS versions/toolchains and live network enforcement are not
> established by this result. Preserve the earlier failure and diagnostic artifacts
> as historical evidence.

## Proposed exact CI commands

Run only after the main owner authorizes execution. A fresh checkout or otherwise
identified candidate is required; this audit does not run these commands.

### Shared process contracts on macOS and Linux

Run in separate native jobs. Save the job's immutable candidate identifier using CI
metadata, plus OS/architecture/interpreter information; do not infer OS from job name.

```bash
set -euo pipefail
uv sync --extra dev --extra sandbox
mkdir -p artifacts/platform
uv run python -c 'import platform,sys; print(platform.platform()); print(platform.machine()); print(sys.version)' > artifacts/platform/host.txt
uv run pytest -o addopts='' -q -rs \
  tests/execution tests/sandbox tests/test_runtime.py \
  --junitxml=artifacts/platform/process.xml
```

The aggregate suite legitimately skips other-OS tests. Enforce non-skipped execution
of each native acceptance test separately; aggregate exit 0 alone is insufficient.

### Required native macOS gate

```bash
set -euo pipefail
test "$(uname -s)" = Darwin
command -v sandbox-exec
uv run pytest -o addopts='' -q -rs \
  tests/sandbox/test_launcher.py::test_macos_real_sandbox_allows_workspace_and_denies_outside_write \
  tests/sandbox/test_launcher.py::test_macos_profile_escapes_paths_and_keeps_default_deny \
  --junitxml=artifacts/platform/macos-enforcement.xml
uv run python - <<'PY'
import xml.etree.ElementTree as ET
root = ET.parse('artifacts/platform/macos-enforcement.xml').getroot()
cases = list(root.iter('testcase'))
assert len(cases) == 2, 'required native cases missing'
assert not any(case.find(tag) is not None for case in cases
               for tag in ('skipped', 'failure', 'error')), 'native gate did not pass'
PY
```

This gates the existing tests only; the historical openat/symlink/read probes need
separate current-candidate evidence or future explicitly authorized regression tests.

### Required native Linux baseline

```bash
set -euo pipefail
test "$(uname -s)" = Linux
uv run pytest -o addopts='' -q -rs \
  tests/sandbox/test_sandbox_policy.py::test_landlock_sandbox_allows_dev_null \
  --junitxml=artifacts/platform/linux-landlock-positive.xml
bash scripts/smoke_linux_sandbox.sh
```

The explicit Linux guard prevents the smoke's non-Linux exit-0 skip being credited.
For a forced bubblewrap job, provision the binary on the intended Ubuntu runner:

```bash
sudo apt-get update
sudo apt-get install -y bubblewrap
```

Installation alone is not evidence; run the forced-backend probe below. If namespace
or host LSM policy prevents launch, record the runner as unsuitable/blocked. Do not
silently replace the backend or weaken system policy to obtain a green result.

### Forced Linux filesystem probes for both backends

This proposed inline probe uses synthetic files and existing APIs. It is not an
existing repository test and was not run or validated by this audit. Execute as a CI
step after dependency/backend provisioning; retain stdout/stderr as job artifacts.

```bash
uv run python - <<'PY'
import json
import sys
import tempfile
from pathlib import Path
from looplane.execution.local_process import run_local_process
from looplane.sandbox.policy import resolve_command_sandbox

assert sys.platform.startswith('linux'), 'requires a real Linux host'
with tempfile.TemporaryDirectory(prefix='looplane-platform-') as directory:
    root = Path(directory)
    outside = root / 'outside'
    outside.mkdir()
    secret = outside / 'synthetic-input'
    secret.write_text('synthetic-only')
    for backend in ('landlock', 'bubblewrap'):
        workspace = root / backend
        task_home = root / (backend + '-home')
        workspace.mkdir()
        task_home.mkdir()
        forbidden = outside / (backend + '-write')
        script = '\n'.join([
            'import errno, os',
            'from pathlib import Path',
            "Path('allowed').write_text('ok')",
            f"Path({str(task_home / 'allowed')!r}).write_text('ok')",
            "with open(os.devnull, 'w') as f: f.write('discard')",
            "with open(os.devnull) as f: f.read()",
            f'for path, operation in [({str(secret)!r}, "read"), ({str(forbidden)!r}, "write")]:',
            ' try:',
            '  if operation == "read": Path(path).read_text()',
            '  else: Path(path).write_text("escaped")',
            ' except OSError as exc:',
            '  assert exc.errno in (errno.EPERM, errno.EACCES, errno.ENOENT), exc',
            ' else: raise AssertionError("outside access succeeded")',
            'print("allowed-and-denied")',
        ])
        result = run_local_process(
            (sys.executable, '-c', script), cwd=workspace,
            timeout_seconds=10, max_output_chars=4000,
            sandbox=resolve_command_sandbox(
                cwd=workspace, task_home=task_home, backend=backend,
            ),
        )
        print(json.dumps({'backend': backend, 'returncode': result.returncode,
                          'stdout': result.stdout, 'stderr': result.stderr}))
        assert result.ok and result.stdout == 'allowed-and-denied\n'
        assert (workspace / 'allowed').read_text() == 'ok'
        assert (task_home / 'allowed').read_text() == 'ok'
        assert not forbidden.exists()
PY
```

ENOENT is accepted for the outside access because bwrap can hide the path entirely;
Landlock may return a permission error instead. Positive operations and the final
sentinel prevent unrelated startup failures from satisfying the denial assertions.

For direct seccomp evidence, add an authorized native probe that compares a harmless
operation outside and inside the wrapper. For example, a zero-flags `unshare` probe
using the architecture-specific number already in the deny list can distinguish an
outside success from inside EPERM. If the host/container already denies it, that
probe is inconclusive, not a Looplane enforcement pass. The existing smoke command
must not be relabeled as having performed this comparison. No new seccomp test path
is invented here; author and validate the probe separately before making it a gate.

### Optional Windows diagnostic job, not a tree-cleanup acceptance gate

On a native Windows runner with uv available, use PowerShell:

```powershell
uv sync --extra dev --extra sandbox
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
New-Item -ItemType Directory -Force artifacts/platform | Out-Null
uv run python -c 'import os,platform,sys; assert os.name == "nt"; print(platform.platform()); print(platform.machine()); print(sys.version)'
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
uv run pytest -o addopts='' -q -rs tests/execution `
  tests/sandbox/test_launcher.py::test_unavailable_sandbox_never_starts_process `
  --junitxml=artifacts/platform/windows-process.xml
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
```

This command exercises existing diagnostic coverage and may expose further portability
failures; a passing result still skips the POSIX descendant fixture. A Windows tree
acceptance job requires new native descendant cases first, covering normal leader
exit, timeout and cancellation, descendants retaining pipe handles, and deterministic
cleanup/readiness observations. None was added or claimed present in this audit.

## Remaining ownership

Main owns checklist/ADR reconciliation, support-matrix decisions, authorization of
new tests/probes, CI configuration changes and actual execution. The Python-retention
ADR remains justified by its limited local measurements; neither unresolved Windows
coverage nor Linux evidence gaps by themselves justify a Rust sidecar. This audit
makes no new full-goal completion, universal containment, or current remote-CI claim.

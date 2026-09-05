# macOS restrictive sandbox SIGABRT diagnosis

Date: 2026-09-05
Host: macOS 26.5.2 arm64, CPython 3.11.14.
Scope: diagnosis only. Production process/sandbox sources, tests, ADR, tracker and
xfail markers were not edited. All experimental profiles and fixtures are under
`.research/macos-sandbox-evidence/`. No web research or network traffic was used.

## Finding

The verification profile denies `file-read-data` on the root directory itself,
`/`. This host's dyld/libignition opens that directory during executable startup.
The open returns EPERM and libignition aborts before Python, shell, or even
`/usr/bin/true` reaches application code.

The smallest tested successful change to the existing generated profile is:

```scheme
(allow file-read-data (literal "/"))
```

This is a **proposed policy change for main review**, not an implemented fix.
It grants data access to the root directory inode, including enumeration of its
immediate entries. It does **not** grant recursive filesystem access, reads of
otherwise denied descendants, writes, Mach services, or network access. Root
enumeration is an actual access increase and must be accepted explicitly in the
deliberate policy fix; it should not be described as metadata-only access.

The failure is locally reproducible and has a specific cause. A permanent broad
xfail is not its resolution, and the evidence does not justify declaring macOS
sandboxing generally unavailable or requiring a Rust launcher.

## Native failure evidence

Existing native crash reports:

- `/Users/xiaoxu/Library/Logs/DiagnosticReports/sh-2026-09-05-183501.000.ips`,
  PID 46674.
- `/Users/xiaoxu/Library/Logs/DiagnosticReports/python3.11-2026-09-05-183501.ips`,
  PID 46673.

Both report `EXC_CRASH`, `SIGABRT`, termination namespace `<0x23>`, code `2`.
Their faulting stacks include:

```text
__abort_with_payload
abort_with_payload_wrapper_internal
abort_with_reason
ignition_halt
boot_boot
ignite
dyld4::CacheFinder::CacheFinder(...)
dyld4::ProcessConfig::DyldCache::DyldCache(...)
dyld4::ProcessConfig::ProcessConfig(...)
dyld4::start(...)
start
```

Kernel events match those PIDs and timestamps:

```text
2026-09-05 18:35:00.960 Sandbox: python3.11(46673) deny(1) file-read-data /
2026-09-05 18:35:00.971 Sandbox: sh(46674) deny(1) file-read-data /
```

New isolated reproductions repeat that denial. Their filtered native log is
`macos-sandbox-evidence/native-denials.log`, with failing PIDs 10839, 10842,
10843, 10845, 10846 and 10849. The root-only candidates, PIDs 10851 and 10863,
start successfully. Other denials, such as `/dev/dtracehelper`, remain visible
after successful startup; they are not a reason to grant additional permissions.

Disassembling the local `/usr/lib/dyld` arm64e slice establishes the native action:

```text
000000000007ccac  ... literal pool for: "/"
000000000007ccb0  mov x0, x20
000000000007ccb4  mov w1, #0x20100000
000000000007ccb8  bl _open
000000000007ccc0  tbnz w0, #0x1f, 0x7cd28
...
000000000007cd4c  ... "failed to open root directory: %s: %d"
000000000007cd5c  bl _ignition_halt
```

The complete extracted function is in
`macos-sandbox-evidence/dyld-boot-boot-arm64e.txt`. Local SDK `sys/fcntl.h`
defines `O_RDONLY=0`, `O_DIRECTORY=0x00100000` and
`O_NOFOLLOW_ANY=0x20000000`. Thus the actual action is equivalent to:

```c
open("/", O_RDONLY | O_DIRECTORY | O_NOFOLLOW_ANY);
```

Applying the original sandbox **after** Python/dyld initialization and then
performing `os.open('/', 0x20100000)` returns errno `1` (EPERM). The same action
with the literal-root exception succeeds. This separates the denied OS operation
from interpreter startup, argument handling, subprocess draining and group cleanup.

The crash reports expose no rendered `asi` message and child stderr is empty.
The compiled native failure format plus observed arguments/errno imply
`failed to open root directory: /: 1`; that rendered sentence is an inference
from disassembly and probes, not a captured stderr string.

## Reduced exact reproducer

The following four-line profile is sufficient to reproduce the same SIGABRT using
`/usr/bin/true`, without Python, a shell command body, workspace operations or
network access:

```scheme
(version 1)
(deny default)
(allow process-exec)
(allow file-read-data (literal "/usr/bin/true"))
```

It is preserved in `.research/macos-sandbox-evidence/true-fail.sb`.
`true-root-only.sb` contains the identical four lines plus the proposed
literal-root rule. From the repository root:

```sh
/usr/bin/sandbox-exec -f .research/macos-sandbox-evidence/true-fail.sb /usr/bin/true
/usr/bin/sandbox-exec -f .research/macos-sandbox-evidence/true-root-only.sb /usr/bin/true
```

Measured with Python `subprocess.Popen` and a five-second deadline:

| Profile | PID | Return code | stdout/stderr |
|---|---:|---:|---|
| Four-line profile | 35275 | -6, SIGABRT | empty |
| Same profile plus literal-root read | 35276 | 0 | empty, as expected for `true` |

A shell normally represents termination by SIGABRT as exit status 134. The raw
Python results are in `macos-sandbox-evidence/true-minimal-results.json`.
These reduced profiles are diagnostic reproducers, not proposed replacements for
the production verification profile.

## Existing-profile experiments

`macos-sandbox-probes.py` generates the existing profile by calling the frozen
canonical `_macos_sandbox_profile()` with a synthetic workspace and trusted
Python read roots. It adds only each named diagnostic rule to the profile copy.

| Addition to existing profile | `/bin/sh -c 'printf ready'` | Python `print('ready')` |
|---|---|---|
| None | SIGABRT, -6 | SIGABRT, -6 |
| `file-read-metadata (literal "/")` | SIGABRT, -6 | SIGABRT, -6 |
| `file-read-xattr (literal "/")` | SIGABRT, -6 | SIGABRT, -6 |
| `file-read-data (literal "/")` | 0, `ready` | 0, `ready` |

The original profile already permits metadata reads. Repeating that permission or
adding root xattr reads does not satisfy the loader's directory-open operation.
No root `subpath`, global read permission, write permission, network rule, or
allow-default replacement was used in these experiments.

Raw generated profiles and results are in
`macos-sandbox-evidence/{original,root_metadata,root_xattr,root_data}.sb`
and `macos-sandbox-evidence/profile-matrix.json`. Additional reduced shell profiles
and results are retained in the same directory.

## Least-privilege boundary checks

`macos-sandbox-boundary-probes.py` uses synthetic local files, a symlink and the
existing profile plus the single literal-root rule. Results are in
`macos-sandbox-evidence/boundary-probes.json`:

| Operation | Observed result |
|---|---|
| Write/read inside allowed workspace | allowed |
| Read synthetic file outside read roots | EPERM |
| Write outside writable roots | EPERM |
| Read outside through workspace symlink | EPERM |
| Open root directory with native flags | allowed |
| `openat(root_fd, denied_relative_path, O_RDONLY)` | EPERM |
| Enumerate immediate root directory entries | allowed; explicit tradeoff |
| `sandbox_check(..., "network-outbound", 0)` | 1, denied |
| `sandbox_check(..., "network-inbound", 0)` | 1, denied |

The same network policy queries return 0 outside the sandbox. These are OS policy
queries, not connection attempts: no DNS, listeners, sockets, packets or live
services were used. They support the unchanged network-policy conclusion but do
not replace a separate network-enforcement smoke. The filesystem checks exercised
real open/write syscalls and all denied cases returned EPERM.

## Proposed deliberate fix and acceptance for main

1. Add the single explicit `(allow file-read-data (literal "/"))` rule to the
   macOS verification profile renderer, with a comment explaining dyld/libignition
   root-directory startup requirements and the nonrecursive boundary. Keep the
   rule separate from `_normalize_sandbox_roots()` and the read-root list: putting
   `/` into that list would render `(subpath "/")` and grant recursive reads.
2. Retain deny-default, all current denied descendant paths, writable roots,
   backend validation and absent network grants. Do not allow all `file-read*`,
   add Mach lookup services, or permit unrelated paths merely because successful
   processes still generate denial logs.
3. Replace the broad version-specific xfail with real startup and workspace/outside
   access assertions once the fix is deliberately adopted. Add a regression that
   distinguishes `(literal "/")` from `(subpath "/")`, and an in-process root
   `openat`/symlink denial check. Keep genuine platform unavailability separate.
4. Run the existing macOS process/sandbox tests and representative verification
   commands under the proposed policy. The diagnostic Python/shell/true probes
   show the startup repair, not universal compatibility for every toolchain.
5. Update the process ADR and handoff to replace the unexplained macOS gap with the
   adopted rule, its evidence and its root-enumeration tradeoff. Reconcile the
   xfail explicitly rather than retaining it as completed contract evidence.

If even root-directory enumeration is outside the accepted threat model, this
single-rule fix needs rejection or a different launch design. The tested
metadata-only alternative does not work; no narrower startup-only Seatbelt
permission has been established in this diagnosis. Do not claim the exception has
zero privilege impact.

The proposed exception repairs the measured startup failure while retaining the
tested descendant-path and network denials. Broader OS/version compatibility,
live network enforcement, and the separate UTF-8/stdin/callback contract gaps are
not resolved by this diagnosis. Main owns any source fix, xfail removal, gate run
and commit; no such changes were made here.

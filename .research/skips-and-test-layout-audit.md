# Skips and test-layout audit

Status: static audit only. No tests, collection, lint, builds, Git commands, production
execution, process implementation review, or architecture ownership audit performed.
Only this report was written. No production, test, or plan files were changed.

## Evidence and confidence

The latest located successful full-suite evidence is
`.research/corrected-final-gate/pytest.log:22`:
`1475 passed, 2 skipped in 247.75s (0:04:07)`.
The earlier `.research/authorized-final-gate/pytest-retry.log` reports three failures
with the same two-skip count; it is not the successful final evidence.

The successful log uses quiet progress output, not explicit `SKIPPED [n]` reasons.
Its two `s` positions are collected items 195 and 1078. The preserved
`.research/authorized-final-gate/collection.log` maps those ordinals to the tests
below. Current static skip conditions agree with both identifications on the macOS
workspace. This is strong static correlation, not a fresh runtime observation or a
claim that the earlier collection artifact is certified identical to the corrected
snapshot. A future authorized gate can retain `-rs` output to remove that caveat;
no rerun is needed merely to produce this report.

## Each skip

| Test | Condition and meaning | Does it block current modularization? |
|---|---|---|
| `tests/sandbox/test_sandbox_policy.py:17`, `test_landlock_sandbox_allows_dev_null` | Decorator at line 16 skips when `sys.platform` does not start with `linux`, reason `Landlock is Linux-specific`. This is a real Landlock execution check: the child opens `/dev/null` for reading and writing through the verification sandbox. It is intentionally unavailable on macOS. | Not a functional regression or a reason to reject a behavior-preserving extraction on this platform. It leaves Linux enforcement unproven by this local gate. Retain the test and require separate supported-Linux evidence before claiming Linux enforcement coverage. |
| `tests/test_runtime.py:65`, `test_sandboxed_command_wraps_linux_with_landlock_backend` | At line 68 it probes `landlock_run.landlock_available()` and at line 69 skips with `Landlock not supported by the running kernel`. It then checks launcher argv and serialized policy, rather than running an enforced child. The real-kernel probe occurs before the test monkeypatches platform to Linux. | Not a modularization failure on macOS. It leaves this legacy compatibility/launcher assertion unexecuted in this gate. The placement and host-dependent setup are migration/coverage debt, not authorization to delete the test or weaken fail-closed assertions. |

These are platform/capability skips, not missing provider credentials, network-gated
smokes, expected failures, or tests disabled specifically for modularization.
The `/dev/null` test has only an OS-level skip: on Linux with unavailable Landlock it
is not statically configured to skip automatically. Do not describe it as a blanket
unsupported-kernel exemption.

Other current skip candidates are distinct: missing ripgrep in `test_tools.py`,
non-POSIX process-group checks in `test_tools.py` and `execution/test_local_process.py`,
and the non-macOS/unavailable-sandbox-exec paths in `sandbox/test_launcher.py`.
They do not match the two observed progress positions. The macOS test's unavailable
launcher branch asserts fail-closed behavior before skipping enforcement measurement.
No new skip/xfail policy is proposed here.

Existing deterministic launcher tests include forced availability/unavailability,
bubblewrap argv, Landlock setup failure before exec, and unsupported-platform policy
checks. Their presence does not substitute for real Linux sandbox execution. A pure
launcher-serialization test could eventually use an injected availability probe,
while the actual enforcement test remains platform-specific; that is an optional
future test change, not an edit or gate result from this audit.

## Plan requirement and current placement

The canonical plan's `Test layout migration` section requires owner directories for
terminal, commands, runtimes/codex, execution, sandbox, workspace, tooling, agent,
contracts and integration. It explicitly says to move tests in the same slice as
production, keep cross-component state-machine tests in integration, prefer recorded
frames/fake clocks/runners/filesystem fixtures, and retain PTY/Linux sandbox/build/
WebSocket/opt-in provider smokes as separate evidence layers.

Placement is **partially migrated, not complete**. All listed owner directories
except `tests/integration/` currently exist. Directory existence and added leaf tests
do not establish that the original feature suites migrated with their production.
The following is a test-placement inventory, not an audit of production ownership.

| Intended owner | Current matches | Remaining placement work |
|---|---|---|
| `terminal/` | Clipboard, links, feature widgets, leaf compatibility, projection/binding tests. | Root `test_tui.py`, `test_transcript.py`, `test_transcript_export.py` still cover this surface. Split leaf/App behavior into terminal and cross-component conversation flows into integration. Real `test_tui_pty.py` should remain an explicitly recognizable integration/evidence layer, not be lost among leaf tests. |
| `commands/` | `test_composition.py` covers lazy imports, dependency ports, constructor compatibility, command-owned caches/resources and session indexing. | Root `test_cli.py`, `test_cli_config.py`, `test_cli_onboarding.py` and command-specific portions of Cloudflare/auth/plugin/slash-command tests remain. Separate command behavior from shared auth/provider/domain tests rather than moving every similarly named file wholesale. |
| `runtimes/codex/` and `runtimes/` | Codex `test_leaf_helpers.py` and `test_protocol_owners.py`, including recorded protocol sequence tests. | Root `test_codex_app_server.py`, `test_codex_backend.py`, `test_codex_conversation.py`, `test_external_cli_backends.py` and Claude runtime suites remain. Place adapter-local behavior with runtimes; place disposable workspace policy with workspace and composed runner/session flows with integration. Do not treat model-provider adapters as external coding runtimes. |
| `execution/` | `test_local_process.py` and `test_process_lines.py` already match bounded process/callback/cancellation ownership. | Residual root `test_runtime.py` now chiefly contains sandbox launcher/legacy facade cases, not a reason to relocate it blindly into execution. Keep tool-level process-group behavior in tooling or integration when its assertion spans tool orchestration. No process implementation assessment is made here. |
| `sandbox/` | `test_launcher.py` and `test_sandbox_policy.py` contain deterministic policy/launcher and real OS contracts. | Root `test_runtime.py` launcher tests belong here or in a clearly scoped compatibility test. Root `test_sandbox_entry.py` imports the hosted task entry, model and event contracts; do not classify the whole file as low-level OS sandbox policy merely because of its name. |
| `workspace/` | `test_git_preparation.py` and `test_local_git.py` cover disposable clones, source integrity, timeout and compatibility. | Root `test_conversation_workspace.py` contains workspace audit/source-integrity/cleanup cases and should move with that owner. Vendor conversation host suites need splitting where they combine workspace and runtime behavior. |
| `tooling/` | Leaf contract and MCP bridge suites are placed correctly. | The large root `test_tools.py` still holds named-check allowlists, transactions/rollback, filesystem edits, search and patch behavior. Decompose by those owners while preserving all path, limit, rollback and compatibility assertions. This is substantial migration work, not merely a missing folder. |
| `agent/` | `test_state_context.py` covers state restoration, checkpoints, event ordering, context additions and lifecycle. | Root `test_check_reuse.py`, `test_cost_tracking.py`, `test_subagents.py`, `test_approval_budget.py` and runner suites remain. Put isolated model/scheduler/verification/completion tests here; keep end-to-end approval/resume/checkpoint/finalization flows in integration. Do not claim absent coverage just because it still lives at root. |
| `contracts/` | Discovery/live capabilities, event-sink compatibility and external-agent compatibility suites. | Root `test_conversation_runtime.py` and `test_runtime_semantics.py` have shared semantic/schema tests that fit here. Split capability-contract checks from runtime-registry discovery behavior. Existing shared model/session/policy tests are not all required to move solely to fill this directory. |
| `integration/` | Directory absent. | Clear candidates include `test_loop_e2e.py`, `test_interactive_runner.py` approval/resume sequences, `test_external_runner_integration.py`, cross-component controller tests, WebSocket and PTY suites. Keep contract/leaf checks near their owners instead of relabeling every test using a filesystem fixture as integration. |

The plan does not require eliminating every root test. Root-wide architecture,
packaging/security/startup gates and unaffected feature tests may remain where they
are when documented. It does require the moved features' tests and the explicit
cross-component integration ownership to be reconciled. Keeping a test pointed at a
legacy facade can be deliberate compatibility coverage; changing its folder does
not require replacing that intentional import with a canonical one.

## Completion implications and bounded handoff

1. The two skips do not by themselves block a passing macOS modularization gate.
   They do prevent describing that gate as real Linux sandbox enforcement evidence.
   Preserve the separate Linux evidence requirement and disclose unavailable coverage.
2. The test-layout acceptance item cannot currently be marked complete. In particular,
   integration ownership is missing and major moved-feature suites remain at root.
   Main must either finish migration under explicit edit authorization or record an
   explicit plan-approved deferral; a passing full suite does not satisfy placement.
3. Future migration should preserve collected cases/parametrization, fixture visibility,
   helper imports, test IDs where operationally relied upon, and separate smoke layers.
   Root `test_cli.py` directly imports `plain_cli_output` from `conftest`; account for
   this helper dependency when relocating, without changing its assertion semantics.
4. Move bounded feature clusters, not an indiscriminate bulk root rename. Record
   before/after collection and focused/full gate evidence only when authorized.
   This report supplies no fresh gate or coverage result and requests no immediate run.

No production corrections, test moves, test rewrites, marker changes or documentation
plan updates were made. Process implementation and architecture ownership decisions
remain with their separate audit/implementation owners.

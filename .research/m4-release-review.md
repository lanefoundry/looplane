# M4 independent release review

Date: 2026-08-21 (Asia/Taipei)

Baseline: committed M3 at `6ed71c2`; scope is the current uncommitted M4 working tree.

Method: current source/tests/docs, installed CLI help, package contents, retained evidence, and raw
temporary M4 run bundles while they still exist. The Codex check was metadata-only against PCA's
own XDG-state path. No token value or credential-bearing environment value was read, `~/.codex`
was not inspected, and no web page was fetched. This report is the only file written by the review.

## Verdict

**NEEDS WORK.** There is no Critical or High finding, and the core release gates pass. However, the
Claude external backend currently has a reproducible fail-open terminal classification: an
error-shaped result can be reported as `completed`. Cancellation also lacks prompt subprocess
cleanup, and the documentation overstates what “never reads tokens” can mean while deliberately
passing the user's `HOME` to the official child. These are implementation/documentation blockers,
not missing external credentials.

The absent PCA Codex browser grant is a separate **external dependency**. The Codex implementation
and local contract coverage are sufficient to wait for user authorization; no authenticated Codex
E2E is claimed. It does not cause the NEEDS WORK verdict, and the official Codex CLI login must not
be used as a substitute.

## Findings

### Medium — release blocker: Claude result classification fails open on missing/non-boolean `is_error`

`ClaudeCodeBackend._normalize_event()` only copies `is_error` when its value is a Python boolean
(`src/coding_agent/claude_backend.py:198-209`). `run()` then considers the external task successful
unless some normalized event has `is_error is True` (`:283-294`). It does not require an explicit
success value or success subtype.

Consequently this zero-exit, syntactically valid stream is classified as `completed`:

```json
{"type":"result","subtype":"error_during_execution","result":"failed"}
```

The review reproduced `malformed=False`, a retained result event with subtype
`error_during_execution`, and the current success predicate evaluating true. The existing error
test includes `"is_error": true`, so it does not cover this protocol-drift/missing-field case
(`tests/test_claude_backend.py:57-76`).

Require positive terminal evidence: for example, exactly one terminal result with
`is_error is False` and an allowlisted success subtype. Missing, non-boolean, contradictory, or
unknown terminal fields must fail closed. Add tests for omitted/non-boolean `is_error`, an error
subtype with exit 0, duplicate result events, and unknown subtype/version drift.

### Medium — release blocker: cancellation/Ctrl-C does not own the external process lifecycle

The backend runs `run_bounded_command()` in `asyncio.to_thread()` and receives only the completed
`CommandResult` (`src/coding_agent/claude_backend.py:257-267`). The subprocess handle lives inside
the worker thread. Cancelling `ClaudeCodeBackend.run()` therefore cannot signal its process group.
The public command catches `OSError`/`ValueError`, not cancellation or `KeyboardInterrupt`
(`src/coding_agent/cli.py:271-278`). During `asyncio.run()` shutdown, the default executor may wait
for the worker until the configured timeout, which is 300 seconds by default, while the official
CLI can continue network/process activity.

The normal timeout path in `runtime.py` does terminate the process group and is tested; that does
not cover task cancellation. The external backend needs cancellable process ownership (or an
explicit process handle/control object), with graceful terminate then bounded kill escalation and
a cancellation test that proves descendants cannot outlive the caller.

### Medium — documentation/security boundary: preserving `HOME` means the child reads its own auth

The environment allowlist deliberately retains the host `HOME` so the installed official Claude
Code process can locate its authentication (`src/coding_agent/claude_backend.py:27-42,95-119`).
The Python coordinator does exclude token/API-key environment variables, uses a temporary cwd,
redirects cache/tmp state, passes `--safe-mode`, disables slash commands and tools, selects plan
permission mode, and disables session persistence (`:135-150,257-266`). Installed Claude Code help
confirms these flags and says safe mode disables user customizations while auth and admin-managed
policy still work.

That is a useful restricted process boundary, but it is not filesystem or credential isolation.
The official child necessarily reads its own HOME/keychain authentication and has the user's OS
authority. Therefore the module statement “never reads ... Claude credentials”
(`src/coding_agent/claude_backend.py:1-4`) and README statement that the backend “never reads or
forwards Claude tokens” (`README.md:31-32`) are too absolute. The env test proves values such as
`CLAUDE_CODE_OAUTH_TOKEN` are not forwarded; it cannot prove the child does not read auth
(`tests/test_claude_backend.py:106-135`).

State the boundary precisely: **PCA does not inspect, copy, persist, refresh, print, or forward token
environment values; the trusted official child receives HOME specifically so it can own/read its
authentication.** Also state that safe mode/tool-free/temp-cwd controls do not form an OS sandbox.

### Medium — evidence durability: Groq 5/5 is currently verifiable but not durably hash-bound

The 5/5 Groq claim is real at this snapshot. The review found all five raw temporary attempt roots
and independently confirmed:

- each `result.json` is `completed` / `verified`, changes only
  `src/tiny_python_bug/calculator.py`, and has successful verification;
- each JSONL event sequence is contiguous;
- all five patches have SHA-256
  `aabb0491ca737152246996e0d9c1139acfc8fde2083b1c5aa3583f460124e35c`;
- each attempt's original source Git worktree is clean;
- the summary's run IDs, tools, usage, checks, 5/5 threshold, durations, endpoint, and model agree
  with the raw bundles;
- a generic pattern scan over retained/raw artifacts found no Bearer header, `gsk_` token,
  `OPENAI_API_KEY`, `OLLAMA_API_KEY`, or `CLAUDE_CODE_OAUTH_TOKEN` string.

However, `.research/evidence/m4/remote-groq/` retains only `summary.json`. Its records point to
`/private/tmp/pca-m4-groq-release.hNFU3y/...`; no per-attempt result/event/patch hashes are retained.
`.research/m4-live-evidence.md:34-38` records the summary and common patch hashes but not the five
result/event hashes. Once `/private/tmp` is cleared, the summary becomes self-attesting and the
otherwise accurate stage claim that durable evidence and hashes live in the repository
(`docs/stages/m4-provider-completion.md:48`) becomes weaker than M3's precedent.

Before release, retain compact per-attempt hashes for result, events, patch, and the summary, plus
the source-isolation check outcome and non-secret scan method/result. Raw transcripts need not be
committed. The claim that the *exact secret bytes* had zero matches cannot be independently rerun
without the secret; label it as an operator-performed scan and separately retain the reproducible
generic-pattern scan.

### Low — Codex callback timeout does not include browser-launch time

The listener is correctly bound before `webbrowser.open()`, and invalid first callbacks no longer
consume the attempt. But the callback deadline is created only after `on_listening()` returns
(`src/coding_agent/oauth_login.py:73-84`). If the browser-launch callback blocks, `--timeout` does
not bound that portion of login. This is a local availability/UX issue, not a secret exposure.
Start the deadline before invoking the readiness callback, or document that the option bounds only
post-launch callback waiting.

### Low — public `run` help still misstates the credential source

`pca run --help` says provider credentials are read only from environment variables because of the
docstring at `src/coding_agent/cli.py:542`. Codex deliberately reads the PCA-owned credential store.
The README provider table is correct. Change the CLI text to “environment variables or the
PCA-owned Codex store” so the daily command does not contradict its auth design.

### Low — Claude failures are safe but not actionable

Raw stderr is correctly excluded from the serialized result, but nonzero/auth/rate-limit/unsupported
flag failures collapse to `external_agent_error`, often with a blank summary
(`src/coding_agent/claude_backend.py:269-303`). Preserve redaction while mapping an allowlist of
known safe terminal categories. Do not expose raw provider stderr.

## Confirmed implementation boundaries

### Codex auth lifecycle and secrets

- Normal login binds `127.0.0.1:1455` before launching the browser. State uses constant-time
  comparison; invalid callbacks receive 400 and the listener continues until a valid callback or
  the bounded wait expires.
- `--manual` prints only the authorization URL, accepts the full loopback callback with hidden
  input, validates host/path/state/code, and never echoes the callback code.
- OAuth client closure occurs both before exchange failure and in the exchange helper's `finally`.
- `status-codex` reports only not-configured/configured and local valid/expired state. It does not
  print tokens or account ID. `logout-codex` validates the app store then unlinks only PCA's file.
- Store permissions, symlink rejection, atomic write, error redaction, refresh and one-401 retry
  remain covered by the M2 tests. The public borrowed client ID remains explicitly experimental.
- Metadata-only current state: PCA auth directory absent; PCA credential absent. No live Codex claim
  is made in README/stage/live evidence.

### Claude architecture and claim boundary

- `ExternalAgentBackend` is a whole-task protocol and is not a `ModelProvider`.
- CLI naming/help says delegation is local-only, experimental, and not PCA's loop.
- The public invocation has no repository argument, runs in an ephemeral empty cwd, disables tools
  and persistence, bounds input/output/event count/runtime, and normalizes only allowlisted event
  shapes. It therefore does not establish repository editing or PCA coding-loop E2E.
- README, stage documentation, and live-evidence notes correctly call the retained sentinel a
  subscription-connectivity/boundary test, not coding evidence. This wording must remain.

### Remote Ollama, API URL, and aliases

- The Ollama preset sends `OLLAMA_API_KEY` only when the resolved endpoint hostname is not an exact
  loopback host. A loopback endpoint receives `None` even if the parent environment contains the
  key. Contract tests cover both paths.
- The shared OpenAI-compatible adapter rejects remote plain HTTP, URL userinfo, query/fragment, and
  a remote endpoint without a key. Its placeholder key is synthesized only for exact loopback.
- `pca run` accepts preferred `--api-url` and compatibility alias `--base-url`; interactive and
  gateway expose `--api-url`. Tests cover the alias.
- The Groq evidence is correctly described as an `openai-compatible` remote API-key provider, not
  Ollama Cloud or a Groq-specific adapter.

### TTY, resume, source isolation, and daily install

- Retained TTY run `a66038f97f204cb3aaa96339f195c37e` has contiguous sequences 0-49,
  allow-once execute/modify approvals, a session-scoped execute grant and reuse, successful patch,
  deterministic check, final verification, and `completed`/`verified` result.
- Retained current-schema resume run `2fa14fe342ff4f81a6ad2dc22cd8ffda` has contiguous sequences
  0-54. It was interrupted at `waiting_approval`, emits `session.resumed`, durably abandons the
  undecided request, receives fresh approvals, edits/checks, and completes verified.
- The raw TTY/resume source worktrees are clean. Their repository-retained patches are identical to
  the five Groq patches.
- Global `/Users/xiaoxu/.local/bin/pca` and `coding-agent` exist; `uv tool list` reports
  `python-coding-agent v0.1.0`; current global help exposes auth/backend/resume/gateway/run. Wheel and
  sdist include the new backend, Claude backend, CLI, and OAuth listener modules.

## Release gates executed

```text
uv run ruff check .
  All checks passed!

uv run pytest
  161 passed in 17.54s

uv build
  Successfully built source distribution and wheel

git diff --check
  passed

pca --help
pca auth --help
pca backend claude-code --help
uv tool list
  all passed; global daily commands are present
```

## Minimum path to GO

1. Make Claude terminal success positive/fail-closed and add drift/error-result tests.
2. Give cancellation ownership of the Claude process group and prove prompt descendant cleanup.
3. Correct the HOME/auth wording in module/README/stage boundary text.
4. Retain per-attempt Groq event/result/patch hashes before the temporary raw bundles disappear.
5. Re-run Ruff, full pytest, build, diff-check, and this independent review.

The user may then either complete the separate PCA Codex browser grant and live tool/edit/check E2E,
or explicitly accept that item as a named external dependency. No implementation should import the
official Codex CLI credential.

---

## Re-review after blocker fixes

Date: 2026-08-21 (Asia/Taipei)

### Updated verdict

**NEEDS WORK — one narrow terminal-stream correctness blocker remains.** The cancellation,
HOME/auth documentation, evidence durability, OAuth deadline, and CLI-help findings are resolved.
The prior single-result fail-open cases are fixed. However, a stream containing more than one
terminal `result` event is still accepted, and only the last result controls success. An earlier
explicit error followed by a success is therefore reported as `completed`.

This is not related to the absent Codex grant. Codex authorization remains a correctly disclosed
external dependency and still does not block implementation release if explicitly deferred.

### Remaining Medium blocker — multiple Claude terminal results fail open

The new logic correctly requires the last result to contain `is_error is False` and exact subtype
`success` (`src/coding_agent/claude_backend.py:289-315`). It does not require
`len(result_events) == 1` and does not reject an earlier error result.

The review reproduced this current stream:

```json
{"type":"result","subtype":"error_during_execution","is_error":true,"result":"failed"}
{"type":"result","subtype":"success","is_error":false,"result":"ok"}
```

`_normalize()` returns two result events with `malformed=False`; the current terminal predicate sees
the last positive result and would classify the zero-exit command as completed. A result event is
terminal by contract, so any second result is malformed/version-drifted output and must fail closed.

Require exactly one result event before examining its fields, and add a regression test for
error-then-success (plus success-then-success if the invariant is expressed generically). This is
the remaining change needed for GO.

### Resolved: cancellation owns child cleanup

`run_bounded_command()` now accepts a thread-safe cancellation event, polls it at a 50 ms maximum
interval, terminates the process group, drains pipes, and returns code 130
(`src/coding_agent/runtime.py:187-279`). `ClaudeCodeBackend.run()` creates the event, shields the
worker task, sets cancellation on `CancelledError`, waits for shielded cleanup, and re-raises
cancellation only after the child worker has returned (`src/coding_agent/claude_backend.py:261-282`).

The regression test starts a descendant, cancels the backend, requires prompt caller cancellation,
and proves the descendant never writes its delayed marker
(`tests/test_claude_backend.py:188-222`). Three isolated reruns passed. One earlier grouped targeted
run failed because its one-second *startup-marker wait* expired before cancellation was initiated;
the subsequent three isolated runs and full suite passed. This appears to be test timing flakiness,
not a process-cleanup failure. Widen the marker startup deadline or use explicit synchronization to
avoid a future CI false negative; it is not a release correctness blocker.

### Resolved: positive single-result classification

For an ordinary one-result stream, completion now requires both `is_error is False` and subtype
`success`; missing flags and error subtypes fail as `invalid_result_event`. The new tests cover the
two originally reproduced cases. Explicit `is_error: true` remains an external-agent failure.

### Resolved: HOME/auth boundary is truthful

The module, README, stage document, and live-evidence note now distinguish the processes:

- PCA does not inspect, parse, copy, persist, refresh, print, or forward Claude credential values;
- the trusted official Claude child retains the user's `HOME` specifically to resolve the auth
  state it owns;
- the backend remains local-only, tool-free, safe-mode, ephemeral-cwd, and not an OS/filesystem
  sandbox or PCA coding-loop E2E.

This matches the implementation and retained sentinel evidence.

### Resolved: Groq evidence is durable and hash-bound

`.research/evidence/m4/remote-groq/` now retains `events.jsonl`, `result.json`, and `changes.patch`
for all five run IDs in addition to `summary.json`. The review recomputed every hash:

- all five patch hashes equal the documented common
  `aabb0491ca737152246996e0d9c1139acfc8fde2083b1c5aa3583f460124e35c`;
- all five event and result hashes exactly match the table in
  `.research/m4-live-evidence.md:41-47`;
- the summary hash remains
  `253891f6e823872640997ee6788a4648de6f2f1fd3297f0660101017b113d4a3`.

The evidence no longer depends on `/private/tmp` surviving. The prior generic secret-pattern scan
also remains clean; the exact-key scan is correctly described as an operator-run check rather than
something this review can reproduce without reading a secret.

### Resolved: Codex deadline and CLI help

The OAuth deadline is now created immediately after listener bind and before `on_listening()` opens
the browser (`src/coding_agent/oauth_login.py:73-84`), so the requested timeout includes launch
callback time. `pca run` help now says it uses configured environment **or app-owned provider
credentials** (`src/coding_agent/cli.py:542`), matching the Codex store behavior.

### Re-review gates

```text
uv run ruff check .
  All checks passed!

targeted Claude/OAuth/CLI/runtime tests
  first grouped run: one startup-marker timeout in the cancellation test
  isolated cancellation test rerun three times: 3/3 passed

uv run pytest
  164 passed in 19.20s

uv build
  Successfully built source distribution and wheel

git diff --check
  passed
```

No production, test, or documentation file was changed by this re-review. No credential value was
read and `~/.codex` was not inspected.

---

## Final narrow re-review

Date: 2026-08-21 (Asia/Taipei)

### Final verdict

**GO for the reviewed M4 implementation and evidence scope.** No release blocker remains.

The final Claude terminal invariant is now fail-closed: after timeout, truncation, exit-code, and
malformed-stream checks, `run()` requires exactly one normalized `result` event before it examines
that event's fields (`src/coding_agent/claude_backend.py:289-315`). Zero or multiple results return
`failed` / `invalid_result_count`; the one result must then have exact `is_error is False` and
subtype `success`. The previously reproduced error-then-success stream can no longer complete.

`test_claude_backend_rejects_duplicate_terminal_results` exercises the exact regression: an
explicit error result followed by a superficially successful result is rejected with
`invalid_result_count` (`tests/test_claude_backend.py:188-206`). Missing error flag and incorrect
positive subtype remain separately covered.

The cancellation regression's startup wait was widened from approximately one to three seconds
(`tests/test_claude_backend.py:209-243`), addressing the only observed CI-style false negative
without weakening its meaningful bounds: caller cancellation must still return within two seconds,
and the descendant must not create its delayed marker.

All earlier re-review conclusions remain unchanged:

- cancellation signals and joins external child cleanup;
- official Claude child HOME/auth ownership is documented truthfully;
- Groq 5/5 raw compact artifacts and hashes are durable;
- Codex callback timeout and daily CLI credential help are correct;
- remote Ollama/API alias, TTY, resume, source isolation, package install, and secret boundaries are
  supported by the reviewed code and evidence.

Codex app-owned browser authorization is still absent. It remains a clearly named user/external
dependency, not a hidden implementation blocker, and no live Codex E2E is claimed. Release/commit
may proceed only with that deferral kept explicit; the official Codex CLI credential must remain
untouched.

### Final gates

```text
uv run ruff check src/coding_agent/claude_backend.py tests/test_claude_backend.py
  All checks passed!

uv run pytest -q tests/test_claude_backend.py
  9 passed

uv run pytest
  165 passed in 19.49s

uv build
  Successfully built source distribution and wheel

git diff --check
  passed
```

This final re-review changed only `.research/m4-release-review.md`. It did not read any credential
value and did not inspect `~/.codex`.

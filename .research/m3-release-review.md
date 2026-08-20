# M3 independent release review

Date: 2026-08-21
Baseline: M2 commit `8151447`
Scope: current uncommitted M3 working diff
Verdict: **NEEDS WORK**

## Severity summary

- High: 3
- Medium: 1
- Low: 1

The real-provider 5/5 result is genuine and the core exact-edit happy path is well tested. Release is
blocked by three ways in which `replace_text` can report success while bypassing the documented
reviewability/change-surface contract.

## Findings

### [HIGH] Existing untracked files can be modified without any patch artifact

`replace_text()` requires only `target.is_file()` before replacing it. After mutation it calls
`reviewable_patch()`, but that method uses plain `git diff`; Git omits untracked files. Unlike
`apply_patch()`'s new-file path, `replace_text()` never registers intent-to-add or rejects an
untracked target.

Independent reproduction against the public classes:

```text
read_file("untracked.txt")
replace_text("untracked.txt", "old", "new")
=> replaced one exact text fragment in untracked.txt

content: new
reviewable_patch.content: ''
reviewable_patch.changed_paths: ()
```

This violates the central invariant that a successful mutation remains visible in
`changes.patch`/`changed_files`. It can also let final verification pass while the run bundle omits
the modified file.

Release requirement: fail closed unless the target is tracked, or make the existing untracked file
reviewable with a reversible intent-to-add transaction. Add an integration test asserting that no
successful modify observation can produce an empty patch for changed bytes.

### [HIGH] `new_text` can turn a text file into an accepted binary change

The original file is rejected when it contains NUL, but `new_text`/`updated` is not checked. NUL is
valid in a Python string and survives UTF-8 encoding. `git diff --check` accepts the result and
`reviewable_patch()` accepts Git's binary summary.

Independent reproduction:

```text
replace_text("a.txt", "old", "new\x00binary")
=> replaced one exact text fragment in a.txt

bytes: b'new\x00binary\n'
patch: 'Binary files a/a.txt and b/a.txt differ'
```

This bypasses the existing `apply_patch` binary refusal and contradicts the exact UTF-8 text/edit
claim. Reject NUL in `new_text` and in the final encoded payload, retain the original bytes, and add
a regression test.

### [HIGH] Cumulative changed-file and line limits are not enforced

`replace_text()` calls `reviewable_patch()` after every edit, which correctly enforces cumulative
patch bytes. However, `reviewable_patch()` does not enforce `max_changed_files` or
`max_patch_lines`; it only bounds diff bytes and validates returned paths. The per-patch validator
that enforces file/line counts belongs to `apply_patch()` and is never reached by `replace_text()`.

Independent reproduction with `max_changed_files=1`:

```text
replace_text("a.txt", "old", "new") => success
replace_text("b.txt", "old", "new") => success
reviewable_patch.changed_paths => ('a.txt', 'b.txt')
```

This directly contradicts the progress/stage claim that the exact editor reuses cumulative patch
limits. Enforce bytes, changed files, and diff lines in the common `reviewable_patch()` boundary;
the edit that crosses a cumulative limit must roll back without removing prior valid edits. Add
separate multi-call tests for file count and line count.

### [MEDIUM] Live eval counts a requested tool as the required tool

`event_tool_names()` collects only `tool.requested`, and `checks["required_tool"]` tests membership
in that list. A run can request `replace_text`, receive a failed observation, then succeed through
`apply_patch`; the eval would still claim the required M3 edit tool was used. The five retained raw
attempts do each contain a matching `tool.completed` with `ok=true`, so the historical 5/5 evidence
is not invalidated. The repeatable acceptance runner is nevertheless capable of a false positive.

Correlate `tool.requested` and successful `tool.completed` by `tool_call_id`, and require at least
one successful completion for the configured tool. Add a runner unit test or a small synthetic
event-log test covering requested-but-failed.

### [LOW] The 5/5 evidence is stored only under a temporary path

The documented summary and all five raw run bundles currently exist at
`/private/tmp/pca-m3-eval.3DJGdU/ollama-qwen3-4b`. They are detailed enough for this review, but an
OS cleanup/reboot can make the stage's artifact paths dead. Retain at least the summary plus stable
hashes or compact per-attempt result/event/patch evidence in a repository evidence directory. The
full disposable workspaces need not be committed.

## Accepted implementation and evidence

### Exact-edit transaction

The tracked UTF-8 happy path has a good narrow contract:

- path resolution uses the existing allowlist/symlink policy;
- a complete `read_file` records a content hash, unread/truncated/stale reads fail closed;
- replacement requires one exact match and refuses replace-all semantics;
- argument/result sizes and the harness timeout are bounded;
- a sibling exclusive temporary file is flushed, mode-preserved, atomically replaced, and its
  directory is fsynced;
- whitespace or cumulative-byte review failure restores original bytes/mode;
- approval classification and preview are correctly integrated as a modify effect.

Tests cover success/mode preservation, no/ambiguous match, traversal, input binary, stale/unread
content, oversized files, cumulative-byte rollback, whitespace rollback, and injected post-replace
fsync failure. The remaining High findings are missing boundary cases, not evidence that these
covered paths are broken.

### Prompt and session integration

`m3-exact-edit-v1` gives the model minimal exact-edit versus unified-diff guidance. New
`SessionManifest` values persist that version and `run.created` emits it. Legacy schema-v1 sessions
default to `m2-unversioned-patch` rather than being falsely relabelled as M3; the migration has a
regression test. Resume retains the already-persisted messages, so it does not silently replace an
in-flight system prompt.

### Live-provider evidence

The summary declares 5 successes in 5 attempts against `ollama/qwen3:4b`, threshold 4/5. I opened
all five raw bundles rather than trusting only the summary. Every attempt has:

- process exit 0, `status=completed`, `terminal_reason=verified`;
- one passing `pytest -q` verification outcome;
- only `src/tiny_python_bug/calculator.py` in `changed_files`;
- the exact subtraction-to-addition patch;
- a `read_file` before `replace_text` in the event stream;
- a matching `replace_text` completion with `ok=true`;
- prompt version `m3-exact-edit-v1` in session and `run.created`;
- source repository HEAD equal to request base SHA and an empty source status.

The recorded durations also exactly match the stage report: 138.85, 124.23, 109.90, 159.20, and
149.05 seconds. This supports the narrowly worded tiny-fixture/provider claim, not general coding
quality; the docs state that distinction honestly.

## Verification performed

```text
uv run ruff check .
All checks passed!

uv run pytest
140 passed in 15.09s

git diff --check 8151447
passed (no output)

uv run python scripts/eval_live_provider.py --help
passed
```

The stage currently says 139 tests because the legacy-prompt migration test was added during this
review; update the final gate count after the working tree stabilizes.

## Release decision

Do not close M3 yet. Fix the untracked-file audit gap, output-binary gap, and common cumulative
file/line limits, then rerun the focused tool tests and full suite. Harden the eval's required-tool
check before treating the script as the durable M3 regression gate. The historical real-provider
5/5 run does not need to be repeated if the fixes only narrow/refuse previously untested edit cases;
its existing artifacts already satisfy the stricter successful-tool interpretation.

---

## Remediation re-review

Re-review date: 2026-08-21
Final verdict: **GO — M3 passes independent release review**

### Finding resolution

| Finding | Result |
|---|---|
| HIGH: untracked file can escape patch artifact | Fixed |
| HIGH: NUL output accepted as binary change | Fixed |
| HIGH: cumulative file/line limits missing | Fixed |
| MEDIUM: eval counts requested rather than successful tool | Fixed |
| LOW: evidence retained only in temporary storage | Open, non-blocking durability recommendation |

`replace_text()` now checks `git ls-files --error-unmatch` before mutation, so an existing untracked
file is refused and its bytes remain unchanged. It rejects NUL in both exact fragments before any
write. The common `reviewable_patch()` boundary now enforces patch bytes, patch lines, and changed
file count; therefore both `replace_text` and final artifact collection share the same cumulative
limits.

I reran the original public-class reproductions after the fixes:

```text
untracked replace => ToolExecutionError; content remains old; patch remains empty
NUL new_text      => ToolExecutionError; tracked bytes remain b'old\n'
max_changed_files=1:
  first tracked edit succeeds
  second-file edit is refused and rolled back
  first file remains changed, second remains original, changed_paths == ('a.txt',)
```

The rollback behavior is important: a cumulative limit failure removes only the newest invalid
edit, not earlier edits that were already within policy.

The live eval now derives its required tools only from `tool.completed` events whose `ok` value is
exactly true. A requested-but-failed `replace_text` can no longer satisfy the M3 acceptance gate.
The retained historical artifacts already contain one successful `replace_text` completion in each
of all five attempts, so the stricter interpretation preserves the reported 5/5 result without
needing to rerun the provider.

The legacy prompt migration also remains correct: schema-v1 manifests without a prompt field are
marked `m2-unversioned-patch`, while newly initialized sessions receive `m3-exact-edit-v1`.

### Final verification

```text
uv run ruff check .
All checks passed!

uv run pytest
143 passed in 17.99s

git diff --check 8151447
passed (no output)

uv run python scripts/eval_live_provider.py --help
passed
```

No release blocker remains in the reviewed M3 scope. The only remaining recommendation is to copy
a compact summary plus stable per-attempt hashes/evidence out of `/private/tmp` before that directory
is cleaned; this improves long-term auditability but does not weaken the currently verified,
narrowly scoped 5/5 claim or the repeatable manifest/runner committed by M3.

---

## Final Ollama turn-bound delta review

Delta review date: 2026-08-21
Verdict: **GO — the final delta preserves the M3 release decision**

The Ollama preset changes only its finite per-turn output bound from 1024 to 4096 tokens. The
provider remains configured with `think=false` and `/no_think`; the public eval still fixes the
task-level budget at 8 agent steps and 300 seconds. This addresses the observed case where Qwen
consumed the smaller generation allowance before emitting a useful tool call without weakening the
harness step, wall-time, approval, tool, or verification boundaries.

`test_ollama_preset_retains_a_bounded_tool_turn_budget` intercepts construction of the public Ollama
preset and asserts all three compatibility values, including `max_tokens=4096`. The change does not
affect OpenAI-compatible, native-provider, or Codex adapter defaults.

The final current-code live run is independently consistent with the updated documentation:

- 5/5 attempts passed the predeclared 4/5 threshold;
- durations are 152.59, 167.46, 174.32, 227.52, and 158.54 seconds;
- every attempt reports the successful sequence
  `list_files, read_file, replace_text, run_check`;
- every attempt retains exact changed-file/patch, verified completion, and unchanged source checks;
- the summary SHA-256, all five events hashes, all five result hashes, and the common patch hash in
  `.research/m3-live-eval-evidence.md` match the raw bundles under the recorded temporary root.

The compact evidence file resolves the previous non-blocking durability recommendation: future
readers retain configuration, outcome, run IDs, durations, token usage, and integrity hashes even
after generated workspaces are removed. It correctly states that hashes do not replace raw model
transcripts and that the result remains scoped to one fixture/model/machine.

### Delta verification

```text
uv run ruff check .
All checks passed!

uv run pytest tests/test_cli.py tests/test_models.py tests/test_loop_e2e.py -q
passed

git diff --check 8151447
passed (no output)

shasum -a 256 final summary/events/result/patch artifacts
all values matched .research/m3-live-eval-evidence.md
```

No new release blocker or material documentation overclaim was found.

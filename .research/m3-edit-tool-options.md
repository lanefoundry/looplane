# M3 edit-tool reliability options

Date: 2026-08-21 (Asia/Taipei)
Scope: read-only research; no production or test file was changed by this audit. The worktree was
active while this was written, so the implementation notes below distinguish the committed M2
baseline from an in-progress `replace_text` prototype owned by another worker.

## Decision

For the observed `qwen3:4b` failure, add one **existing-file, exact, unique replacement** tool:

```text
replace_text(path, old_text, new_text)
```

Keep `apply_patch` for new files, deletions, and edits that genuinely need a multi-file or multi-hunk
patch. Do not replace `git diff` as the review artifact, the modify approval, `SafePathPolicy`, or the
cumulative patch budget.

The M3 version should deliberately have no fuzzy matching, regex, `replace_all`, arbitrary
occurrence index, file creation, deletion, rename, or whole-file overwrite. Those all expand the
effect surface and none is needed to fix the current failure. If later evals prove bulk replacement
is important, add it as a separate, explicitly approved capability rather than quietly changing the
meaning of this tool.

This is a harness fix, not a claim that `qwen3:4b` became a stronger coder. It removes syntax work
that deterministic code can perform more reliably, consistent with the local QuidProQuo analysis
in `2026-08-10-model-component-harness-system.md`: keep deterministic operations in code and leave
semantic selection to the model.

## The failure is diff syntax, not task understanding

The committed M2 tool contract asks the model for a non-empty `patch` string and then requires a
valid unified diff. The harness correctly applies path, byte, line, file-count, forbidden-operation,
`git apply --check`, rollback, and final-review limits. The weak point is before those guards: the
model must author file headers, hunk line numbers/counts, context markers, and EOF newline semantics.

Two real local Ollama traces show the distinction clearly:

1. `/tmp/pca-ollama-runs-4b/bfd536fabfed481a81d287999b8b3143/checkpoint.json`
   contains the semantically correct change `return left - right` to `return left + right`, but the
   patch ended immediately after the added line. `git apply --check` rejected it as `corrupt patch
   at line 5`.
2. `/tmp/pca-ollama-final-e2e/2ee44d01831e42eabea700b9c936e35d/checkpoint.json`
   contains the same correct change, but declares `@@ -3,3 +3,3 @@` while providing only one old and
   one new line. It was rejected as `corrupt patch at line 6`.

The existing guard did exactly the right thing: neither malformed patch changed the disposable
workspace. Prompting the model harder to count hunk rows would preserve the unnecessary failure
surface. Exact replacement lets the model provide only the information it already got right.

### Current `apply_patch` failure-surface inventory

| Layer | Failure | Present handling | Interpretation |
|---|---|---|---|
| Provider arguments | tool arguments are not valid JSON/string | adapter rejects malformed arguments | provider/model compatibility issue; not an edit algorithm issue |
| Tool language | model emits shell prose or a replacement snippet instead of a diff | `_validate_unified_diff` requires paired headers and at least one hunk | observed with `qwen3:0.6b`; safe rejection is intentional |
| Diff bookkeeping | wrong hunk counts or missing EOF-newline representation | `git apply --check` rejects as corrupt | observed twice with `qwen3:4b`; primary reliability target for M3 |
| Context | syntactically valid hunk uses stale/wrong context | `git apply --check` rejects | safe optimistic-concurrency behavior; re-read and retry is correct |
| Whitespace | patch introduces whitespace errors | `--whitespace=error-all` rejects | policy choice; should not be bypassed by the new tool's generated diff |
| Path/operation | absolute/traversal/disallowed path, binary, symlink, rename, or copy | `SafePathPolicy` and forbidden markers reject | security boundary; rejection is success, not a reliability defect |
| Per-call bounds | too many bytes/lines/files | Python limits reject before apply | resource boundary; retain unchanged |
| Cumulative bounds | individually valid edit makes final review artifact too large | post-apply `reviewable_patch` check rolls back current patch | final artifact boundary; reuse for every write tool |
| Recovery | apply succeeds but intent-to-add/final review fails | reverse patch/reset rollback | transaction boundary; exact replace needs equivalent rollback |
| Loop recovery | rejected patch is followed by long reasoning/output truncation instead of a corrected action | loop remains safe but task does not complete | error feedback and simpler tool choice are both needed |

Only the tool-language/bookkeeping rows are addressed by `replace_text`. The rest are guards to
preserve and test. Automatically “repairing” a malformed unified diff would be riskier: inferred
hunk counts/context could change the model's intended target, and approval was granted for different
bytes than the repaired patch.

## Local reference comparison

No webpage content was fetched. OpenCode and OMP were acquired through shallow official Git clones;
Pi was read from the locally installed package; the requested Claude source checkout was inspected
but is explicitly treated as unofficial/reverse-engineered evidence.

| Reference | Snapshot inspected | Edit contract | Useful lesson | Deliberately not copied |
|---|---|---|---|---|
| Pi | installed `@mariozechner/pi-coding-agent` 0.70.6 | `edit(path, edits[{oldText,newText}])`; exact/unique targeted replacements; generates its own diff | The model selects content, the harness generates diff syntax; prompt says smallest unique context | Batched edits and whitespace-normalizing/fuzzy fallback add complexity |
| OpenCode | `5e75e5e9901f0d178f425bfb47f1bd46cbe78a59`, package 1.18.19 | `edit(filePath, oldString, newString, replaceAll?)` plus a separate file-oriented `apply_patch` envelope | Exact replacement and structured patch are separate capabilities; permission preview is generated from computed diff | Its replacer chain tries trimmed, whitespace, indentation, escape, and context fallbacks; this can edit text other than the literal model supplied |
| OMP | `72000acfeb902e21816252699482887f34d1a5a4`, package 17.4.0 | model-selectable replace, JSON patch, Codex envelope, and hashline modes; replace is `path, old_string, new_string, replace_all?` | Different models benefit from different edit languages; the simple replace mode is appropriate here | Fuzzy thresholds, hashline grammar, LSP/formatting, and multi-mode routing are not a minimal M3 |
| Claude source | local checkout `83b3ecd74976fc9732c2455bd44bbcf0744b00ec`, claims 2.1.88 | `file_path, old_string, new_string, replace_all?`; validates uniqueness and read freshness | Read-before-edit/stale-read checks are deterministic guards, not prompt prose | Checkout is not authoritative and is older than the installed Claude CLI; absolute-path and product-specific policies do not fit PCA |

Concrete source locations:

- Pi schema and guidance:
  `/opt/homebrew/lib/node_modules/@mariozechner/pi-coding-agent/dist/core/tools/edit.js`
  (schema near lines 11-22, description/guidelines near 161-174, execution near 205-247).
- OpenCode exact edit:
  `/tmp/m3-opencode-source/packages/opencode/src/tool/edit.ts` (schema near 47-56; replacement
  chain near 682-736). Its alternative envelope is documented in `src/tool/apply_patch.txt`.
- OMP replace schema and execution:
  `/tmp/m3-omp-source/packages/coding-agent/src/edit/modes/replace.ts` near 1062-1129; model prompt
  in `src/prompts/tools/replace.md`. OMP keeps `allowFuzzy` as a setting, which means exact-only is
  a supported design choice rather than a missing feature.
- Claude-shaped schema and guards:
  `/Users/xiaoxu/Projects/claude-code-source/src/tools/FileEditTool/types.ts` and
  `FileEditTool.ts` near 137-315. The checkout enforces permission rules, maximum file size,
  read-before-edit, stale-read rejection, exact/unique match, and then produces a structured diff.

OpenCode/OMP's stripped-down `*** Begin Patch` envelope is a reasonable later replacement for raw
unified diff because it removes numeric hunk counts. It is not the smallest answer to this failure:
the model must still learn an envelope grammar, prefix every body line correctly, and select an
operation. Hashline editing is even more capable but couples writes to line/hash output from the
read tool and requires more session state.

## Recommended M3 wire schema

Use the project's workspace-relative naming and strict schema conventions:

```json
{
  "name": "replace_text",
  "description": "Replace exactly one occurrence in one existing UTF-8 file. Copy old_text exactly from the latest read_file result. Use apply_patch for create/delete or complex edits.",
  "input_schema": {
    "type": "object",
    "properties": {
      "path": {
        "type": "string",
        "minLength": 1,
        "description": "Workspace-relative path to an existing allowed file."
      },
      "old_text": {
        "type": "string",
        "minLength": 1,
        "description": "Exact text that must occur exactly once."
      },
      "new_text": {
        "type": "string",
        "description": "Literal replacement text; may be empty for a deletion inside the file."
      }
    },
    "required": ["path", "old_text", "new_text"],
    "additionalProperties": false
  }
}
```

Why exactly once:

- zero matches means the read is stale, the copied text is wrong, or the intended change is already
  present; mutation must not occur;
- more than one match means the model did not identify a unique location; it must add a few context
  lines and retry;
- a second identical call naturally sees zero matches, making accidental replay idempotent by
  rejection;
- it avoids giving a small model another numeric control field to misunderstand.

An in-progress worktree prototype currently exposes
`expected_replacements: integer = 1`. Its default is safe and its exact count is much safer than an
unbounded `replace_all`, but values greater than one are not required for the qwen fixture and widen
the blast radius. Recommendation: remove that field for M3; revisit only with a concrete bulk-rename
eval and an approval preview that makes every affected span visible.

### Review notes on the in-progress prototype

These are not findings against committed M2; they are implementation notes for the concurrent M3
work visible during this audit:

- `target.read_bytes()` reads the entire file before checking `max_read_bytes`. Use a bounded
  `max_read_bytes + 1` read, as `read_file` already does, so the resource limit is real.
- The first `_atomic_replace_file(target, updated, mode)` call is outside the rollback `try`. Inside
  that helper, `os.replace()` occurs before directory `fsync`. If directory open/fsync/close raises
  after `os.replace`, the tool can report failure while leaving the updated target on disk. The
  transaction must know whether replacement occurred and restore original bytes/mode on every
  post-mutation exception, including durability failures.
- `reviewable_patch()` bounds the final artifact but does not run the whitespace policy. Add the
  bounded `git diff --check` rollback described below.
- The description says “Read the file first,” but no read-version state is checked. Either describe
  this truthfully as guidance plus exact compare-and-swap, or implement durable read state; do not
  present prompt text as an enforced guard.
- Structured old/new approval is sufficient to preserve the existing approval gate. A computed diff
  preview would improve informed review but can be staged after the mutation transaction is correct.

## Deterministic guards and transaction

All checks must be Python enforcement. Prompt statements are guidance only.

1. Validate strict runtime types even if a provider claims JSON-schema enforcement. Reject booleans
   as integers, unknown fields, empty `path`/`old_text`, NULs, and `old_text == new_text`.
2. Bound `old_text + new_text` by `max_patch_bytes` before filesystem work. This prevents a tool call
   from bypassing the patch budget through arguments.
3. Resolve `path` through the existing `SafePathPolicy`. Therefore absolute paths, `..`, backslashes,
   `.git`, disallowed globs, and symlink escapes remain rejected. Do not accept the absolute-path
   contracts used by other agents.
4. Require an existing regular file. Do not create parent directories, create a file, follow an
   outside-workspace symlink, edit a directory, or interpret a special device.
5. Read at most the existing `max_read_bytes + 1`; reject oversized, NUL-containing, or non-UTF-8
   input. Preserve original bytes outside the replacement and preserve the executable mode.
6. Count literal, case-sensitive, Unicode-codepoint-exact occurrences. Require exactly one. Do not
   trim, normalize Unicode, use regex, or fuzzy-match whitespace. Returning `observed=0` versus
   `observed=N` is useful bounded feedback.
7. Build the result in memory and enforce the resulting-file byte limit before mutation. Also
   enforce the shared deadline.
8. Classify `replace_text` as `ToolEffect.MODIFY`. The existing approval must occur before any write;
   denial/cancel leaves the file byte-identical. At minimum preview `path`, `old_text`, and
   `new_text`. A later improvement can expose a pure preflight method so the CLI approval shows the
   generated diff, as OpenCode does.
9. Write atomically in the same directory without shell execution, preserving mode. On any failure
   after mutation, restore the exact original bytes and mode. A cancellation or timeout must never
   leave the temporary file or a partial target.
10. Run the Git equivalent of the existing whitespace policy on the resulting diff (for example,
    bounded `git diff --check -- <path>`). `reviewable_patch()` alone does not reject whitespace
    errors. Roll back if this check fails; otherwise `replace_text` would silently weaken
    `apply_patch --whitespace=error-all`.
11. Call the existing `reviewable_patch()` after writing. This retains cumulative changed-file and
    `max_patch_bytes` enforcement and makes `changes.patch` remain the single Git review artifact.
    Roll back this replacement if the cumulative patch is not reviewable.
12. Persist the normal tool start/completion/failure events and approval history; do not introduce a
    second state format. Resume behavior should reconcile an interrupted approval exactly as for
    `apply_patch`.

The exact `old_text` is already a compare-and-swap token for the affected region. A stronger
read-before-edit invariant would require durable read-version state across resume, not merely the
sentence “Read first.” Do not claim that invariant until it is implemented and tested. In the
current disposable, serialized workspace, exact matching is the smaller truthful guard; a future
stage can persist a per-path content digest from `read_file` if concurrent writers are introduced.

## Required tests

### Schema and dispatch

- tool definition has exactly `path`, `old_text`, `new_text`; all three required; no extra fields;
- unknown tool arguments and harness-owned `timeout_seconds` fail visibly;
- `replace_text` maps to `MODIFY`, and the interactive preview includes the proposed before/after;
- provider serialization round-trips newlines, quotes, backslashes, and non-ASCII text.

### Exact behavior

- one unique replacement succeeds and `git_diff` contains the expected normal unified diff;
- empty `new_text` deletes only the matched fragment;
- empty `old_text`, identical old/new, zero matches, and multiple matches fail without any change;
- a second identical call fails with zero matches and does not duplicate content;
- increasing `old_text` with surrounding lines disambiguates a repeated short fragment;
- stale `old_text` after an earlier edit fails and does not undo the earlier edit;
- CRLF, UTF-8 BOM policy, non-ASCII, final-newline, no-final-newline, executable mode, and empty-file
  edge cases are explicit. If BOM is unsupported in M3, reject it rather than silently strip it.

### Existing security/budget invariants

- reject absolute, traversal, backslash, `.git`, disallowed-path, and symlink-escape targets;
- reject missing file, directory, binary/NUL, invalid UTF-8, oversized input, oversized result;
- first small change succeeds, second change that exceeds cumulative patch bytes rolls back only the
  second change;
- replacement that introduces a trailing-whitespace error is rejected and rolled back, matching the
  existing `apply_patch --whitespace=error-all` contract;
- deny/cancel approval leaves source and disposable workspace byte-identical;
- injected failure in atomic replace or final diff collection restores bytes and mode and removes
  temporary files;
- timeout before write and timeout during post-write review both leave a reviewable original state;
- event argument/output truncation remains bounded and does not leak unrelated file content.

### Loop and live eval

- scripted loop: `read_file -> replace_text -> run_check -> final` completes, produces all artifacts,
  and never calls `apply_patch`;
- interrupted approval/resume presents or abandons the pending replace under the same exactly-once
  rules already used for modify tools;
- real `qwen3:4b` tiny-bug E2E: it reads `calculator.py`, requests the exact replacement, user approves,
  `pytest -q` passes, `changes.patch` has only the arithmetic fix, source repository stays unchanged;
- repeat the real fixture enough times to expose sampling variance (recommended release evidence:
  five consecutive completions at the chosen Ollama settings). Record tool-choice and edit-failure
  rate, not only final pass/fail;
- add a small golden set with: unique one-line fix, repeated line needing context, CRLF file, deletion
  within a file, a new file (must choose `apply_patch`), and a forbidden test-file edit.

## Prompt adjustment

Replace “patch-only agent” language with a compact decision rule:

```text
Read a file before editing it. For a small change to one existing file, use replace_text:
copy old_text exactly from read_file and include the smallest context that occurs once.
If it reports 0 matches, re-read; if it reports multiple matches, add context.
Never retry unchanged failed arguments. Use apply_patch only for create/delete or complex patches.
```

For small/local models, add one short tool example rather than a long unified-diff tutorial:

```json
{
  "path": "src/tiny_python_bug/calculator.py",
  "old_text": "    return left - right",
  "new_text": "    return left + right"
}
```

Keep the security clauses about untrusted repository/tool output, remote writes, credentials,
workspace paths, approvals, and final verification unchanged. Error messages should repeat the next
deterministic action (“re-read” or “add context”), but should not dump the full file or perform an
automatic fuzzy correction.

## Release recommendation

Ship `replace_text` beside, not instead of, the guarded `apply_patch`. The release gate is the real
qwen E2E plus the negative path/budget/approval tests above. Do not claim the tool enforces
read-before-edit merely because the prompt says so, and do not add fuzzy correction until a measured
failure set shows exact matching is the remaining bottleneck. The malformed-diff evidence already
shows exact replacement is the smallest intervention that addresses the actual failure.

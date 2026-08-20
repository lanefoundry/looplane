# M3: Reliable exact editing and real-provider coding eval

> Status: complete and committed.
> Date: 2026-08-21
> Baseline: M2 commit `8151447`

## Scope

Turn the M2 real-provider failure into a measured harness improvement. The local `qwen3:4b` model
had identified the correct one-line change twice but emitted malformed unified-diff bookkeeping.
M3 adds a narrow exact-text edit for this common case, retains unified diffs for structural edits,
and evaluates the public `pca run` CLI repeatedly against a real Ollama service.

## Baseline and acceptance criteria

M2 proved the provider transport, approvals, disposable workspace, and verification boundary, but
the tiny calculator task did not complete. M3 requires:

- an existing-file, unique-match edit contract with no fuzzy matching or whole-file writer;
- the same path policy, read and patch limits, approval, rollback, Git review, and deadline rules as
  the existing tool layer;
- a versioned prompt that tells the model when to use the exact edit versus unified diff;
- at least four verified completions in five independent real-provider CLI attempts;
- exact changed-file and patch assertions plus unchanged source HEAD, status, and bytes;
- full local release gates and an independent code/evidence review.

## References studied

| Reference | Boundary used |
|---|---|
| Pi (`badlogic/pi-mono`) | Keep small, explicit editing tools; tool results remain canonical observations rather than provider-specific state |
| OpenCode | Distinguish exact string replacement from patch application; do not turn an edit tool into unrestricted file access |
| OMP / oh-my-pi | Treat model capability and tool protocol as separate from the local execution policy |
| Claude Code recovered source | Read before edit and reject stale edit context instead of silently applying an outdated proposal |
| QuidProQuo harness-system article | Classify this as a harness contract failure after the model found the semantic fix |
| QuidProQuo prompt-iteration guide | Change one prompt/tool boundary, version it, and evaluate the outcome repeatedly |
| QuidProQuo agent-walls article | Keep verification, isolation, and rollback outside the model's discretion |

The detailed option comparison and pinned implementation references are retained in
`.research/m3-edit-tool-options.md`. Provider availability and the E0-E8 evidence matrix are in
`.research/m3-provider-e2e-audit.md`.

## Ideas borrowed

The useful shared pattern was an exact replacement tool whose old text comes from a prior read.
This removes line-number and hunk-count arithmetic from a small edit without relaxing the harness.
The provider still sees a strict JSON schema and the agent core still consumes canonical tool
calls and observations.

## Adjustments made for this project

`replace_text(path, old_text, new_text)` is deliberately narrower than many reference tools:

- the target must already exist, be Git-tracked, be allowed, be regular UTF-8 text, and fit the
  read bound; new files remain an `apply_patch` operation so their diff is reviewable;
- `read_file` must have returned the complete current bytes, whose hash must still match;
- `old_text` must occur exactly once and bulk replacement is refused;
- arguments and resulting content are bounded and cannot introduce NUL/binary output; the original
  mode is preserved;
- the write uses a sibling temporary file, fsync, atomic replace, and directory fsync;
- `git diff --check` and cumulative byte, line, and changed-file patch limits run after the edit;
- every post-mutation failure restores the original bytes and mode.

`apply_patch` remains available for new files, deletions, and multi-hunk changes. There is no fuzzy
match, regex edit, replace-all, or unrestricted `write_file` fallback.

## Ideas deliberately not adopted

- A stronger prompt alone: it cannot make malformed hunk counts deterministic.
- Fuzzy replacement: ambiguity would move policy from code into heuristics.
- Full-file writes: they enlarge the change surface and make accidental truncation easier.
- Reusing another CLI's login: provider authorization belongs to this application or an explicit
  operator-controlled endpoint.
- Claiming general coding quality from one fixture: the eval result is scoped to its manifest.

## Implementation

- `src/coding_agent/tools.py` adds the exact edit schema, read-version ledger, bounded atomic
  transaction, whitespace validation, cumulative patch validation, and rollback.
- `src/coding_agent/prompts.py` defines prompt version `m3-exact-edit-v1`; new sessions and
  `run.created` persist it.
- `src/coding_agent/approvals.py` classifies the new tool as a modify effect; the loop previews the
  path and both exact fragments before approval.
- `evals/live/tiny-python-bug.json` is the repeatable acceptance manifest.
- `scripts/eval_live_provider.py` creates a new source Git repository and run root for every
  attempt, calls `python -m coding_agent run`, and writes per-attempt logs plus `summary.json`.

## Verification evidence

Real-provider command:

```bash
uv run python scripts/eval_live_provider.py \
  --provider ollama \
  --model qwen3:4b \
  --output-dir /tmp/pca-m3-release-eval.46EMiT/ollama-qwen3-4b
```

Result: 5/5 passed; the predeclared daily-ready threshold was 4/5. The five durations were 138.85,
124.23, 109.90, 159.20, and 149.05 seconds in the initial run. Independent review then added
tracked-file, NUL, cumulative line/file-limit, and successful-tool evidence guards. The final
post-fix run also passed 5/5 in 152.59, 167.46, 174.32, 227.52, and 158.54 seconds. Each result was
`completed` with terminal reason
`verified`, used `replace_text`, changed only `src/tiny_python_bug/calculator.py`, contained the
expected subtraction-to-addition patch, passed `pytest -q`, and preserved source HEAD, clean Git
status, and source bytes.

The initial 1024-token Ollama turn bound could be consumed by Qwen's hidden reasoning before a tool
call, making one post-fix trial hit `max_steps_exceeded`. The preset now retains a finite 4096-token
turn bound plus the existing 8-step and 300-second task bounds. The final 5-run release eval had no
truncated turns, though local 4B generation remained slow and variable. M3 does not interpret the
5/5 result as broad 4B-model reliability.

Local pre-review gate:

```bash
uv run ruff check .
uv run pytest
git diff --check
uv run pca --help
uv run pca run --help
uv run python scripts/eval_live_provider.py --help
```

- Ruff: all checks passed.
- Pytest: 144 passed in 16.49 seconds.
- Diff check and every help surface passed.
- Package build produced both the source distribution and wheel.
- Independent review reproduced and closed untracked-file, NUL-output, cumulative structural-limit,
  and eval false-positive findings, then verified the 4096-token Ollama delta and final hashes; its
  final verdict is GO in `.research/m3-release-review.md`.
- The exact staged snapshot was exported to `/private/tmp/pca-m3-stage.FJfcZV`; from its own fresh
  `.venv`, Ruff passed, all 144 tests passed in 17.53 seconds, and both distributions built.

## Known limitations

- The local verification runtime executes trusted repository code on the host; it is not an OS or
  network sandbox.
- Exact replacement intentionally cannot create, delete, rename, or bulk-edit files.
- The live eval covers one small Python task on one local model; larger and multi-file evals remain
  future work.
- No app-owned Codex credential existed during the metadata-only check, so no Codex subscription
  E2E is claimed. Claude subscription reuse remains deliberately unsupported; API keys or approved
  endpoints use the provider-neutral adapter boundary.
- Cloudflare Worker/Sandbox deployment remains the next separate milestone.

## Artifact paths

- Manifest: `evals/live/tiny-python-bug.json`
- Eval runner: `scripts/eval_live_provider.py`
- Durable compact evidence and hashes: `.research/m3-live-eval-evidence.md`
- Live summary: `/private/tmp/pca-m3-release-eval.46EMiT/ollama-qwen3-4b/summary.json`
- Per-attempt stdout, stderr, source, run workspace, events, patch, verification, and result:
  `/private/tmp/pca-m3-release-eval.46EMiT/ollama-qwen3-4b/attempt-01` through `attempt-05`
- Editing research: `.research/m3-edit-tool-options.md`
- Provider audit: `.research/m3-provider-e2e-audit.md`

## Commit

- Implementation commit: `6bb4b5a` (`feat(edit): 建立可驗證的精準編輯與真實模型評測`).
- The implementation commit is the exact snapshot independently exported and verified above.
- QuidProQuo practice article is drafted at
  `quidproquo/src/content/posts/ai/2026-08-21-python-coding-agent-exact-edit-real-ollama-eval.md`
  and remains uncommitted for user review.

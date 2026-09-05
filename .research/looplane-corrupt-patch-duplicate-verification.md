# Looplane malformed patch and duplicate verification diagnosis

Date: 2026-09-05

## Incident evidence

- Run: `/Users/xiaoxu/.local/state/python-coding-agent/runs/bb3fc704f7154864813dfce0692de74d`
- The first model-authored new-file diff failed because physical patch line 251 was a Markdown blockquote without the required `+` prefix.
- The retry declared `@@ -0,0 +1,260 @@` but supplied about 338 additions. Git accepted only the declared 260-line hunk and ignored the trailing patch body. The tool reported success, while sections 9-12 and the conclusion were not written.
- The model manually ran `check-1` (`git diff --check`) successfully, then the harness requested the same command again for final verification.
- The continuation had a 900-second wall-time budget. The first modify approval waited about 12m29s. Final-verification approval was requested with about six seconds remaining, waited about 35 seconds, then emitted `verification.started` and failed immediately with `wall_time_exceeded`.

## Looplane causes

- `src/looplane/tools.py#ToolExecutor._validate_unified_diff` does not validate hunk old/new counts, legal body prefixes, or complete input consumption before `git apply`.
- `src/looplane/prompts.py#render_task_context` and the `run_check` tool description tell the model to run checks after edits, while `src/looplane/loop.py#AgentRunner._verify_all` always runs the configured checks again at finalization.
- `src/looplane/loop.py#AgentRunner._approval` does not pause or separately budget human approval time; the task deadline continues to elapse.
- The top-level `TimeoutError` handler reports "Model request exceeded" even when timeout happens in final verification.
- `src/looplane/dialect.py#encode_inband_history` renders failed in-band observations from empty `content` instead of populated `error`.
- TUI uses a generic `Update files` title for apply-patch and renders its plain failure as diff detail; successful `run_check` exposes raw JSON.

## Reference comparison

- Pi and Claude use structured exact-content editing rather than asking models to author raw unified diffs.
- Codex and current OpenCode use strict apply-patch parsers and return parse failures as nonfatal tool results for the next model turn.
- Oh My Pi strictly parses its Codex-style patch envelope; streaming leniency is preview-only.
- Most references have no mandatory host final-verification phase. Oh My Pi orchestrate mode and Claude's verification-agent mode intentionally rerun checks for independent final assurance.

## Recommended implementation order

1. Strictly parse unified diff hunks and reject illegal prefixes, count mismatches, and trailing/unconsumed body before approval or mutation.
2. Add structured `create_file(path, content)` so long new files do not depend on model-authored hunk counts.
3. Decide explicit semantics for manual `run_check`: either reuse it only when command and workspace fingerprint match, or remove the prompt instruction to run final checks manually and reserve final verification for the harness.
4. Exclude human approval wait from active wall time, or give approval a separate deadline; fix phase-specific timeout messages.
5. Preserve `ToolObservation.error` for in-band providers and simplify TUI result rendering.

## Minimum regression tests

- Reject `@@ -0,0 +1,1 @@\n+first\n+second\n` without creating a target.
- Reject new-file body lines missing `+` without mutation.
- Preserve a failed tool's `error` in in-band history.
- Prove malformed patch feedback reaches the next model turn and corrected input can recover.
- Prove approval wait does not consume execution budget.
- Prove manual check plus unchanged workspace does not cause an accidental duplicate approval, according to the chosen policy.

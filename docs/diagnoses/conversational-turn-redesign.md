# Conversational turn redesign — greetings must not trigger the coding pipeline

Date: 2026-08-25
Scope: native looplane-agent path (`src/looplane/loop.py`, `prompts.py`, `tui.py` approval scope).

## Problem

Sending "hi" in the TUI caused: repo exploration → diff inspection → approval prompt for
`check-1` (`git diff --check`) → verification run → only then a reply. Three causes:
no conversational-input guidance in the system prompt, unconditional final verification,
and a `run_check` session-grant scope bug (see `docs/diagnoses/approval-scope-diagnosis.md`).

## 1. Reference-design research

Evidence from `/Users/xiaoxu/Projects/coding-agent-reference` (scout-verified, file:line):

### Conversational input — handled purely in prompt text, never in code
- **codex**: `codex-rs/core/base_instructions/default.md:183` "casual conversation … respond
  in a friendly, conversational tone"; `:256` "For casual greetings … respond naturally
  without section headers"; `gpt-5.2-codex_instructions_template.md:34` "For casual
  chit-chat, just chat."
- **opencode**: `kimi.txt:7` has the sharpest conditional rule: "For simple
  questions/greetings that do not involve any information in the working directory or on
  the internet, you may simply reply directly. For anything else, default to taking action
  with tools." `copilot-gpt-5.txt:94/117`: "answer the user's question directly" / "without
  using any tools".
- **oh-my-pi**: main system prompt has no greeting text; `live-instructions.md:19` states
  greetings "MUST answer directly without delegation."
- **pi-mono**: `system-prompt.ts:123` contains no greeting guidance at all ("Be concise in
  your responses"); relies on model defaults.

### Verification threshold — no harness runs checks automatically
None of codex (`codex-rs/core/src`), opencode (`session/processor.ts`), pi-mono
(`agent-session.ts:2048`, post-run loop only does compaction/queued-message continuation),
or oh-my-pi auto-runs build/test after a turn. There is no diff-non-empty or
edit-tool-called machine check anywhere. Conditionality lives entirely in prose:
codex `prompt_with_apply_patch_instructions.md:151` gates on "If the codebase has tests or
the ability to build or run"; `gpt-5.2-codex_instructions_template.md:35` asks the model to
disclose skips. OpenCode `default.txt:74-75` conditions lint/typecheck on having "completed
a task". looplane keeps its stronger harness-owned gate (a project invariant) but makes it
conditional on actual changes — a deliberate improvement over the references, not a copy.

### Session approval scope
- **opencode** is the closest analogue: an "always" reply pushes `{permission, pattern,
  action:"allow"}` into a session-scoped ruleset (`permission/index.ts:145-149`, evaluated
  `:28-36`); bash grants are human-meaningful prefixes like `git checkout *`
  (`tool/shell.ts:412-414` + `permission/arity.ts`).
- **codex**: exact-key session cache over canonicalized argv
  (`tools/sandboxing.rs` ApprovalStore, `with_cached_approval :39-115`, keys from
  `command_canonicalization.rs:14`) plus token-prefix amendments surfaced back to the model
  as "Approved command prefixes" (`prompts/src/permissions_instructions.rs:269-272`). MCP
  tools keyed by `(server, tool)` name (`mcp_tool_call.rs:1978-1986`).
- **oh-my-pi / pi-mono**: no session-persistent grants at all.

looplane's fix follows the opencode/codex-MCP pattern: key the grant by tool name + stable
argument (`run_check:<name>`).

## 2. Changes

### Prompt guidance (`src/looplane/prompts.py`)
- `CODING_AGENT_PROMPT_VERSION`: `m3-exact-edit-v1` → `m3-exact-edit-v2`.
- Added (modeled on kimi.txt's conditional rule and codex's chit-chat wording): "A final
  answer is accepted only after the harness reruns every check that could be affected by a
  change; when the run made no change at all, skip straight to the answer. Greetings, small
  talk, and questions you can answer from the conversation alone deserve a direct text
  reply: do not call tools or touch the repository when the user has not asked for any
  change to the code."

### Conditional verification (`src/looplane/loop.py`)
- New `AgentRunner._made_changes` flag (init `False`).
- Set to `True` after any **modify-effect** tool succeeds (`effect is ToolEffect.MODIFY and
  observation.ok`) — mirrors the ticket's suggested semantics; `_collect_patch` emptiness is
  still enforced by the existing patch artifact path.
- Final answer with `_made_changes == False` → `_finish(COMPLETED,
  terminal_reason="no_changes")` without emitting any `verification.*` events.
- Resume path arms the flag conservatively (`True` with comment): an interrupted workspace
  may already contain modifications, so the gate stays on.
- `terminal_reason="no_changes"` already existed as a value in
  `external_runner.py:731`; consumers compare equality only (`contracts.py:258` free string;
  `tui.py` uses f-strings/replace; tests assert literals). No switch to break.

### run_check approval scope (`src/looplane/tui.py:_grant_scope`)
- When `request.tool_call.name == "run_check"` and arguments contain a non-blank `name`,
  return `f"run_check:{name.strip()}"[:4_096]`. Missing name falls back to previous
  behavior (`action:{tool_call_id}` via caller). "Allow for this session" now matches every
  later request for the same named check.

### Invariant update (`docs/progress.md:347-349`)
Security invariant reworded: "A model final answer **that changed files** is not success
until all declared verification commands pass. A run in which no modify-effect tool
succeeded completes without rerunning checks (`terminal_reason="no_changes"`); resumed runs
keep the gate armed conservatively."

## 3. Tests

- `tests/test_prompts.py`: version assertion bumped; new
  `test_prompt_directs_conversational_input_to_a_plain_reply`.
- `tests/test_loop_e2e.py`:
  - New `test_conversational_run_skips_verification_and_completes_without_changes`:
    read-only tool call + plain final answer → COMPLETED, `no_changes`, empty changed_files/
    verification, zero `verification.*` events despite declared checks.
  - `test_max_steps_retains_failed_verification_and_failure_artifacts` rewritten to make a
    real change first (new contract: no-change runs no longer fail verification); retains
    artifact/event coverage.
  - `test_verification_command_is_clamped_by_run_wall_time` now patches first so the slow
    check actually runs under wall-time clamp (`max_steps` 2→3 so the post-feedback loop
    reaches the TimeoutError).
  - `test_failed_final_verification_is_fed_back_then_retried` now applies `BROKEN_PATCH`
    (docstring-only change) before its first answer; the retry uses
    `FIX_PATCH_AFTER_BROKEN` whose context matches the broken state. Feedback-message
    assertion moved to `model.calls[2]`.
- `tests/test_tui.py`: new `test_run_check_allow_session_is_scoped_by_command_name` — two
  `run_check` requests with different action_ids but the same name; first ALLOW_SESSION,
  second auto-ALLOW_ONCE, single modal, grant scope `run_check:check-1`.

## Results

```
uv run pytest tests/test_prompts.py tests/test_loop_e2e.py tests/test_tui.py -q
→ 141 passed (includes previously flaky test_session_model_selector_lists_catalog_models…
  which passed this run; it belongs to another in-flight task)
uv run pytest tests/test_session.py tests/test_ask_runner.py -q → 12 passed
uv run ruff check src/ tests/ → All checks passed!
```

## Changed files (this task)

- `src/looplane/prompts.py` — version bump + conversational guidance
- `src/looplane/loop.py` — `_made_changes` tracking, conditional verification, resume arming
- `src/looplane/tui.py` — `run_check:<name>` grant scope
- `docs/progress.md` — security invariant rewording
- `tests/test_prompts.py`, `tests/test_loop_e2e.py`, `tests/test_tui.py`

Note: `git status` shows other modified files (`models.py`, `startup_cache.py`,
`slash_commands.py`, etc.) belonging to parallel in-flight work; untouched by this task.

# Development progress

> Temporary engineering name: `python-coding-agent`. Product naming is deliberately deferred.

## Goal

Build a Python-first coding agent that is usable as an interactive daily CLI while keeping a
bounded, auditable headless mode for CI and future Cloudflare execution. The agent owns its
loop, approvals, sessions, tools, and verification. Model access is provided through explicit
protocol adapters and configurable API endpoints rather than by delegating the experience to
another coding-agent CLI.

## Current milestone: M1 local harness

- [x] Define typed task, model, tool, event, checkpoint, and result contracts.
- [x] Implement JSONL events and atomic checkpoint/result persistence.
- [x] Implement disposable local Git workspace pinned to a base SHA.
- [x] Implement bounded list/read/search/apply-patch/run-check/git-diff tools.
- [x] Implement explicit loop with budgets, repeated-action guard, and verification gate.
- [x] Define a provider-neutral `ModelProvider` contract with canonical messages, tool calls,
      capabilities, usage, and classified errors; prove it with OpenAI-compatible, Anthropic,
      Gemini, Workers AI, and scripted adapters.
- [x] Add CLI and a runnable fixture evaluation.
- [x] Test path traversal, forbidden commands, patch bounds, real-worktree isolation, and artifacts.
- [x] Run `uv run pytest`, `uv run ruff check .`, and an end-to-end fixture smoke test.

## Completed milestone: M2 interactive CLI and provider bridge

- [x] Make bare `pca` start our own interactive agent in the current repository.
- [x] Stream model/tool/verification events while a turn is running.
- [x] Require explicit approval before patches and local command execution.
- [x] Persist sessions and support safe `pca resume` after process restart.
- [x] Preserve `pca run` as deterministic, non-interactive headless mode.
- [x] Support explicit provider protocols and custom API URLs, including loopback Ollama.
- [x] Validate a real provider path without changing the provider-neutral agent core.
- [x] Document how OMP, OpenCode, and Pi informed the provider boundary and where this
      implementation deliberately differs.

### Live provider evidence

- Ollama `qwen3:4b` returned real text and a correctly structured `read_file` tool call through
  `OpenAICompatibleModel` at `http://127.0.0.1:11434/v1`.
- `pca gateway` translated a real `/v1/chat/completions` request through the same adapter and
  returned `GATEWAY_OK`; shutdown now closes the provider on the serving event loop.
- A full tiny-bug agent run did not pass: the 4B model read the correct source but produced an
  invalid unified diff and later exhausted output limits. Approval, `git apply --check`, durable
  cancellation, resume, and the truncated-turn guard all behaved correctly. Do not report this as
  a successful coding eval.

### Provider boundary decision

- The Python agent loop consumes the canonical `ModelProvider` interface only.
- OpenAI-compatible, native Anthropic/Gemini/Workers AI, and future Responses adapters are
  transports behind that interface.
- A configurable API URL is an upstream endpoint, not permission to scrape credentials from
  another application's files.
- Official Codex/Claude CLIs are reference implementations and optional external tools, not the
  default UI or the owner of this agent's loop.
- Subscription OAuth support is accepted only when the provider explicitly supports third-party
  clients; otherwise use an approved API key or a user-controlled compatible endpoint.

## Active milestone: M3 reliable editing and real-provider coding eval

- [x] Add a bounded exact-text replacement tool for small edits to existing UTF-8 files.
- [x] Reuse the same path policy, cumulative patch limits, approval classification, rollback, and
      reviewable Git diff as `apply_patch`.
- [x] Keep unified diff support for multi-file/new/delete changes; do not replace it with an
      unrestricted whole-file writer.
- [x] Add fault and contract tests for ambiguous matches, missing text, traversal, binary/oversized
      files, cumulative patch overflow, atomic rollback, and provider tool schemas.
- [x] Version the coding-agent prompt and add only the minimal tool-choice guidance justified by
      the observed malformed-diff failure.
- [x] Run the tiny Python bug from a real local Ollama provider through the full headless agent loop
      and require verified completion plus unchanged source worktree.
- [x] Record a repeatable live-provider eval manifest and exact artifact paths; separate transport,
      tool-use, edit, verification, and task-completion results.
- [x] Recheck whether an app-owned Codex credential exists without reading its value. If absent,
      retain Codex live authorization as an explicit external dependency rather than claiming E2E.
- [x] Close M3 with stage documentation, a QuidProQuo draft article, independent review, isolated
      staged-snapshot verification, and complete commits.

## Explicitly deferred after M2

- Cloudflare Worker/Sandbox deployment.
- Multi-agent, MCP, RAG, long-term memory, full-screen TUI, LSP, GitHub writes, push, PR, deploy.
- Public product/package naming.

## Required artifacts per run

- `request.json`
- `events.jsonl`
- `checkpoint.json`
- `changes.patch`
- `test.log`
- `result.json`

## Milestone closure protocol

Every milestone is closed independently so later work can be compared against a fixed baseline:

1. Finish the implementation and run the milestone verification commands.
2. Write `docs/stages/<milestone>.md` with scope, references, borrowed ideas, deliberate
   deviations, implementation details, evidence, limitations, and the resulting commit SHA.
3. Draft one QuidProQuo practice article using the `post` workflow. The article remains
   uncommitted until user review.
4. Inspect the exact staged diff and create a complete commit message with Why/How sections.
5. Do not begin the next milestone until the stage report points to a verified commit.


## Security invariants

- Never edit the supplied source repository.
- Never silently execute against the host when a disposable workspace cannot be prepared.
- Tool paths stay within the copied workspace and allowed path patterns.
- Checks run only from exact argv allowlists; no shell expansion.
- Model/API secrets are not passed to repository check processes.
- A model final answer is not success until all declared verification commands pass.

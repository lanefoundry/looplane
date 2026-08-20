# Development progress

> Temporary engineering name: `python-coding-agent`. Product naming is deliberately deferred.

## Goal

Build a Python-first coding-agent MVP that copies a fixed local Git repository into a disposable workspace, lets a model use a small bounded tool set, runs deterministic checks, and returns a patch plus an auditable run bundle without changing the source worktree.

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

## Explicitly deferred

- Cloudflare Worker/Sandbox deployment.
- Multi-agent, MCP, RAG, long-term memory, TUI, LSP, GitHub writes, push, PR, deploy.
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

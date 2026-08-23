# Development progress

> Product name: Rivumi. Distribution and command: `rivumi`.

## Goal

Build a Python-first coding agent that is usable as an interactive daily CLI while keeping a
bounded, auditable headless mode for CI and future Cloudflare execution. Rivumi offers two parallel
runtime paths: its independently implemented native harness, which owns the loop, approvals,
sessions, tools, verification, and model API adapters; and explicitly selected external coding CLI
runtimes, which own their own agent loops while sharing Rivumi's conversation UI, workspace safety,
patch audit, and verification boundary. One path is never disguised as the other.

## Completed milestone: M1 local harness

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

- [x] Make bare `rivumi` start our own interactive agent in the current repository.
- [x] Stream model/tool/verification events while a turn is running.
- [x] Require explicit approval before patches and local command execution.
- [x] Persist sessions and support safe `rivumi resume` after process restart.
- [x] Preserve `rivumi run` as deterministic, non-interactive headless mode.
- [x] Support explicit provider protocols and custom API URLs, including loopback Ollama.
- [x] Validate a real provider path without changing the provider-neutral agent core.
- [x] Document how OMP, OpenCode, and Pi informed the provider boundary and where this
      implementation deliberately differs.

### Live provider evidence

- Ollama `qwen3:4b` returned real text and a correctly structured `read_file` tool call through
  `OpenAICompatibleModel` at `http://127.0.0.1:11434/v1`.
- `rivumi gateway` translated a real `/v1/chat/completions` request through the same adapter and
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

## Completed milestone: M3 reliable editing and real-provider coding eval

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

## Completed milestone: M4 subscription and provider completion

- [x] Re-audit the current interactive/headless CLI and every requested provider path from source
      and live evidence; do not treat constructor or mock coverage as provider E2E.
- [x] Recheck current Anthropic policy and official Claude Code/Agent SDK authentication boundaries
      before implementing any Claude Pro/Max bridge.
- [x] Preserve the project-owned `AgentRunner` as the default loop; distinguish a model transport
      from an optional external coding-agent backend instead of pretending both have one contract.
- [x] Complete an app-owned ChatGPT/Codex browser grant and real tool-calling E2E, or retain it as a
      named external dependency with an exact verification command if user authorization is needed.
- [x] Prove at least one configured remote API-key provider through the canonical model adapter and
      full coding loop without copying credentials into run artifacts or repository subprocesses.
- [x] Decide and implement the closest policy-supported Claude path: native API, operator-approved
      proxy, or explicitly isolated external backend. Do not scrape another CLI's credential files.
- [x] Close M4 with stage documentation, one QuidProQuo practice article, independent review,
      isolated staged-snapshot verification, and complete commits.

## Completed milestone: M5 subscription-backed external coding

- [x] Define one external coding-run contract that always operates on a pinned disposable clone,
      validates the resulting paths and cumulative patch, and runs exact final checks through Rivumi.
- [x] Add an official Codex CLI backend that uses the CLI-owned ChatGPT login without reading or
      copying its credential store, with workspace-write sandboxing and bounded JSONL output.
- [x] Add the narrowest policy-compatible Claude Code path for local/private use: explicit file
      tools only, no shell/network/MCP tools, disposable clone, and Rivumi-owned final verification.
- [x] Expose both backends as explicit experimental subscription CLI commands while keeping the
      project-owned `AgentRunner` as the default interactive and headless path.
- [x] Complete real tiny-repository coding runs for the available logged-in subscription CLIs;
      retain reviewable artifacts, verify the source worktree is unchanged, and state any external
      dependency or policy limitation without upgrading a sentinel smoke test into coding proof.
- [x] Close M5 with stage documentation, one QuidProQuo practice article, independent review,
      isolated staged-snapshot verification, and complete commits.

## Completed milestone: M6 Cloudflare Sandbox service

- [x] Keep HTTP coordination and provider credentials in a Worker; pass only a short-lived,
      run-scoped model capability into the Sandbox container.
- [x] Package the Python agent runtime in Cloudflare Sandbox and accept a bounded uploaded source
      tree instead of Git/provider credentials.
- [x] Add an authenticated Worker run endpoint plus a narrow OpenAI-compatible internal model
      proxy with request, model, lifetime, and response bounds.
- [x] Prove local contract tests, Docker build, Wrangler types/dry-run, and a disposable sandbox
      execution path; attempt remote deployment only when account/container entitlement succeeds.
- [x] Retain Cloudflare capability/evidence gaps honestly: no consumer subscription relay, no
      unbounded egress, and no production claim without a real deployed run.
- [x] Close M6 with stage documentation, one QuidProQuo practice article, independent review,
      verification, and complete commits.

## Completed milestone: M7 familiar CLI ergonomics

- [x] Accept the task as a positional prompt and default to the current Git repository:
      `rivumi "fix the failing tests"`.
- [x] Match familiar coding-agent entry points: bare interactive `rivumi`, non-interactive
      `rivumi -p`, `rivumi exec`, `rivumi resume`, and `rivumi -C/--cd`.
- [x] Preserve `rivumi run`, `--task`, and `--repo` as compatibility aliases with explicit migration
      errors for any reassigned short option.
- [x] Persist only non-secret provider/model/API URL defaults with CLI > environment > config
      precedence; never store API keys or OAuth credentials in CLI config.
- [x] Keep approval, disposable-workspace, exact-check, session, and headless safety semantics
      unchanged while shortening the command surface.
- [x] Add command-routing/config/help compatibility tests and run a real local CLI smoke test.
- [x] Close M7 with stage documentation, one QuidProQuo practice article, independent review,
      verification, and complete commits.

## Completed milestone: M8 first-run onboarding and model selection

- [x] Replace the raw `Model:` fallback with a provider-aware first-run setup when a TTY has no
      configured provider/model.
- [x] Detect a reachable loopback Ollama service and offer its installed models without executing
      repository code or reading another CLI's credentials.
- [x] Save only the selected non-secret provider/model/API URL through the existing strict `0600`
      config contract; keep API keys and OAuth grants in their existing stores/environment.
- [x] Let `rivumi config --interactive` rerun setup, and print actionable non-TTY errors that show the
      exact config or flag commands instead of prompting.
- [x] Use a natural task prompt and clearly distinguish coding-agent tasks from general chat without
      adding a full-screen TUI or changing `AgentRunner`.
- [x] Test first-run, cancellation, local discovery, configured startup, non-TTY behavior, and
      provider/model validation; verify one real bare-CLI setup path.
- [x] Close M8 with stage documentation, one QuidProQuo practice article update, independent review,
      verification, and complete commits.

## Deferred future capabilities

- Multi-agent, MCP, RAG, long-term memory, LSP, GitHub writes, push, PR, deploy.

## Completed milestone: M9 full-screen terminal application

- [x] Launch a Textual full-screen application for bare interactive `rivumi`, while preserving
      `rivumi -p`, `rivumi exec`, non-TTY automation, and an explicit `--plain` fallback.
- [x] Present first-run provider/model onboarding, repository context, task input, raw live run
      events, approval decisions, stop state, and terminal result in one coherent screen.
- [x] Connect the TUI only through provider-neutral `EventSink` and `ApprovalPolicy` seams; keep
      Textual out of `AgentRunner`, providers, tools, and durable event formats.
- [x] Add cooperative cancellation so Stop never abandons a running tool/check thread or releases
      the session writer before the current side effect has a durable completion event.
- [x] Cover the Textual app with pilot tests and retain all existing CLI/session/safety tests.
- [x] Close M9 with real-terminal smoke evidence, independent review, a stage record, one draft
      QuidProQuo practice article, and complete commits.

## Completed milestone: M10 runtime-first subscription TUI

- [x] Separate execution runtime, Rivumi provider, and model state in the full-screen application.
- [x] Offer installed Claude Code and Codex CLI runtimes without claiming their login is valid or
      copying their credentials into Rivumi.
- [x] Use each official runtime's Automatic model by default and expose bounded model choices via
      Ctrl+L; keep unknown custom API endpoints explicit.
- [x] Route subscription runtimes only through ExternalCodingRunner while retaining disposable
      clone, patch audit, source integrity, exact verification, and cooperative Stop.
- [x] Separate read-only Ask conversation from Agent coding tasks; allow Ask on dirty repositories
      without clone or modification approval, while preserving every Agent safety gate.
- [x] Keep Ask transcript bounded and process-local, reset it across runtime/model/mode changes,
      and never persist vendor session identifiers.
- [x] Make external modification approval backend-scoped and process-local; never let it grant Rivumi
      tools or another backend permission.
- [x] Synchronize the editable global command with uv.lock and verify the packaged TUI dependency.
- [x] Complete the independent review, full release gates, and stage record.
- [x] Review the in-repo stage record and create the complete M10 commit
      (`b0801b2`); the QuidProQuo blog draft is reviewed by the user in the external `quidproquo`
      repository.

## Completed milestone: M11 unified native conversation

- [x] Replace one-process-per-prompt delegation with one long-lived Codex app-server or Claude
      Agent SDK session and one disposable workspace across turns.
- [x] Remove the visible Ask/Agent split; answer, inspect, edit, execute, and approve at typed tool
      boundaries inside one conversation.
- [x] Project a Claude Code-style semantic transcript: prompt rows, streaming assistant text,
      correlated tool rows, subordinate diffs/output, and bottom-docked approval.
- [x] Keep Rivumi-owned durable conversation history separate from agent-run artifacts and never
      persist vendor session/thread identifiers or credentials.
- [x] Fail closed on unknown protocol frames, stale or cross-thread correlation, duplicate or
      unfinished tools, workspace/source-integrity drift, and bounded cancellation timeout.
      Observational notifications (token usage, turn diff, warnings) degrade to log-and-skip instead
      of ending the conversation, so a single protocol drift does not poison later turns; the failing
      frame still records the native id and recent stderr for post-hoc diagnosis.
- [x] Verify 352 tests, Ruff, lock, diff, build, editable CLI refresh, and a real installed Codex
      app-server `PCA_SMOKE_OK` turn (historical pre-rename marker).
- [x] Review the in-repo stage record and create the complete M11 commit
      (`b0801b2`); the QuidProQuo blog draft is reviewed by the user in the external `quidproquo`
      repository.

## Active milestone: M12 measured startup performance

- [ ] Establish reproducible cold/warm baselines for `rivumi --help`, bare `rivumi` to first
      editable composer, and representative headless/config commands using real user configuration
      without recording credentials or repository content.
- [ ] Add `scripts/bench_startup.sh` with hyperfine JSON output, import-time capture, environment
      metadata, warmups, repeated runs, and a paired before/after comparison workflow.
- [ ] Add opt-in `RIVUMI_STARTUP_LOG` telemetry from process entry through first editable composer;
      keep the log bounded, private, non-secret, machine-readable, and disabled by default.
- [ ] Remove eager imports of Codex OAuth/OpenAI SDK, vendor backends, conversations, gateway,
      uvicorn, and other path-specific dependencies from common CLI startup. `--help` and unrelated
      commands must not import integrations they do not use.
- [ ] Record the startup dependency graph and parallelize only independent measured work. Defer MCP
      or tool-server initialization until first use rather than adding it to the startup critical
      path.
- [ ] Add versioned, atomic, non-secret disk caching and per-process single-flight only for measured
      repeated discovery work. Cache keys must use the smallest relevant configuration/version
      inputs; failures, partial results, credentials, and workspace contents must never be cached.
- [ ] Add regression tests proving lazy imports preserve every CLI route, authentication boundary,
      TUI startup, cancellation path, and packaged entrypoint.
- [ ] Add a CI startup benchmark gate after a stable runner baseline exists: compare medians across
      repeated paired runs, publish artifacts, and fail regressions greater than 10% while avoiding
      a brittle absolute threshold across unlike machines.
- [ ] Close M12 with before/after distributions for time-to-composer and import paths, full Python
      and Cloudflare regression gates, stage documentation, independent review, and complete commits.

Detailed execution plan: `.work/m12-startup-performance-plan.md`.

## Planned milestone: M13 extensible external coding CLI runtimes

- [ ] Keep **Rivumi Agent** as the project-owned native harness: Rivumi owns its model loop,
      tools, context, approvals, sessions, compaction, verification, and provider API adapters.
- [ ] Treat every installed coding CLI as an optional sibling runtime, never as the implementation
      underneath Rivumi Agent or as a `ModelProvider` transport.
- [ ] Generalize the existing Claude Code and Codex `ConversationRuntimeSession` boundary for
      sibling external CLI adapters, covering lifecycle, turns, streaming text, tool activity,
      approvals, cancellation, resume capability, usage, and terminal errors without creating a
      second overlapping runtime abstraction.
- [ ] Add OpenCode, Pi, and OMP (Oh My Pi) adapters through their most stable structured machine
      interfaces. Prefer versioned SDK/RPC/ACP/JSONL contracts; allow bounded subprocess framing
      where necessary and do not make terminal-screen scraping the default protocol.
- [ ] Normalize external events into Rivumi-owned identifiers and typed conversation events while
      keeping vendor session IDs, tool IDs, protocol frames, and model-specific state inside each
      adapter.
- [ ] Preserve one agent loop per execution: an external CLI owns its full harness and tools;
      Rivumi supplies the conversation/workspace boundary, renders normalized events, mediates
      approvals where supported, audits the resulting patch, and performs final verification.
- [ ] Keep authentication owned by the selected CLI. Detect executable/readiness without reading,
      copying, relaying, or converting its credential store into a model API; document separately
      whether each CLI's supported login uses a subscription, extra usage, or API billing.
- [ ] Define a capability handshake so the UI can honestly expose per-runtime differences such as
      structured streaming, interactive approvals, session resume, model switching, MCP, usage
      reporting, and cancellation instead of pretending all CLIs have identical behavior.
- [ ] Add fake-CLI contract suites plus opt-in live smoke tests for Claude Code, Codex, OpenCode,
      Pi, and OMP. Fail closed on protocol drift, unknown approval/tool frames, stale correlation,
      output overflow, cancellation timeout, workspace escape, or incomplete patch accounting
      (observational frames such as token usage, turn diff, and warnings degrade rather than fail).
- [ ] Keep runtime selection explicit in onboarding and `Ctrl+L`: `Rivumi Agent`, `Claude Code`,
      `Codex CLI`, `OpenCode`, `Pi`, and `OMP`; availability means installed, not authenticated.
- [ ] Remove the current Claude/Codex-only assumptions from runtime discovery/dispatch/model
      options and the durable conversation runtime schema while preserving backward compatibility.
- [ ] Close M13 with current policy/source verification, stage documentation, independent review,
      full regression gates, and live evidence only for locally installed and authenticated CLIs.

Detailed execution plan: `.work/m13-external-coding-cli-adapters-plan.md`.

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
   For TUI layout changes, also run `tests/test_tui.py`, render the deterministic wide and narrow
   screenshots with `scripts/render_tui_screenshot.py`, and inspect the generated images. Changes
   to loading or tool feedback must include the corresponding active-state screenshot. Changes to
   the loading animation must use `--loading-frame` to capture every distinct frame and verify
   movement, constant terminal-cell width, and a stationary status label.
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

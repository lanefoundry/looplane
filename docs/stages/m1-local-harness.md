# M1: Local Python coding-agent harness

> Status: complete and committed.
> Date: 2026-08-21
> Engineering name: `python-coding-agent` (temporary)

## Scope

Build the smallest useful Python coding agent that can take a bounded task against a fixed local
Git commit, work only in a disposable clone, use a narrow tool surface, rerun deterministic checks,
and return a patch plus an auditable artifact bundle without changing the supplied repository.

Cloudflare deployment, checkpoint resume, hostile-code process isolation, GitHub writes,
multi-agent orchestration, MCP, RAG, memory, TUI, LSP, and the public product name are outside M1.

## Baseline and acceptance criteria

The fixture starts with `add(2, 3)` returning `-1`. M1 passes only when:

- a deterministic model uses the same loop and tools as a real provider;
- the disposable workspace changes while source commit, status, and bytes remain unchanged;
- `pytest -q` passes inside the disposable workspace;
- `request.json`, `events.jsonl`, `checkpoint.json`, `changes.patch`, `test.log`, and
  `result.json` exist and agree about terminal state;
- path traversal, absolute paths, symlink escape, forbidden checks, and oversized/disallowed
  patches are rejected by Python code;
- every model adapter returns the same canonical text/tool/usage/error contracts;
- Ruff, the complete test suite, `git diff --check`, and an offline end-to-end smoke pass.

## References studied

| Reference | Boundary used in M1 |
|---|---|
| Pi | Small model/core/CLI layers and a compact default tool surface |
| mini-SWE-agent | Explicit `Model / Agent / Environment` Python boundary and simple loop |
| Aider | Reviewable edit format, Git diff, and deterministic lint/test feedback |
| SWE-ReX | Agent/runtime separation so local and cloud execution can share a contract |
| Claude Code recovered source | Schema → validation → permission → execution → result pipeline; append-only sessions and headless boundary |
| OpenCode | Client/server separation and `allow / ask / deny` permission thinking |
| OpenAI Codex CLI | Sandbox capability and human approval are separate controls |
| QuidProQuo: coding-agent internals | The useful core is a small tool loop; tools execute off-LLM |
| QuidProQuo: model is a component | Deterministic checks and policy belong in code, not prompts |
| QuidProQuo: harness-layer security | Treat repository/tool output as untrusted and enforce damage bounds at tool boundaries |
| QuidProQuo provider implementation | Separate provider metadata from actually callable adapters; normalize usage and errors |

Local source map: `.work/python-coding-agent-reference-map.md` under the parent Projects workspace.

## Ideas borrowed

- **mini-SWE-agent / Pi:** keep the first loop visible and framework-free.
- **Aider:** use unified diffs and Git as the review boundary instead of whole-file model writes.
- **SWE-ReX:** inject an execution backend; do not let model code choose a host environment.
- **Claude Code:** keep provider calls, tool execution, permissions, events, and headless CLI as
  distinct modules.
- **Codex:** fail closed when the advertised model/runtime capability cannot satisfy the task.
- **QuidProQuo:** move path, command, budget, verification, and success rules out of prompts.

## Adjustments made for this project

| Reference behavior | M1 adjustment | Reason |
|---|---|---|
| Pi does not provide an inherent sandbox | M1 always clones to a disposable workspace; cloud phase will add OS/container isolation | Repository content is untrusted and the source worktree must remain untouched |
| mini-SWE-agent can expose a general environment command | Only exact named verification argv is executable; no general shell tool | A tiny fixture does not justify arbitrary host shell authority |
| Aider has broad Git UX and auto-commit flows | M1 only produces an unstaged patch and never commits/pushes the target | Human review remains the external-write boundary |
| Provider implementations often leak SDK message types | All adapters normalize into local Pydantic contracts | The loop must not change for Anthropic, Gemini, Workers AI, or OpenAI-compatible APIs |
| Models may decide they are finished | Final text triggers a deterministic rerun of every declared check | Model confidence is not verification evidence |
| `git apply --intent-to-add` helps expose new files | M1 uses plain `git apply`, then intent-to-add only for paths that were new | Applying intent-to-add to tracked files corrupted the final diff into a false new-file patch |
| A disposable clone can look like a sandbox | Local checks now require `--unsafe-local-exec`; hostile code is deferred to an OS/container backend | A clone protects the source worktree but cannot hide host files, network, or process privileges |
| Per-tool patch limits appear sufficient | Every apply rechecks the cumulative reviewable diff and rolls back the current patch on overflow | Many individually small edits can otherwise exceed the final artifact budget |
| A child-process timeout appears sufficient | Commands use bounded concurrent drains and terminate/reap the process group | Captured output could exhaust memory and descendants could survive the direct child |
| Provider metadata can be normalized away | Canonical tool calls retain opaque provider metadata while core fields remain portable | Gemini continuation IDs/signatures must survive the second tool-result turn |

## Ideas deliberately not adopted

- LangGraph or another orchestration framework: the current state machine is small enough to test
  directly.
- Multi-agent planning/editor/reviewer roles: there is no measured failure requiring them.
- MCP: six in-process tools are simpler and keep the trust boundary visible.
- General Bash: exact check commands cover the fixture while avoiding unnecessary authority.
- Automatic provider fallback: schema, tool, and policy errors must not be hidden by switching
  models. Routing belongs after provider behavior is measured.
- Cloudflare Worker/Sandbox: M1 proves the Python runtime contract first.

## Implementation

```text
TaskContract
    |
    v
AgentRunner -------------------------- events.jsonl / checkpoint.json
    |             \
    |              -> ModelProvider
    |                   |- Scripted
    |                   |- OpenAI-compatible
    |                   |- Anthropic
    |                   |- Gemini
    |                   `- Workers AI
    v
ToolExecutor -> SafePathPolicy -> LocalGitWorkspace (detached disposable clone)
    |
    |- list/read/search
    |- unified patch
    |- exact allowlisted check
    `- git diff
```

Important implementation points:

- `TaskContract`, provider messages, tool calls, usage, errors, checkpoints, and results are strict
  Pydantic v2 models.
- Provider-specific tool-call and tool-result formats are translated only inside adapters.
- `LocalGitWorkspace` requires a full commit SHA, clones without hardlinks, and uses detached HEAD.
- `SafePathPolicy` rejects absolute, Windows/backslash, parent traversal, `.git`, allowlist misses,
  and resolved symlink escapes.
- `run_check` selects a named contract entry and executes its exact argv with `shell=False`.
- Check subprocesses receive a minimal task-specific environment without model/GitHub secrets and
  without repurposing `HOME`; Python bytecode writes are disabled to prevent same-second,
  same-size source edits from reusing stale timestamp-based `.pyc` files.
- Tool output, cumulative patch bytes/lines/files, steps, repeated actions, command duration, and
  total wall time are bounded. Timeout cleanup terminates and reaps the command process group.
- Allowed path globs are segment-aware: `*` does not silently cross directories; only a complete
  `**` segment does.
- Final changed paths come from NUL-delimited Git output, so additions and deletions are preserved.
- Final verification runs even if the model already invoked the check itself.

## Independent review and corrections

The first green suite was not treated as closure. A separate execution/provider/test review found
gaps that the original fixture did not exercise:

- local verification was unsandboxed host code execution without an explicit acknowledgement;
- captured subprocess output was bounded only after completion and descendant processes could
  outlive a timeout;
- an unsafe library `run_id` could escape the run root during failure artifact creation;
- final verification and tools were not all clamped by the task deadline;
- deletion tracking, cumulative patch size, segment glob semantics, and event argument size were
  incomplete;
- provider tests parsed first responses but did not prove the second tool-result request;
- OpenAI, Gemini, and Workers AI each had a different continuation/error edge case;
- immediate equal-size Python edits could reuse stale `.pyc` during verification retry.

The fixes are now regression-tested. Provider tests still use shaped mocks, so the report keeps
protocol-contract evidence separate from live-provider evidence.

## Verification evidence

Commands:

```bash
uv sync --extra dev
uv run ruff check .
uv run pytest
uv run coding-agent --help
uv run python scripts/demo_fixture.py --run-root runs/demo-smoke
git diff --check
```

Final release-gate result:

- Ruff: all checks passed.
- Pytest: 55 passed in 8.52 seconds.
- CLI: exposes the explicit `coding-agent run` command.
- Offline smoke run: `eb638d159c184786915dba3fef0045ce`, status `completed`, terminal reason
  `verified`, changed only `src/tiny_python_bug/calculator.py`, verification exit code `0`.
- Source-isolation E2E compares source HEAD, porcelain status, and file bytes before and after.
- Five provider implementations share first- and second-turn contract tests; native HTTP errors
  map 401 → auth, 429 → rate limit, and 5xx → retryable, while Workers AI code 7505 maps to rate
  limit with provider diagnostics retained.

## Known limitations

- `LocalGitWorkspace` is workspace isolation, not a hostile-code OS sandbox. M1 must not execute
  untrusted repositories outside Docker/Cloudflare Sandbox.
- Provider tests use injected fake clients/HTTP transports; live API connectivity and individual
  model tool-calling behavior are not claimed yet.
- Tool calling defaults to disabled for every unknown real-provider model and requires an explicit
  capability assertion.
- Checkpoint data is persisted, but M1 does not implement resume or active-writer fencing.
- No artifact secret scanner or network egress policy exists yet.
- No automatic retries, provider routing, streaming, cost calculation, context compaction, or
  external writes.

## Artifact paths

- Fixture: `evals/fixtures/tiny-python-bug/`
- Tests: `tests/`
- Offline smoke: `runs/m1-release/eb638d159c184786915dba3fef0045ce/` (ignored by Git)
- Active plan: `docs/progress.md`

## Commit

- Commit: `859db23` (`feat(harness): 建立可驗證的 Python coding agent 核心`)
- The commit was verified from an isolated checkout of the staged snapshot: Ruff passed and all
  55 M1 tests passed before the root commit was created.
- The QuidProQuo practice article remains a reviewable draft outside this repository.

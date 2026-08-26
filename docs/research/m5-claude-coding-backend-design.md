# M5 design — useful local Claude Code coding backend

Date checked: 2026-08-21
Installed Claude Code: `2.1.238` (native arm64 binary)
Scope: turn the current tool-free sentinel backend into a repository-editing backend without making Claude subscription auth a `ModelProvider` or proxy. This is an engineering and product-policy design, not legal advice.

## Decision

The safest useful first version is a **local/private, explicitly acknowledged `ExternalAgentBackend` that edits an exact-SHA disposable clone with file tools only**:

1. PCA resolves the source repository to a full commit SHA and prepares the existing `--no-hardlinks` detached clone outside the source worktree.
2. The user approves one bounded delegated edit run before launch.
3. The user-installed official `claude` binary runs with its own auth and complete inner loop, but sees only `Read`, `Glob`, `Grep`, and `Edit`. It gets no Bash, network, MCP, subagent, notebook, or whole-file/new-file tool.
4. Claude automatically applies edits only inside the disposable clone. PCA never interprets Claude auth and never sends a token.
5. After Claude exits, PCA—not Claude—collects and validates the patch, requests a separate execute approval, runs the exact declared verification commands, and writes the terminal artifacts.
6. The source repository is never modified. A failed policy check, failed verification, timeout, cancellation, or malformed stream leaves only a reviewable failed run in the run root; the clone can then be discarded.

This gives Claude a real coding task without unrestricted Bash and avoids implementing a fragile per-tool approval protocol in the first useful slice.

```text
source repository @ full SHA
        |
        | PCA LocalGitWorkspace.prepare()
        v
disposable detached clone --------------------------+
        |                                            |
        | one run-level MODIFY approval              | PCA post-run ownership
        v                                            v
official claude CLI                         validate Git patch/status
Read / Glob / Grep / Edit                   allowed paths / size / type
owns auth + inner agent loop                exact verification approval
        |                                   exact argv checks
        +---------- edits clone ---------------------+
                                                    |
                                                    v
                                      artifacts + ExternalAgentResult
```

## Why the current backend cannot code

The M4 backend intentionally uses:

```text
--safe-mode
--disable-slash-commands
--tools=
--permission-mode plan
```

It has good process boundaries: bounded stdin/stdout/events, an ephemeral cwd, a controlled environment that strips token/API-key variables, stream-JSON normalization, process-group cleanup, and cancellation propagation. It proved only an exact sentinel through the installed official CLI.

It cannot inspect or edit a repository because its cwd is an empty temporary directory and all tools are removed. `plan` mode also explicitly prevents source edits. The replacement must preserve the process/auth restrictions while changing workspace and tool policy; it must not reuse `AgentRunner`'s `ModelProvider` contract.

## Current authoritative behavior

### Installed CLI 2.1.238

The installed help confirms:

- `--tools` selects the exact built-in tool set; the documented examples include `Bash`, `Edit`, and `Read`.
- `--permission-mode` supports `acceptEdits`, `auto`, `bypassPermissions`, `manual`, `dontAsk`, and `plan`.
- `acceptEdits` automatically accepts file edits and common filesystem commands in the working directory/additional directories.
- `-p` skips the workspace-trust dialog and therefore must run only in a trusted directory. A PCA-created clone of a user-authorized repository is the narrow acceptable case; it is still not hostile-code isolation.
- `--safe-mode` disables CLAUDE.md discovery, skills, plugins, hooks, MCP, agents, output styles, and other customizations while leaving auth, model selection, built-in tools, and permissions working.
- `--no-session-persistence`, stream-JSON input/output, `--strict-mcp-config`, `--disable-slash-commands`, and `--no-chrome` are available.
- `--bare` does **not** read OAuth/keychain auth. It is therefore unsuitable for this local subscription-owned CLI boundary; `--bare` is appropriate only for API-key/provider credentials.

### Official permission documentation

[Anthropic's permission documentation](https://code.claude.com/docs/en/permissions), fetched through `stealth_fetch` on 2026-08-21, states that permission rules are enforced by Claude Code rather than by prompt prose. It also establishes these relevant properties:

- read-only file tools do not ask within the working directory;
- file modifications ask in manual mode and can be allowed for the session;
- `acceptEdits` automatically accepts edits in the working directory;
- a bare deny for a tool removes it from the model's tool context;
- `Read`/`Edit` path rules do not constrain arbitrary programs launched through Bash; OS-level sandboxing is required for subprocess enforcement;
- `Read` denial also blocks `Edit` and `Write` for the same path in current versions, while `Edit` is the canonical path-rule name for edit/write tools;
- permission rules and Bash parsing are useful controls, but Bash remains a larger boundary than file tools.

That last point is why the MVP excludes Bash completely instead of trying to assemble a long command allowlist.

### Source/protocol evidence and its authority limit

The locally available official `@anthropic-ai/claude-agent-sdk` `0.1.77` implements a bidirectional stream-JSON transport. Its `canUseTool` callback receives the tool name/input and returns an allow/deny result; internally it correlates `control_request`, `control_response`, and `control_cancel_request` messages. It requires stream-JSON input.

The local `/Users/xiaoxu/Projects/claude-code-source` checkout exposes the same protocol shape and shows that headless permission requests are routed to the SDK consumer rather than an interactive CLI dialog. However, that checkout is reverse-engineered and older than the installed native binary, so it is **not normative evidence** for 2.1.238. The installed help, official docs, and official Agent SDK package control this design.

## Exact MVP invocation

Run from the prepared clone, with the task supplied as a stream-JSON user message on stdin rather than in argv:

```text
claude
--print
--safe-mode
--disable-slash-commands
--no-chrome
--strict-mcp-config
--mcp-config={}
--input-format=stream-json
--output-format=stream-json
--verbose
--no-session-persistence
--tools=Read,Glob,Grep,Edit
--disallowedTools=Bash,Write,NotebookEdit,WebFetch,WebSearch,Agent
--permission-mode=acceptEdits
```

Notes:

- `--tools=Read,Glob,Grep,Edit` is the primary capability boundary. `--disallowedTools` is defense in depth and makes the intent auditable.
- Do not add `--add-dir`, `--plugin-dir`, `--agent`, `--agents`, `--file`, `--chrome`, `--worktree`, or any remote/cloud flag.
- Do not pass `--dangerously-skip-permissions`, `--allow-dangerously-skip-permissions`, `auto`, or `bypassPermissions`.
- Do not use `--bare`, `setup-token`, an OAuth-token environment variable, a copied keychain, or another CLI's credential store. The official executable finds and owns its existing local auth through the user's normal HOME/keychain context.
- The controlled environment should retain only the minimum OS/locale/PATH/HOME keys required by the official binary and strip all names containing API, AUTH, CREDENTIAL, PASSWORD, SECRET, or TOKEN. Redirect TMP and XDG cache to the run root. Keep safe-mode's nonessential-traffic suppression.
- No model must be forced initially. If model selection is later exposed, record the non-secret requested model and returned model metadata; do not imply subscription availability from an alias.
- On startup, require the normalized `system/init` event to report no tools outside the configured set. Fail closed if the CLI version or effective tools cannot be established.

### Why `acceptEdits` is acceptable here

It is not a general permission bypass. PCA first asks the user to approve this exact statement:

```text
Allow local experimental Claude Code to edit a disposable clone of <repo>@<sha>?
Available tools: Read, Glob, Grep, Edit. No Bash/network/MCP/subagents.
Allowed patch paths: <patterns>. Source worktree will not be changed.
PCA will separately ask before running: <exact verification argv>.
```

The approval is for mutation of a disposable clone, not for publishing to the source repository. PCA validates the resulting Git diff before treating the run as reviewable. A denied approval means the CLI is never spawned.

This is safer and simpler than headless `manual` mode without an approval bridge. The current backend writes one input and closes stdin; if Edit asks, it cannot answer the correlated control request. `manual` therefore cannot be used until transport becomes bidirectional.

## Task and workspace contract

The coding backend needs a new coding-specific task rather than overloading the current two-string sentinel contract:

```python
class ExternalCodingTask(ContractModel):
    repository: Path
    base_sha: str                    # exact 40-char commit
    instruction: str
    allowed_paths: tuple[str, ...]
    verification: tuple[VerificationCommand, ...]
    limits: Limits
    task_id: str
```

Composition should reuse:

- `LocalGitWorkspace` for exact-SHA validation, `--no-hardlinks` clone, and detached checkout;
- `SafePathPolicy` for allowed relative paths, `.git` rejection, traversal rejection, and symlink escape detection;
- `run_bounded_command` for process groups, timeout, bounded pipes, and cancellation;
- the existing exact verification-command contracts and sanitized repository subprocess environment;
- the existing append-only/atomic artifact primitives.

The run root must be outside the source repository. Capture source HEAD, status, and hashes of relevant source bytes before launch and prove they are unchanged after every terminal path.

The clone must outlive Claude execution long enough for patch collection and verification. The current `TemporaryDirectory` lifetime ends before a coding patch could be inspected, so M5 should use the run-owned workspace created by `LocalGitWorkspace` instead.

## Patch ownership and validation

Claude owns only the edit decision inside its external loop. PCA owns the acceptance boundary.

After the subprocess exits:

1. Inspect Git status using exact, shell-free Git argv.
2. Permit only tracked regular-file modifications for the first MVP. Reject new, deleted, renamed, copied, binary, submodule, symlink, mode-only, `.git`, and out-of-policy changes.
3. Resolve every changed path through `SafePathPolicy` and the task's `allowed_paths`.
4. Enforce cumulative patch byte, line, and changed-file limits; run `git diff --check`.
5. Produce the no-color/no-external-diff/no-renames text patch. Do not automatically apply it to the source worktree, commit, push, or open a PR.
6. If there is no patch, return a distinct `no_changes` failure unless the task explicitly allows analysis-only completion.
7. Only after patch validation, request execute approval and run every exact verification command through PCA's sanitized environment.
8. Mark the run completed only when the external result is a single successful result event, the patch is policy-valid, every required check passes, and the source invariants still hold.

The existing `reviewable_patch()` already bounds size/lines/file count and resolves changed paths, but the external-edit path needs an additional Git-status/file-type gate because Claude mutated the clone outside `ToolExecutor.apply_patch()`/`replace_text()`.

### Verification feedback without Bash

Claude does not need Bash to produce the first useful coding E2E. PCA runs the tests after Claude exits.

If a later version needs repair iterations, use at most a small harness-owned retry count:

1. PCA runs the exact allowlisted check.
2. PCA gives a new bounded Claude invocation the original task plus redacted/bounded failure output.
3. Claude again gets file tools only and edits the same disposable clone.
4. PCA revalidates the cumulative patch before the next check.

This is not a transparent nested `ModelProvider` loop. Each Claude invocation remains an external agent run, while PCA owns retry budgets and deterministic verification. Do not give Claude `Bash(pytest *)` merely to avoid implementing this boundary.

## Approval bridge feasibility

### Recommended M5.1: run-level edit approval

Use `acceptEdits` after one explicit TTY/callback approval. It is sufficient because edits land only in a disposable clone and PCA never publishes them automatically. Keep verification as a second PCA execute approval.

### Optional M5.2: per-tool approval bridge

Technically feasible, but not required for useful coding:

- replace the one-shot bounded subprocess helper with a long-lived asynchronous process transport;
- keep stdin open after the initial user message;
- parse stream-JSON incrementally;
- correlate only `control_request{subtype=can_use_tool}` with a unique request/tool-use ID;
- normalize Edit input to a relative clone path, validate it with `SafePathPolicy`, bound the preview without persisting replacement content, then call PCA's `ApprovalPolicy`;
- return a documented allow/deny `control_response`; deny unknown tools/subtypes and expire unanswered requests;
- handle `control_cancel_request`, duplicate/late responses, output/event limits, EOF, cancellation, and process exit without deadlock.

Prefer the official Python Agent SDK's permission callback over hand-maintaining this wire protocol **when using a policy-supported API-key integration**. For a subscription-backed local bridge, adopting Agent SDK product APIs does not erase Anthropic's third-party subscription restriction. Raw control-protocol code would also be a compatibility risk because the CLI and SDK can evolve together.

Even with a bridge, keep `--tools` restrictive and post-validate the Git patch. Approval is not a replacement for deterministic path and patch guards.

## Artifacts and event normalization

Add an external coding-run bundle with the same truthfulness as PCA runs:

- `request.json`: task ID, source path, base SHA, allowed paths, limits, verification argv, backend name, `local_only=true`, `experimental=true`; never auth metadata.
- `backend.json`: Claude CLI version, exact non-secret argv/capabilities, effective tools, permission mode, start/end time, exit code, timeout/cancel reason.
- `events.jsonl`: normalized bounded lifecycle, assistant text, tool name, policy-relative file path, approval decision, and terminal result. Do not persist session IDs, raw environment, raw stderr, tool replacement content, or provider control frames.
- `changes.patch`: PCA-collected validated text patch.
- `test.log`: exact PCA-owned verification outcomes.
- `result.json`: backend identity, terminal status/reason, changed files, verification, bounded usage if reported, and artifact paths.
- source-invariant evidence: base/head/status/hash comparison without credential paths or HOME contents.

Raw Claude stdout/stderr should remain in bounded memory only long enough to normalize. If a diagnostic is necessary, store a classified error and byte/truncation counts, not raw auth/config output. All artifact files should retain the existing 0600/atomic-write behavior.

External events must be labelled `backend=claude-code`. They must never be merged into evidence claiming the PCA `AgentRunner` or a `ModelProvider` completed the task.

## Timeout and cancellation

The current runtime's process-group cleanup and new cancellation event are the right primitives:

- spawn with `shell=False` and a new POSIX session/process group;
- on timeout or `asyncio.CancelledError`, set the cancellation event, send TERM to the group, wait briefly, then KILL the group;
- always drain bounded pipes and await cleanup before returning/re-raising;
- classify user cancellation separately from timeout (`cancelled`, exit 130 vs `timed_out`, exit 124); the external contracts currently need a `CANCELLED` status;
- after cancellation, do not execute repository checks automatically; collect a bounded partial patch only for review, mark it unverified, and keep the source invariant proof;
- never delete the run workspace while a descendant may still hold it.

No Claude background agents, session resume, `--bg`, or persisted Claude session should be enabled. PCA's run state is authoritative.

## Tests required before a live run

Use fake executables for deterministic coverage:

1. **Exact argv/cwd:** fake asserts clone cwd, stream-JSON stdin, safe flags, exact four tools, `acceptEdits`, and absence of Bash/bypass/cloud/remote flags.
2. **Approval before spawn:** denial leaves no child marker; allow starts exactly once and records the approved SHA/tool/path/check summary.
3. **Successful tracked edit:** fake edits one allowed tracked UTF-8 file and emits a valid stream; PCA collects the expected patch and passes an exact fake verification command.
4. **Forbidden path:** fake edits an out-of-scope path; run fails before verification and source stays unchanged.
5. **File-type/status gates:** reject new/delete/rename, symlink, binary, submodule, mode-only, `.git`, too many files, and oversized/too-long patches.
6. **Verification ownership:** fake tries no Bash; PCA asks execute approval and runs only the declared argv. Denial/failure stays failed and unverified.
7. **Malformed stream:** invalid JSON, oversized event, duplicate/missing/non-success result, unknown control frame, and output truncation fail closed while preserving a reviewable patch classification.
8. **Secret boundary:** injected API/OAuth/token/secret canaries do not reach child env, events, errors, patch metadata, or result. HOME may reach only the official child; PCA never enumerates auth files.
9. **Timeout/cancellation:** leader and grandchild are dead, no later marker appears, partial patch is unverified, and source is unchanged.
10. **Concurrent/source mutation:** source worktree changes during the run do not enter the exact-SHA clone or output patch; source invariants report any unexpected source mutation without overwriting it.
11. **Init capability attestation:** an unexpected effective tool or missing init event fails before accepting completion.
12. **Protocol separation:** `ClaudeCodeCodingBackend` satisfies `ExternalAgentBackend` and is not accepted as `ModelProvider`; result/eval labels cannot be mistaken for `AgentRunner` evidence.

Run targeted tests, full tests, Ruff, build, and an isolated staged-snapshot verification before calling the implementation ready.

## Live E2E gate

The first real E2E should remain local/private and user-invoked:

1. Use a fresh Git-initialized tiny Python bug fixture and resolve a full base SHA.
2. Put the run root outside the fixture and record clean source HEAD/status/bytes.
3. Invoke the explicit experimental backend command from a real TTY and approve the displayed delegated-edit scope.
4. Confirm init capability attestation reports only `Read`, `Glob`, `Grep`, and `Edit`.
5. Ask Claude to fix the existing calculator bug. It must use an Edit tool; no Bash/tool execution/network/subagent event may appear.
6. PCA collects exactly one allowed text-file patch.
7. Approve PCA's exact `pytest -q` verification; require exit 0 and final `completed/verified` semantics that name the external backend.
8. Require source HEAD/status/bytes unchanged and artifacts free of environment/auth material.
9. Retain the full normalized artifact bundle and exact installed Claude version.

This proves: the official CLI can own a local edit loop in a disposable clone while PCA owns patch acceptance and verification. It does **not** prove the PCA `AgentRunner`, native Anthropic API adapter, hosted Cloudflare execution, or a distributable Claude subscription integration.

A small repeated run (for example 3/3 on this narrow fixture) may measure mechanical reliability, but it must remain a separate `external-backend` eval series and must not be combined with Groq/Ollama `ModelProvider` success rates.

## Supported today versus requiring Anthropic approval

| Path | Technical/product status on 2026-08-21 | PCA position |
| --- | --- | --- |
| PCA `AnthropicModel` with an operator's commercial `ANTHROPIC_API_KEY` | Official supported API path under Commercial Terms | Supported model transport; PCA owns `AgentRunner` |
| Organization proxy terminating authorized Anthropic API/Bedrock/Vertex/Foundry credentials | Supported only to the extent the underlying commercial relationship permits it | May be a `ModelProvider` transport; not a subscription bridge |
| User manually invokes installed official Claude Code locally; CLI owns existing auth; bounded file-only external backend edits a disposable clone | Technically available; published policy does not provide a blanket third-party product entitlement. Safest interpretation is private/local/experimental, not a product promise | Explicit opt-in only; no token handling, hosting, resale, or Cloudflare exposure |
| Third-party product offers `claude.ai` login or Claude Pro/Max rate limits | Anthropic Agent SDK documentation says prior approval is required | Do not implement or advertise without written approval |
| Hosted/multi-user/Cloudflare service relays consumer subscription credentials or exposes Claude Code capacity | Same prohibited/unsupported boundary plus credential-custody risk | Forbidden absent explicit Anthropic approval; do not proxy |
| Agent SDK embedded in a product using subscription auth | Agent SDK does not exempt the product from the third-party login/rate-limit restriction | Require Anthropic approval; API-key Agent SDK usage is the supported alternative |

The M5 local backend must carry `local_only=true`, `experimental=true`, and an explicit acknowledgement every time (or a narrowly scoped local config that cannot enable hosted mode). It should refuse execution when it detects a server/worker/non-TTY deployment context.

## Non-goals

- No Claude login UI, setup-token import, keychain scraping, OAuth refresh, token store, or subscription proxy.
- No `ModelProvider` wrapper around Claude Code or Agent SDK.
- No source-worktree editing, auto-apply, commit, push, PR, deploy, or Cloudflare execution.
- No Bash in the first coding backend; PCA owns exact verification.
- No claim that local subscription delegation is an approved distributable product integration.
- No claim that a Claude external-backend E2E validates PCA's native agent loop.

## Implementation sequence

1. Add the coding task/result fields and a run-level approval boundary while preserving the existing sentinel mode.
2. Prepare a run-owned `LocalGitWorkspace`; move Claude cwd into its clone.
3. Replace the empty tool set with the exact file-only argv above and add init capability attestation.
4. Add external Git status/path/type/patch gates and artifact persistence.
5. Add PCA-owned verification approval/execution and source invariants.
6. Add cancellation status/partial-artifact behavior.
7. Complete fake-executable tests, then one user-invoked live E2E.
8. Consider bidirectional per-Edit approvals only after the simpler boundary has evidence; use official SDK APIs where policy/auth fit.

## Bottom line

Claude Code can become useful without Bash and without turning a subscription into a model API. Let the official CLI edit existing files in an exact-SHA disposable clone with `Read/Glob/Grep/Edit`; approve that bounded clone mutation once; then let PCA enforce path/patch limits and run exact verification separately. This preserves the truthful architecture: Claude owns an external agent loop and its auth, while PCA owns workspace isolation, acceptance, evidence, and the decision to trust the patch.

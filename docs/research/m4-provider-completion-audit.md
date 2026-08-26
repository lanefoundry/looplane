# M4 current-state provider completion audit

> Audit date: 2026-08-21 (Asia/Taipei)
> Audited revision: `6ed71c2` on `main`
> Method: current source, current CLI help, credential metadata, installed-provider status, and
> retained live run artifacts. This audit did not initiate OAuth, read a credential value, run a
> model, create a session, or modify production/test source. A concurrent worker had an uncommitted
> `docs/progress.md` planning update; this report does not overwrite or treat it as implementation.

## Verdict

**The active goal is not complete.**

The project now has a genuine Python-owned interactive/headless loop and a narrowly proven real
Ollama coding path. The final M3 Ollama eval is strong evidence: five independent public `pca run`
attempts all made the intended exact edit, passed `pytest`, produced audited artifacts, and left
their source repositories unchanged.

That does not prove the complete objective. The current repository still has no live PCA Codex
subscription grant, no Claude subscription backend/transport, no live remote API-URL coding E2E,
and no current real-TTY interactive success/resume run. Codex and Claude's official CLIs are logged
in, but PCA neither imports those credentials nor invokes those CLIs. Their login state therefore
cannot be counted as PCA provider completion.

## Requirement-by-requirement completion matrix

Evidence levels used below:

- **source**: current code exposes the behavior;
- **contract test**: committed tests simulate the behavior with scripted/mocked dependencies;
- **live transport**: a real provider responded;
- **coding E2E**: the public PCA CLI edited, verified, and emitted a complete run bundle.

| Objective requirement | Current authoritative evidence | Verdict |
|---|---|---|
| Bare/default interactive Python CLI | `pyproject.toml` maps `pca` to `coding_agent.cli:app`; the no-subcommand callback prompts for task/model and constructs this project's `AgentRunner`, `TTYApprovalPolicy`, and `ConsoleEventSink`. `uv run pca --help` renders. The CLI test substitutes a scripted model and headless approval policy. | **Implemented, not live-TTY proven.** No current artifact proves a real provider completed through actual terminal prompts. A global `pca` executable is not installed in the current shell; daily use is currently `uv run pca` from the repo unless installed separately. |
| Immediate tool/check trace | Durable events fan out to `ConsoleEventSink`; the projection prints model step, tool requested/started/completed, approval, verification, run, and session lines in sequence. Unit tests prove formatting/order. | **Implemented, partially evidenced.** M3 artifacts prove the underlying event sequence, but headless runs do not attach the console sink. There is no current real-TTY transcript. It is event-level trace, not streamed model-token output. |
| Approval before modify/execute | `replace_text`/`apply_patch` are `modify`; `run_check` and final verification are `execute`; non-TTY denies and TTY offers once/session/deny/cancel. Approval state/history is persisted before side effects. Contract/integration tests cover approval, session grants, denial, cancellation, and interrupted approval. | **Implemented, not real-human E2E proven at current revision.** M3 live runs contain approval events, but those were deterministic headless approvals enabled by `--unsafe-local-exec`, not a user responding in a TTY. |
| Durable session and resume | Every run creates `session.json`, an OS `flock` writer lease, atomic state, event sequence, provider/protocol/model/base SHA, messages, usage, budgets, approval history, and workspace validation. `pca resume` exists and current tests cover restart/fencing/fail-closed ambiguity. | **Implemented, live persistence proven, current live resume not proven.** M3 completed runs have current-schema sessions. The retained real Ollama resume artifact is pre-current schema (`protocol`/`prompt_version` absent), so current code would reject it; it cannot prove current live resume compatibility. |
| Headless mode retained for automation/Cloudflare | Public `pca run` is non-interactive, requires explicit tool capability and unsafe local-exec acknowledgement, prints JSON, and uses deterministic approval policy. | **Achieved locally.** Five retained M3 Ollama coding E2Es used this exact public surface. Cloudflare/container execution remains explicitly deferred and the local runtime is not an OS sandbox. |
| Ollama provider | `ollama` preset routes through canonical `OpenAICompatibleModel`, default loopback `/v1`, tool calling, Qwen no-think compatibility, and a finite 4096-token turn bound. Local Ollama `0.32.5` is responding; `qwen3:4b` and `qwen3:0.6b` are installed. | **Achieved for local `qwen3:4b` on one fixture.** Final retained eval is 5/5. This is not broad repository-level quality; attempts took roughly 153–228 seconds. |
| Custom/API URL bridge | Interactive/gateway expose `--api-url`; headless exposes `--base-url`. OpenAI-compatible remote HTTPS plus API key and loopback HTTP validation are implemented and contract-tested. | **Partially implemented, no live remote coding E2E.** Current provider env variables are absent. Naming differs across interactive/headless commands. The M3 summary has `base_url=null`, so it does not prove the explicit URL option. |
| Ollama API URL forwarding | `--provider ollama` accepts a base URL and works keylessly for loopback. | **Loopback only is proven.** The preset passes no API key; `OpenAICompatibleModel` rejects a non-loopback endpoint without one. Therefore an authenticated remote Ollama/Ollama Cloud URL is not currently usable through the `ollama` provider path. |
| ChatGPT/Codex subscription | PCA has app-owned PKCE login, fixed Codex Responses endpoint/audience, refresh, 0600 atomic store, tool/SSE adapter, experimental opt-in, and mocked transport tests. | **Not complete.** The PCA credential file and parent auth/state directories are absent. No live PCA Codex request or coding E2E exists. Official Codex CLI is logged in, but its store is deliberately not read. |
| Claude Code/Claude subscription | Native Anthropic API-key adapter exists; research describes an external Agent SDK/CLI backend boundary. Installed Claude Code reports a `claude.ai` login. | **Missing from PCA.** No Claude subscription class, CLI provider value, backend, SDK dependency, or live evidence exists. `ANTHROPIC_API_KEY` billing is not a Claude subscription bridge. |
| Real provider E2E | Retained M3 summary and all raw attempt roots are present; summary hash matches committed compact evidence. | **Achieved only for headless Ollama tiny-bug.** No Codex, Claude, remote API-key, or real interactive-TTY coding E2E. |
| Daily usable provider experience | Provider-neutral errors and terminal artifacts exist; session/tool safety is substantial. | **Not yet achieved across promised providers.** There is no redacted `providers/auth status`, no Codex logout/status, no global install in the current shell, inconsistent API URL option names, and requested subscription paths are unavailable. |

## Current CLI surface

### Default interactive

The no-subcommand callback is the default: `no_args_is_help=False` and
`invoke_without_command=True`. With a TTY it prompts for a task and model when absent. It then:

1. resolves one configured `ModelProvider`;
2. creates a `TaskContract` against the supplied/current repository;
3. permits all repository paths by default and uses `git diff --check` when no check is supplied;
4. injects `TTYApprovalPolicy` and `ConsoleEventSink` into `AgentRunner`;
5. prints completion, session id, and patch path.

The current CLI surface is usable as:

```bash
uv run pca --provider ollama --model qwen3:4b --repo /path/to/repo --check 'pytest -q'
```

`command -v pca` currently fails, even though the package declares the console script. This is an
installation/readiness issue, not an agent-core defect.

### Live trace boundary

The trace is durable-first: the JSONL sink succeeds before console projection. It renders compact
event lines and flushes after each event. `model.requested` is visible before a potentially slow
call, followed by tool and approval events. It does not expose incremental assistant/reasoning
tokens because the canonical loop awaits `ModelProvider.complete()`; even the Codex adapter
collects SSE internally before returning one `ModelTurn`.

### Approval boundary

Read tools auto-allow. Modify and execute actions require TTY approval, including final
verification. Session-scoped grants are stored in both the policy and session manifest. The loop
persists `approval.requested` and the pending action before asking, then persists resolution before
`tool.started`/`verification.started`. Resume abandons a request interrupted before execution and
refuses an ambiguous session whose durable last event says a side effect started.

The real M3 event streams include `approval.requested`/`approval.resolved` around every tool and
final verification, but the headless policy generated those decisions. They are proof of policy
wiring, not proof of the TTY prompt interaction.

### Session and resume boundary

Current sessions persist provider name, explicit protocol, model, prompt version, base SHA,
conversation, usage, step/repetition/wall-time budgets, verification, approvals, and writer token.
Resume validates request, contiguous events, exact workspace Git root/HEAD, lifecycle, and the
same provider/protocol/model.

The five final M3 sessions are terminal and correctly not resumable. An older live Ollama artifact
does contain three `session.resumed` events, but its manifest lacks current required
`protocol`/`prompt_version` fields and ended cancelled. It is historical behavioral evidence only,
not proof that the current schema resumes a real provider. Current resume correctness is supported
by committed scripted tests, not a current live-provider artifact.

### Headless boundary

`pca run` keeps provider calls in the coordinator, never reads stdin, writes one JSON result, and
requires `--unsafe-local-exec` before repository checks execute on the host. This is the strongest
completed surface: the final M3 eval invokes `python -m coding_agent run`, which is the same Typer
command implementation as `pca run`.

## Credential state metadata

Only presence, file type/mode/ownership when applicable, and official CLI boolean/auth-method
status were inspected. No token, claim, account id, email, credential JSON, keychain entry, or
credential file owned by another application was read.

| Credential/source | Current state |
|---|---|
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` | absent from current shell |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` | absent |
| `GEMINI_API_KEY` / `GOOGLE_API_KEY` | absent |
| `CLOUDFLARE_ACCOUNT_ID` / `CLOUDFLARE_API_TOKEN` | absent |
| `PCA_API_URL` / `OLLAMA_HOST` | absent; Ollama uses its default loopback URL |
| PCA app-owned Codex store | absent at default `~/.local/state/python-coding-agent/auth/openai-codex.json`; parent PCA state/auth directories also absent |
| Official Codex CLI | installed and reports logged in; not imported by PCA |
| Official Claude Code | installed and reports logged in with method `claude.ai`; not connected to PCA |

Because the PCA Codex store is absent, live permission safety cannot be observed. Current source
would require a regular non-symlink file with no group/other permission bits and writes it
atomically as `0600` under a `0700` parent. These are source/test guarantees only until a PCA grant
exists.

## Current live E2E evidence

### Final M3 acceptance run

Current retained summary:

```text
/private/tmp/pca-m3-release-eval.46EMiT/ollama-qwen3-4b/summary.json
SHA-256 006af53f6e27cf12d9e9e187eb131297a9f21fff41e8624f0d3f8d9036c8732a
```

This hash matches `docs/research/m3-live-eval-evidence.md`. All five attempt roots remain present. The
summary independently reports:

- provider/model `ollama` / `qwen3:4b`;
- `base_url=null`;
- 5 attempts, 4 required, 5 successful, `daily_ready=true`;
- exit zero, structured result, verified completion, expected changed file and patch, successful
  required tool, unchanged source HEAD/status/bytes for every attempt;
- successful tool sequence `list_files`, `read_file`, `replace_text`, `run_check`;
- non-zero real token usage;
- durations of 152.59, 167.46, 174.32, 227.52, and 158.54 seconds.

A separate retained completed M3 Ollama run has a contiguous 0–40 event stream and result
`completed`/`verified`; it changed only the calculator source and passed verification. Its events
show the exact model/tool/approval/verification lifecycle. Again, its approvals were headless.

### Evidence that does not exist

- no current real-TTY transcript or artifact labelled as an interactive run;
- no current-schema live provider interruption followed by successful `pca resume`;
- no live OpenAI-compatible remote URL coding result;
- no live authenticated Ollama remote/Cloud URL result;
- no PCA Codex OAuth file, live text/tool request, or coding result;
- no Claude subscription implementation or PCA run;
- no live native Anthropic, Gemini, or Workers AI result.

## Provider completion details

### Ollama

Local Ollama is the only fully executable provider today. Service version `0.32.5` responds and the
installed catalog includes `qwen3:4b` (2.5 GB) and `qwen3:0.6b` (522 MB). No model was loaded at the
metadata check. The 4B model meets the committed one-fixture repetition threshold, but latency and
scope make “general daily coding quality” unproven.

The `ollama` preset is not a general authenticated URL adapter. For a non-loopback HTTPS endpoint,
the shared model constructor requires an API key, while `_model_from_env(provider="ollama")` passes
none. A remote Ollama-compatible service must currently be configured as `openai-compatible` with
`OPENAI_API_KEY`, or the preset needs a provider-specific credential option.

### OpenAI-compatible/API URL

The generic adapter has a useful security boundary: absolute HTTP(S), HTTP only for exact
loopback, no URL credentials/query/fragment, and API key required remotely. Unit tests cover text,
tool calls, observations, usage, error classification, endpoint validation, and compatible
options.

Completion is still missing because no remote endpoint was exercised through the full coding
loop. The interface also uses `--api-url` for bare/gateway but `--base-url` for headless, making
configuration harder to remember and preventing one documented command from transferring across
surfaces unchanged.

### ChatGPT/Codex subscription

The dedicated adapter is materially more than a constructor: fixed OAuth audience and callback,
PKCE, account routing, token refresh, Codex Responses request shaping, SSE parsing, tool replay,
usage parsing, fixed destination, and secret-redacting errors all have mocked contract tests.

However, app-owned authorization is the decisive external state, and it is absent. The installed
official Codex CLI login cannot substitute because PCA explicitly does not read its credential
store. `pca auth` also has no redacted `status` or `logout`, so a daily user cannot inspect or revoke
PCA's grant through the CLI once it exists.

### Claude subscription

No current source path satisfies this requirement. `provider=anthropic` is a native Messages API
adapter requiring `ANTHROPIC_API_KEY`; the current CLI does not expose `claude-code`,
`claude-agent-sdk`, or another external-backend value. Official Claude Code authentication proves
only that the official CLI can use the user's account.

If the approved solution is the official Agent SDK/CLI, it should be an explicitly isolated
external coding-agent backend with its own event/permission/session contract—not falsely presented
as raw `ModelProvider.complete()`. If policy permits only API billing or an operator-approved
proxy, the product must say so and remove Claude subscription reuse from its success claim.

## Exact remaining work before the active goal can be completed

1. **Prove the actual default UX.** Run one real Ollama task through a TTY, exercise at least one
   modify approval and final execute approval, capture the console trace plus run bundle, interrupt
   a separate current-schema run, and successfully resume it.
2. **Make command installation/readiness explicit.** Either install the package/CLI for the user or
   document and verify a stable `uv tool install`/project command so `pca` is a daily command, not
   only `uv run pca` inside the checkout.
3. **Complete one remote API URL E2E.** Inject an explicitly authorized key into the coordinator,
   run the same manifest through a remote tool-capable provider, and verify that run artifacts and
   repository subprocess environment contain no secret. Unify or clearly alias `--api-url` and
   `--base-url`.
4. **Complete Codex authorization and E2E.** The user must perform PCA's app-owned browser grant.
   Then verify metadata/permissions without values, run text → tool → edit → check through the
   public CLI, test refresh behavior safely, and add redacted status/logout commands.
5. **Resolve Claude truthfully.** Revalidate current Anthropic authorization terms, then implement
   and live-test the closest allowed path: API-key/approved proxy or a deliberately isolated local
   Agent SDK/CLI backend. Do not scrape or forward Claude Code tokens.
6. **Fix authenticated Ollama/custom URL semantics.** Add an explicit credential mapping for remote
   Ollama-compatible endpoints or document that users must select `openai-compatible`; verify it
   with the public headless and interactive surfaces.
7. **Broaden daily reliability evidence.** Keep the 5/5 tiny fixture, but add at least one multi-step
   or multi-file task and one expected-failure/safe-stop task before claiming general daily use.
8. **Close M4 normally.** After implementation, rerun the full release gate and exact staged
   snapshot, write the M4 stage document and article draft, obtain independent review, and commit
   the complete stage.

## Completion statement that is accurate today

The accurate current statement is:

> PCA is a real Python-owned interactive/headless coding-agent harness. Its headless local Ollama
> path has a repeatable 5/5 verified result on the tiny Python fixture. Interactive trace,
> approvals, and resume are implemented and contract-tested, but current real-TTY/resume evidence
> is missing. Remote API URL, PCA Codex subscription, and Claude subscription completion remain
> unproven or unimplemented.

Any stronger claim—especially “Codex and Claude subscriptions work”—would contradict current
credential state, source, and live artifacts.

# M5 current-goal completion gap audit

Date: 2026-08-21 (Asia/Taipei)

Reviewed commit: `bc5931cbbb4f9f9e126e9d9edc21c400a8abf6e7`

Scope: read-only audit of the committed repository after M4. This report is the only file written.
No token value or credential file was read. `~/.codex` was not inspected. No web page was fetched.

The goal service exposed no active orchestrator goal to this subtask (`goal=null`), so requirements
are derived from the committed `progress.md` Goal, the M4 acceptance boundary, and the requested
audit dimensions: daily CLI; trace/approval/session; headless/Cloudflare; real subscription-backed
coding for Claude and Codex; and Ollama/configurable API URLs.

## Executive conclusion

The **local Python coding-agent goal is substantially achieved**, but the broader current goal is
not complete if it requires Cloudflare execution or real subscription-backed coding:

- Daily `pca`, interactive trace/approval, durable local resume, deterministic headless mode,
  source isolation, local Ollama, and a remote OpenAI-compatible API-key coding path are proven.
- Claude subscription connectivity is proven only through a tool-free official-CLI sentinel. It
  cannot edit a repository and does not exercise PCA's loop.
- The Codex subscription transport is implemented and mocked, but PCA's own grant is absent, so no
  authenticated model turn or coding run exists.
- There is no Cloudflare Worker, Sandbox/Container runner, deploy configuration, remote task API,
  cloud artifact/session store, or cloud approval/resume mechanism.

The minimum credible sequence is:

1. **M5: close local provider truth.** Obtain the user-assisted PCA Codex grant and prove the PCA
   loop; optionally add a sharply separated, local-only Claude external coding backend that edits
   only an outer-created disposable workspace and is independently verified.
2. **M6: cloud execution.** Deploy PCA's own loop with an API-key/Workers-AI model inside a real
   Cloudflare isolation boundary. Consumer-subscription credentials must not be the cloud path.

## Current local status — metadata only

| Component | Observed state | Meaning |
| --- | --- | --- |
| Git | HEAD exactly `bc5931c`; worktree clean before this report | M4 is a fixed committed baseline |
| Daily CLI | `/Users/xiaoxu/.local/bin/pca` and `coding-agent`; uv tool `python-coding-agent v0.1.0` | Installed daily command exists |
| PCA Codex grant | `pca auth status-codex` returned `not configured`, exit 1 | No live PCA Codex request is possible |
| Official Codex CLI | executable present, version `0.147.0` | Reference CLI exists; login status was intentionally not queried because that would touch its store |
| Official Claude Code | version `2.1.238`; redacted status reported `loggedIn=true`, `authMethod=claude.ai` | Subscription connectivity can be exercised by the official local child; no token was displayed |
| Ollama | version `0.32.5`; `qwen3:4b` and `qwen3:0.6b` installed | Existing local Ollama evidence remains reproducible |
| Cloudflare runtime | no Wrangler config, Worker entrypoint, Dockerfile/container config, Sandbox client, or deploy workflow | Cloud execution is not started |

## Prioritized acceptance matrix

| Priority | Requirement | Current status and evidence | Gap | Minimum acceptance |
| --- | --- | --- | --- | --- |
| P0 | Preserve PCA-owned default agent loop | **Achieved.** Bare `pca` constructs `AgentRunner`, `TTYApprovalPolicy`, `ConsoleEventSink`, canonical tools, and verification (`cli.py:86-158`). `ExternalAgentBackend` is a separate whole-task protocol, not `ModelProvider` (`backends.py:1-83`). | Do not blur this boundary when adding subscription coding. | Bare/default, `run`, and cloud paths continue to use `AgentRunner`. Any official CLI stays under `pca backend ...` and its results are never described as PCA-loop evidence. |
| P0 | Daily interactive CLI | **Achieved locally.** Global executable is installed. M4 retained a real bare-TTY Groq run with execute/modify/session approval and verified completion. | Packaging is personal/editable; no release/version/update story, but that is not needed for personal M5. | Fresh `uv tool install` smoke, global `pca --help`, one real TTY coding run, concise provider/auth preflight, and uninstall/update instructions. Existing evidence already satisfies most of this. |
| P0 | Live trace and approval before effects | **Achieved locally.** Console projection covers model/tool/approval/verification events. M4 TTY journal proves once/session decisions and reuse. | Remote/cloud approval has no transport or authenticated decision endpoint. TTY policy cannot be reused in a Worker request. | M6 must persist a waiting action before pausing, expose a run/action-bound approve/deny API, reject replay/stale decisions, then resume the same session. |
| P0 | Durable session and resume | **Achieved on one local filesystem.** Current-schema M4 hard interruption at `waiting_approval` resumed with contiguous events and verified completion. | `SessionStore` is path/JSONL/atomic-file based and uses POSIX `flock`; neither storage nor writer fencing spans containers/hosts (`session.py:186-309`). | Define a durable store and distributed lease/fencing contract, or prove a single durable Sandbox owns a run until terminal. Kill the coordinator, restore the same run ID/workspace/budgets, and complete without duplicating a side effect. |
| P0 | Deterministic headless mode | **Achieved locally.** `pca run`, exact checks, bounded tools, artifacts, 5/5 local Ollama and 5/5 remote Groq evidence exist. | It still executes trusted repository checks on the host and accepts a local filesystem repository path. | Retain local mode for CI/trusted repositories. Cloud mode must accept a remote/uploaded source descriptor, never a host path, and run checks only inside isolation. |
| P0 | Actual PCA Codex subscription coding | **Missing; implementation-ready but externally blocked.** Dedicated app store, PKCE, refresh, fixed Codex endpoint, canonical tools/SSE, status/logout and experimental opt-in exist. PCA status is not configured. | User browser grant; live supported-model discovery; authenticated text/tool/edit/check evidence; borrowed public client identity and inter-process refresh fencing remain experimental concerns. | User performs `pca auth login-codex`; metadata confirms regular 0600 app file; one text smoke; then a predeclared disposable-fixture eval through `pca run --provider openai-codex --experimental-subscription` with successful read/edit/check, verified patch, clean source, usage, artifact secret scan, and retained hashes. Keep local/private experimental unless the project obtains its own authorized client registration. |
| P0 | Honest Claude subscription boundary | **Architecture achieved; coding not achieved.** Official CLI login works and the external backend proves a bounded tool-free sentinel. Docs correctly say it is not repository editing or PCA-loop evidence. | The committed backend has only `task_id` and instruction, runs in an empty temporary cwd, and passes `--tools=`. There is no repository/base SHA, outer diff gate, or verification. | Make an explicit product decision: (A) core goal remains Claude API-key `ModelProvider`, with no Claude subscription coding claim; or (B) add a local-only optional external coding backend and label it as Claude Code's loop, never PCA's loop or a hosted subscription proxy. |
| P0 | Safe external-agent repository editing | **Not implemented.** Current Claude backend is safe partly because it has no repository and no tools. | Enabling Read/Edit/Bash while retaining user `HOME` gives the official child the user's filesystem/network authority. A disposable clone protects the original repo but is not a sandbox. Model/repository instructions can request escape or exfiltration. | See the external-edit security gate below. Until it passes, allow only trusted local fixtures with an explicit unsafe/local flag; do not expose this through Cloudflare. |
| P0 | Cloudflare execution | **Missing and explicitly deferred.** Workers AI is only a model adapter. No Worker/Sandbox/Container orchestration exists. | Worker ingress/auth, Python/container runner, Git source acquisition, sandbox, durable state, approvals, artifact store, quotas, cancellation, cleanup, deployment and live evidence. | M6 deployed authenticated API starts an isolated run using a non-consumer API credential, produces the normal artifact contract, supports status/cancel/approval, and survives one coordinator restart. |
| P1 | Ollama local provider | **Achieved on the tiny fixture.** Local `qwen3:4b` M3 eval passed 5/5 with unchanged source. Current service/models are present. | Evidence covers one small task/model; not broad daily quality. | Add one multi-file or failure-repair fixture before claiming general daily reliability. Existing result is enough to say local Ollama works. |
| P1 | Remote Ollama/API URL | **Contract achieved, live evidence split.** `--api-url`/`--base-url` alias works; remote HTTPS requires a key; exact loopback never receives `OLLAMA_API_KEY`. Groq proved the general compatible URL path 5/5. | No live authenticated remote Ollama/Ollama Cloud run. Groq does not prove Ollama-specific remote behavior. | If remote Ollama is a promised product path, run one authenticated remote Ollama text/tool/full coding eval and scan artifacts. Otherwise state that generic compatible HTTPS is proven and remote Ollama is contract-tested only. |
| P1 | Cloud model choice | **Several adapters exist; one remote compatible provider is live-proven.** Workers AI/native Anthropic/Gemini are mocked/contract-tested. | No model/provider has run inside Cloudflare execution. | First cloud E2E should use Workers AI or an authorized commercial API key stored only in Cloudflare secrets; record provider, model, usage, retries and redacted error behavior. |
| P1 | Evaluation breadth | **Strong harness evidence, narrow task distribution.** Repeated thresholds and durable hashes exist for one calculator bug. | A model can overfit the exact read/replace/check pattern; no multi-file, test-diagnosis, denial, malicious repo, or cloud recovery suite. | Add a small fixed matrix covering exact edit, multi-file patch, failing verification repair, denied action, timeout/cancel, and hostile-repository containment. Report per-capability results, not one aggregate score. |

## Product decision that must precede implementation

The committed Goal says model access should use protocol adapters rather than delegate the
experience to another coding-agent CLI. That is compatible with:

- **Codex subscription:** the app-owned Codex Responses adapter can power PCA's own loop after a
  user grant, although client authorization remains experimental.
- **Claude API:** the native Anthropic adapter powers PCA's own loop, but uses API billing rather
  than a Pro/Max subscription.

It is **not** compatible with describing official Claude Code delegation as PCA's own loop. Current
policy research also says a third-party/hosted product must not offer Claude consumer login or reuse
subscription rate limits without approval. Therefore “actual Claude subscription coding” has only
two honest interpretations:

1. a personal, local-only optional `ExternalAgentBackend` in which Claude Code owns the loop; or
2. out of scope for the product, with Claude coding supported through the commercial API adapter.

M5 should record this decision explicitly. It must not invent a Claude Pro/Max `ModelProvider`,
scrape a setup token, copy keychain state, or relay a consumer token through Cloudflare.

## External-agent editing security gate

Before giving Claude Code, Codex CLI, or another full agent a disposable repository, all of the
following are blockers or acceptance requirements:

### 1. Outer workspace ownership

- PCA resolves a full base SHA and creates a no-hardlink disposable clone before spawning the
  external agent. The child never receives the supplied source worktree.
- The external task contract contains run ID, workspace identity, base SHA, allowed path globs,
  patch/file/byte limits, wall time, and declared verification commands.
- After the child exits, PCA independently validates Git root/HEAD, changed paths, symlinks,
  cumulative diff bounds and source-repository HEAD/status/bytes. External success text is not
  authoritative.

### 2. Process and filesystem isolation

- `--safe-mode`, tool allowlists, permission modes and an ephemeral cwd are defense in depth, not
  an OS sandbox. Read/Edit/Bash tools can otherwise reach outside the workspace using absolute
  paths, child processes or language runtimes.
- A personal M5 experiment may run only on a trusted fixture with a noisy
  `--unsafe-local-external-agent` acknowledgement. Hostile or third-party repositories require a
  container/Sandbox with the workspace as the only writable mount, no host HOME, no Docker socket,
  no SSH agent, no cloud metadata endpoint and a non-root user.
- Process-group cancellation, descendants, output/event/input bounds and cleanup are already good
  local primitives and must remain enforced in the sandbox.

### 3. Authentication versus isolation

- The current Claude child retains user `HOME` specifically to read its own official auth. That is
  incompatible with claiming filesystem isolation and is unsuitable for a hosted multi-user path.
- Do not copy HOME/keychain auth into a container merely to make subscription coding work. For
  Cloudflare use an authorized API key/provider secret injected only into the coordinator/model
  process, never repository tools/checks.
- PCA Codex refresh tokens need an authorized app registration, encryption-at-rest/key management,
  account separation, revocation and cross-process refresh fencing before any hosted custody.

### 4. Network and data exfiltration

- Repository content and prompts are untrusted. A full agent with network access can exfiltrate
  source, model outputs or accessible credentials even if path checks later reject its patch.
- Default sandbox egress should be denied or allowlisted separately for Git clone, model API and
  package installation. Model calls should occur outside the repository subprocess environment.
- Verification must not inherit provider, GitHub, Cloudflare, SSH, npm, PyPI or user-home secrets.
  Package-network requirements must be predeclared and isolated from credential-bearing processes.

### 5. Approval and terminal truth

- Local interactive external editing needs approval before enabling edit/execute capabilities, not
  only after an unrestricted child has already acted.
- A remote approval is authenticated, run/action/effect-bound, expiring and single-use. State is
  persisted before returning `waiting_approval`; stale/replayed decisions fail closed.
- Final completion requires PCA's own diff/path gate and deterministic verification in the isolated
  workspace. Exactly one positive external terminal result is necessary but never sufficient.

### 6. Artifact and tenant hygiene

- Persist only canonical/redacted events, diff, verification and result. Raw child streams require
  explicit retention limits because they may contain source or model-repeated secrets.
- Artifact paths cannot be host absolute paths in cloud results. Use opaque run IDs and
  authorization-checked object references.
- Quotas cover request size, repository size, file count, patch size, model tokens/cost, CPU,
  memory, disk, processes, wall time, output, network bytes and artifact retention. Cleanup is
  observable and idempotent.

## Minimum credible M5 evidence — local provider completion

M5 should stay local and produce two explicitly different evidence lanes.

### Lane A: PCA-owned Codex loop

1. User completes PCA's browser grant; metadata-only preflight proves a regular 0600 app file and
   0700 directory without printing/hashing credentials.
2. One authenticated exact-text smoke proves the supported model and live SSE transport.
3. A predeclared eval runs at least three fresh fixture attempts through public `pca run` with
   `openai-codex`, explicit experimental opt-in, tool calling, finite steps/time, exact allowed path
   and deterministic check.
4. Acceptance requires successful canonical read, edit and check events; exact patch; verified
   completion; unchanged source; usage; clean cancellation; and retained event/result/patch hashes.
5. Scan artifacts and repository subprocess environments for generic credential patterns and the
   operator-known exact secret without retaining that secret. Exercise refresh through a safe
   expiry/401 test if the live service naturally permits it; do not force account damage.

Three attempts are enough for transport/coding feasibility. Use the established 4/5 threshold only
if M5 wants to claim this model is daily-ready on the fixture.

### Lane B: optional Claude external coding

1. Keep the command under `pca backend claude-code` and require explicit local/experimental/unsafe
   acknowledgement. Do not add it to `--provider`.
2. PCA creates the disposable fixture workspace. The official child receives only that cwd and the
   minimum documented tools needed to inspect/edit; avoid general Bash initially.
3. PCA captures and rejects out-of-scope changes, independently runs the declared check in its
   sanitized environment, produces the normal patch/result artifacts, and confirms the original
   source is byte-for-byte unchanged.
4. Retain one successful real subscription-backed edit/check result, one denied/out-of-scope edit,
   and one cancellation with descendant cleanup. Scan env/artifacts for token/API-key markers.
5. Label this evidence “Claude Code external-agent coding on a trusted local fixture.” It does not
   satisfy PCA-loop, cloud, sandbox, or third-party-product subscription claims.

If the product decision chooses API-only Claude, omit Lane B and record that Claude Pro/Max coding
is deliberately not a goal.

## Minimum credible M6 evidence — Cloudflare isolation and execution

M6 should use PCA's own loop and an authorized API-key/Workers-AI model. It should not depend on
Claude/Codex consumer subscriptions.

### Required implementation slices

1. **Ingress/control plane:** authenticated Worker routes for submit, status/events, approval,
   cancel and artifact retrieval; body/schema limits; idempotency key; opaque run IDs.
2. **Source contract:** Git URL/upload/object reference plus immutable revision, not `Path`; clone
   credential used only during acquisition and removed before agent/check execution.
3. **Runner:** pinned Python package and image digest inside Cloudflare Sandbox/Container, non-root,
   bounded CPU/memory/disk/process/wall time and denied/allowlisted egress.
4. **Durability:** run manifest/events/artifacts in durable storage, plus distributed writer lease
   or documented single-sandbox affinity. Local `flock` alone is insufficient.
5. **Approval/resume:** durable `waiting_approval`, authenticated single-use decisions, coordinator
   restart, same run/workspace/budgets, and existing ambiguous-side-effect fail-closed behavior.
6. **Secrets:** Cloudflare secrets only in the model coordinator; never in source workspace, child
   env, check logs, artifacts or client responses. No consumer-login endpoint.
7. **Operations:** health/readiness, structured redacted logs, usage/cost budget, cancellation,
   sandbox teardown, artifact TTL, retry/idempotency and orphan cleanup.

### Required live evidence

- Deploy from a committed/configured revision and retain deployment ID/config/image digest.
- Submit one fixed coding task through the public authenticated Worker API; observe read/edit/check,
  verified patch, unchanged source revision and canonical artifacts.
- Pause a second run for modify or execute approval, restart/replace the coordinator, approve it,
  and complete the same run without event gaps or duplicated side effects.
- Cancel/timeout a third run and prove process/sandbox teardown and no later mutations.
- Run a hostile fixture that attempts path escape, HOME/cloud-metadata read, secret-env read,
  undeclared command, network exfiltration, fork/descendant survival and oversized output; every
  attempt must be blocked or contained with auditable events.
- Search retained artifacts/logs for injected canary secrets and generic credential patterns; zero
  matches. Verify unauthorized cross-run/artifact access is denied.
- Record model usage, sandbox duration, storage size and cleanup so the paid-plan cost boundary is
  measurable rather than assumed.

## Recommended milestone order

| Milestone | Outcome | Must not claim |
| --- | --- | --- |
| M5A | PCA Codex personal grant and real own-loop coding E2E | Stable/authorized third-party Codex product integration |
| M5B optional | Claude Code external backend edits a trusted disposable fixture under outer PCA validation | PCA loop, sandboxed hostile-code safety, hosted Claude subscription proxy |
| M6A | Cloud runtime/storage/approval abstraction with offline/scripted model | Real provider or hostile-code readiness |
| M6B | Deployed Cloudflare Sandbox/Container E2E with Workers AI/commercial API credential and adversarial containment | Consumer-subscription cloud support or broad coding quality |

## Goal-completion decision

At `bc5931c`, it is truthful to say:

> PCA is a usable Python-owned daily local coding CLI with interactive approvals, durable local
> sessions, deterministic headless automation, local Ollama evidence, and a real remote compatible
> API-key coding result. It also has a restricted local Claude connectivity backend and an
> implementation-ready experimental Codex adapter.

It is not yet truthful to say:

> PCA runs on Cloudflare, safely executes hostile repositories in containers, uses Codex
> subscription for real coding, or uses Claude Pro/Max for repository coding through PCA's loop.

The overall goal is complete only after either:

- M5/M6 evidence above is produced; or
- scope is explicitly narrowed to the achieved local/API-key CLI, with Cloudflare and consumer
  subscription coding retained as named future work.

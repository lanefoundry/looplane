# M4: Provider completion and subscription boundaries

> Status: complete and committed; app-owned Codex live authorization is a named external dependency.
> Date: 2026-08-21
> Baseline: M3 commit `6bb4b5a`

## Scope

Turn the M3 headless Ollama proof into a daily CLI with real remote-provider, terminal approval,
and current-session recovery evidence. At the same time, separate three concepts that Pi,
OpenCode, OMP, Claude Code, and Codex expose in different combinations:

1. `AgentRunner` owns the coding loop, tools, approvals, checkpoints, and verification.
2. `ModelProvider` owns one canonical model turn through an authorized API transport.
3. `ExternalAgentBackend` delegates a whole task to another official agent runtime and never
   claims to have exercised PCA's loop.

## Baseline and acceptance criteria

M3 proved one local Ollama model through `pca run`, but the default TTY, current resume schema,
remote API URL, and subscription paths still lacked live evidence. M4 requires:

- a directly installed `pca` executable with the bare interactive command as the default;
- one remote HTTPS model endpoint completing at least four of five fixed coding attempts;
- real TTY approvals for execute and modify, with a verified terminal result;
- a hard-interrupted current-schema live run that successfully resumes;
- explicit remote Ollama credential behavior without leaking a key to loopback;
- Codex app-owned login usability without reading the official CLI credential;
- a policy-supported Claude design that does not re-export Pro/Max as a raw model API;
- retained redacted artifacts, hashes, release gates, and independent review.

## References studied

| Reference | Boundary used |
| --- | --- |
| Pi (`badlogic/pi-mono`) | Provider protocol and OAuth mechanics are separate from the agent loop |
| OpenCode | Current Anthropic subscription removal is a product-policy precedent; API adapters remain valid |
| OMP / oh-my-pi | An optional auth/model gateway translates wire formats rather than raw-passthrough proxying |
| Claude Code CLI and Agent SDK | The official runtime owns its authentication, tools, permission mode, and sessions |
| Codex CLI and app-server/exec | Official CLI delegation and app-owned Responses transport are different integration shapes |
| QuidProQuo harness-system article | Deterministic loop, tool, approval, and verification ownership stays in the harness |
| QuidProQuo security article | Provider secrets stay outside repository subprocesses and untrusted tool output |

Pinned source paths, policy links, and comparison details are retained in
`docs/research/provider-bridge-comparison.md`, `docs/research/subscription-bridges.md`,
`docs/research/m4-claude-subscription-boundary.md`, and `docs/research/m4-codex-live-readiness.md`.

## Ideas borrowed

- From Pi/OpenCode/OMP: keep provider identity, wire protocol, endpoint, credential resolution, and
  model capability as separate decisions.
- From OMP: make the OpenAI-wire gateway optional and translate through canonical contracts.
- From Claude Code/Codex: expose official CLI delegation as a distinct backend, not an invented
  completion adapter that nests two agent loops.
- From all three reference CLIs: make the common daily path short, while preserving explicit
  headless flags for automation and evaluation.

## Adjustments made for this project

- `pca run` now prefers `--api-url`; `--base-url` remains an alias so earlier scripts still work.
- Remote Ollama-compatible HTTPS uses `OLLAMA_API_KEY`, while exact loopback hosts always receive
  `None` even when the parent environment contains that key.
- Codex login binds its callback listener before opening the browser, ignores invalid early
  callbacks until the total deadline, offers hidden manual input, and exposes only redacted
  status/logout. It still uses a separately owned PCA store and remains experimental.
- `ClaudeCodeBackend` invokes the installed official CLI with no tools, no slash commands, no
  session persistence, an ephemeral cwd, bounded I/O/time/events, and process-group cleanup.
  PCA does not parse credentials; the official child retains `HOME` to resolve its own login.
- External result success is positive and fail-closed: exit zero, exactly one terminal result,
  `is_error=false`, and `subtype=success` are all required.
- Cancellation sets a cross-thread signal and waits for process-tree cleanup before propagating
  `CancelledError`.

## Ideas deliberately not adopted

- Claude Pro/Max as `ModelProvider`: Anthropic's current Agent SDK policy requires prior approval
  for a third-party product to offer `claude.ai` login or subscription rate limits.
- Consumer-token proxying through Cloudflare: a proxy cannot turn an unauthorized credential into
  an authorized API integration and adds credential-custody risk.
- Reading `~/.codex`, Claude keychain state, Pi/OpenCode/OMP stores, or importing another CLI's
  refresh token. PCA owns its Codex grant; the official Claude child owns its own auth access.
- Calling a Claude sentinel a coding E2E. The current backend is intentionally tool-free and is
  evidence only for the local delegated boundary.
- Calling one lucky remote run daily-ready. The fixed manifest retains a 4/5 threshold.

## Implementation

- `src/rivumi/oauth_login.py` and `src/rivumi/cli.py` harden Codex login and add
  redacted `status-codex` / app-only `logout-codex` commands.
- `src/rivumi/backends.py` defines a contract separate from `ModelProvider`;
  `src/rivumi/claude_backend.py` implements the restricted official-CLI backend.
- `src/rivumi/runtime.py` gives bounded subprocesses an optional cancellation signal.
- `src/rivumi/models.py` tightens explicit API-key validation for remote compatible URLs.
- `scripts/eval_live_provider.py` forwards explicit experimental opt-in and retains it in the
  summary instead of enabling subscription paths implicitly.
- `uv tool install --editable .` installs global `pca` and `coding-agent` executables.

## Verification evidence

Remote coding command used an authorized Groq key only in the coordinator environment:

```bash
uv run python scripts/eval_live_provider.py \
  --provider openai-compatible \
  --model openai/gpt-oss-120b \
  --base-url https://api.groq.com/openai/v1 \
  --output-dir /private/tmp/pca-m4-groq-release.hNFU3y/eval
```

Result: 5/5 completed and verified in 11.86, 40.86, 41.49, 42.01, and 46.11 seconds. Every run
used successful list/read/`replace_text`/`run_check`, produced the same patch, and preserved source
HEAD, clean status, and bytes. Scanning every retained evidence file for the exact key bytes found
zero matches.

Bare global `pca` completed TTY run `a66038f97f204cb3aaa96339f195c37e` after once/session
approvals for execute and modify. Run `2fa14fe342ff4f81a6ad2dc22cd8ffda` was then killed while
`waiting_approval` at sequence 5; `pca resume` emitted `session.resumed` at sequence 6 and ended
verified at sequence 54.

The restricted Claude backend returned the exact `CLAUDE_BACKEND_OK` sentinel through the user's
official CLI login. This proves only the delegated subprocess/auth boundary. PCA's app-owned Codex
browser authorization did not complete during the automated wait; no Codex live coding result is
claimed.

Final local gate:

```text
uv run pytest                 165 passed in 19.02s
uv run ruff check .           All checks passed
uv build                      sdist and wheel built
git diff --check              passed
pca --help                    global executable and all command groups rendered
```

The exact Git index tree `ecf588febfb1d25ebe4b19ac5e9ae297c63fc2ca` was exported to
`/private/tmp/pca-m4-stage.5OR031`. Its fresh uv environment independently passed all 165 tests in
20.51 seconds, Ruff, source/wheel builds, and dependency installation.

Independent review reproduced the initial fail-open result, cancellation, documentation, and
evidence-durability findings. Their fixes added positive result classification, prompt process-tree
cancellation, an exact auth boundary, and all five raw bundles plus compact hashes. Final verdict is
recorded in `docs/research/m4-release-review.md`.

## Known limitations

- The native PCA verification runtime still executes trusted repository code on the host; it is
  not an OS/network sandbox.
- Claude delegation is local-only, tool-free, and not a repository editor or PCA-loop evaluation.
- The app-owned Codex store remains absent until the user completes browser authorization. The
  borrowed public client identity keeps the feature experimental even after a personal grant.
- Groq 5/5 covers one small Python fixture and one remote model, not general coding quality.
- Cloudflare Worker/Container/Sandbox deployment remains the next independent milestone.

## Artifact paths

- Compact evidence: `docs/research/m4-live-evidence.md`
- Retained remote summary and five raw event/result/patch bundles:
  `.research/evidence/m4/remote-groq/`
- TTY session/event/result/patch: `.research/evidence/m4/tty/`
- Resume session/event/result/patch: `.research/evidence/m4/resume/`
- Provider audit: `docs/research/m4-provider-completion-audit.md`
- Claude policy boundary: `docs/research/m4-claude-subscription-boundary.md`
- Codex readiness: `docs/research/m4-codex-live-readiness.md`
- Independent review: `docs/research/m4-release-review.md`

## Commit

- Implementation commit: `11b2dfe` (`feat(providers): 完成日常 CLI 的遠端與訂閱邊界`).
- The implementation commit is the exact staged tree independently exported and verified above.
- QuidProQuo practice article is drafted at
  `quidproquo/src/content/posts/ai/2026-08-21-python-coding-agent-provider-boundaries.md` and
  remains uncommitted for user review.

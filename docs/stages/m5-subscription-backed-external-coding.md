# M5: Subscription-backed external coding

> Status: complete and committed.
> Date: 2026-08-21
> Baseline: M4 documentation commit `bc5931c`

## Scope

Turn M4's tool-free Claude connectivity sentinel and unaffiliated official Codex login into useful
local/private coding paths without merging either external agent loop into PCA's `ModelProvider`.
The official CLIs may produce a candidate change only inside a disposable working copy. PCA owns
source isolation, approval acknowledgements, patch acceptance, final verification, and artifacts.

## Acceptance criteria

- keep PCA's own `AgentRunner` as the default `pca` path;
- add bounded official Codex CLI and Claude Code `ExternalAgentBackend` implementations;
- pin a clean source HEAD, clone without hardlinks, and never give the child the source repository;
- isolate Git metadata from the child, reject staged/config/control-state manipulation, untracked
  output, disallowed paths, symlinks, binary patches, and verification-time patch mutation;
- prove the source tree, including ignored files, is unchanged before accepting a result;
- require separate opt-ins for external clone modification and trusted host verification;
- complete real coding runs using both already logged-in official subscription CLIs;
- retain current-code result/event/checkpoint/patch/test artifacts and independent review.

## References studied

| Reference | Decision used |
| --- | --- |
| Codex `exec --help` and OpenAI non-interactive docs | JSONL, ephemeral run, ignored user config/rules, workspace-write sandbox, non-Git cwd opt-in |
| Claude Code 2.1.238 help and permissions docs | safe mode, explicit built-in tool allowlist, `acceptEdits`, no session persistence |
| Pi, OpenCode, and OMP research from M4 | an external agent runtime is not a model adapter or raw subscription proxy |
| QuidProQuo harness-system article | PCA keeps deterministic policy, verification, state, and artifact ownership |
| QuidProQuo security article | prompt scope is not enforcement; guards belong at the tool/output boundary |

Detailed source and policy notes are retained in `.research/m5-codex-cli-backend.md` and
`.research/m5-claude-coding-backend-design.md`.

## Implementation

`ExternalCodingRunner` resolves the source HEAD, requires a clean worktree, and computes a
deadline-bound streaming SHA-256 snapshot of every source filesystem entry except Git internals.
That deliberately includes ignored files, directories, and symlinks. It creates a no-hardlink
clone, moves `.git` to `.rivumi-git-metadata` outside the child cwd, removes `origin`, and records Git
control hashes before delegation.

After the backend exits, PCA checks source integrity before invoking Git. It then checks the
isolated HEAD/index/config control files, rejects a newly created child `.git` and all untracked
output, obtains a full non-textconv/non-external diff against the immutable index, applies the path
policy and cumulative limits, and rejects binary/symlink/rename/copy output. Every explicit final
command runs through the existing bounded trusted-local verifier. PCA obtains and compares the
patch again afterward, so a check cannot silently change the accepted artifact.

The official Codex backend uses:

```text
codex exec --json --ephemeral --ignore-user-config --ignore-rules
  --sandbox workspace-write --color never --skip-git-repo-check -C <working-copy> -
```

It retains `HOME`/`CODEX_HOME` only so the official child owns its login, strips secret-like parent
environment variables, bounds JSONL/input/output/events/time, and kills the process group on
timeout or cancellation. Protocol drift remains fail-closed and records only safe event type names.

The Claude backend uses stream JSON, safe mode, no slash commands/session persistence, permission
mode `acceptEdits`, and the exact tools `Read,Glob,Grep,Edit`. It does not enable Bash, Write,
WebFetch, WebSearch, MCP, or subagents. The child retains `HOME` for its own official login; PCA
therefore labels this path local/private and experimental rather than an OS-isolated product path.

CLI commands require all three explicit boundaries: `--experimental-subscription`,
`--allow-external-modify`, and `--unsafe-local-exec`. At least one `--check` is mandatory; there is
no implicit Git command because the child cwd intentionally contains no Git metadata.

## Reproduced failures and fixes

- Two early Codex live attempts changed tracked or untracked `.pyc`; path/untracked guards rejected
  both. `PYTHONDONTWRITEBYTECODE=1` was then added to the controlled child environment.
- A staged forbidden file could be hidden from a working-tree-only diff. Isolating the immutable
  index and hashing it before inspection closes that path.
- A child-supplied `core.fsmonitor` could execute during PCA's `git status`. Git metadata now lives
  outside the child cwd; control files are compared before any Git command, while fsmonitor/hooks
  are explicitly disabled for inspection.
- A source ignored `.env` could change while Git status remained clean. The source invariant now
  hashes the complete non-`.git` filesystem with streaming reads and a wall-time deadline.
- An omitted CLI `--check` previously inherited `git diff --check`, which cannot run in the
  intentionally non-Git child cwd. External coding now requires an explicit final check.

Regression tests retain exact reproductions for staged output, config-hook execution order,
tracked and ignored source mutation, path/untracked rejection, output bounds, cancellation, and
backend protocol drift.

## Live evidence

Both final runs used clean temporary repositories copied from the tiny Python fixture, allowed only
`src/tiny_python_bug/calculator.py`, and ran exact `pytest -q` through PCA after delegation.

- Codex CLI run `cc56e556d8c94dcb865e4bec05b1e0d4`: `completed / verified`; patch SHA-256
  `aabb0491ca737152246996e0d9c1139acfc8fde2083b1c5aa3583f460124e35c`.
- Claude Code run `aa366fe83d03479eb61c7dfd755b4aa9`: `completed / verified`; the same patch
  SHA-256 and verification output.
- Both source repositories retained the exact request `base_sha` and an empty status.
- All request/event/checkpoint/backend/patch/test/result artifacts are mode `0600`.
- A secret-pattern scan of retained evidence returned no matches.

Complete hashes and the two fail-closed Codex attempts are documented in
`.research/m5-live-evidence.md`; raw artifacts are retained below `.research/evidence/m5/`.

## Verification

```text
uv run pytest                 183 passed in 25.86s
uv run ruff check .           All checks passed
uv build                      sdist and wheel built
git diff --check              passed
```

Independent review report and final verdict: `.research/m5-release-review.md`.

## Known limitations

- External backends are local/private, non-resumable whole-task delegates; their normalized events
  are emitted after bounded process capture rather than streamed live during execution.
- Claude Code uses a tool allowlist and post-run enforcement but PCA does not provide an OS or
  network sandbox for that official child. It may use its own auth through the retained `HOME`.
- Codex CLI supplies its own workspace-write sandbox, but PCA still treats its result as an
  untrusted candidate and rechecks every invariant.
- External editing currently accepts tracked-file modifications/deletions only; new untracked files
  fail closed rather than being implicitly staged.
- Source snapshot cost grows with every non-`.git` file, including ignored dependency trees. The
  task deadline turns oversized repositories into an explicit preparation failure.
- These subscription logins are not relayed to Cloudflare. Cloud deployment remains a separate
  milestone using authorized API credentials and a Container/Sandbox trust boundary.

## Artifacts

- Live evidence and hashes: `.research/m5-live-evidence.md`
- Raw bundles: `.research/evidence/m5/`
- Codex implementation research: `.research/m5-codex-cli-backend.md`
- Claude design/policy research: `.research/m5-claude-coding-backend-design.md`
- Goal gap audit: `.research/m5-goal-gap-audit.md`
- Independent review: `.research/m5-release-review.md`
- Draft practice article:
  `quidproquo/src/content/posts/ai/2026-08-21-python-coding-agent-subscription-cli-isolated-clone.md`

## Commit

- Implementation commit: `ff2b9ee` (`feat(backends): 完成訂閱 CLI 的隔離改碼流程`).
- Documentation commit: this stage report and progress closure commit.

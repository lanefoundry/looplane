# M5 independent release review

Date: 2026-08-21
Baseline: `bc5931cbbb4f9f9e126e9d9edc21c400a8abf6e7`
Reviewed target: the final M5 working-tree delta and retained current-code live evidence
Verdict: **GO**

## Executive conclusion

No Critical, High, or Medium release finding remains in the reviewed M5 delta.

The final implementation makes the external CLI result an untrusted candidate rather than an
acceptance decision. PCA owns a clean-source precondition, a deadline-bound source snapshot, an
isolated immutable Git control plane, changed-path/type/size checks, explicit host-verification
approval, post-verification patch equality, terminal source revalidation, and durable private
artifacts. Codex and Claude remain whole-task `ExternalAgentBackend`s and do not cross the
`ModelProvider` boundary.

The review initially reproduced five blockers against earlier revisions of the same working delta:

1. a staged disallowed file could be hidden from a working-tree diff;
2. child-controlled local Git config could make PCA's `git status` execute a fsmonitor hook;
3. tracked and ignored files in the source repository could be changed without preventing a
   `verified` result;
4. the implicit `git diff --check` default could not operate after Git metadata isolation; and
5. text artifacts inherited mode `0644` and were not atomically persisted.

All five are closed in the final snapshot, with focused regression tests and fresh live runs made
after the fixes. The final full suite, Ruff, package build, and diff check pass.

## Severity summary

| Severity | Open | Disposition |
| --- | ---: | --- |
| Critical | 0 | None found |
| High | 0 | All reproduced integrity/command-execution paths closed |
| Medium | 0 | No release-blocking correctness or security gap remains |
| Low | 2 | Evidence provenance and Claude runtime attestation limitations |

## Threat model and trust boundaries

The relevant attacker is an external model/CLI influenced by task text or repository content. It
may propose arbitrary edits, try to hide changes through Git state, create new files, consume the
wall-time/output budget, or return malformed or misleading terminal events. Repository verification
code is also untrusted host code, but the user explicitly acknowledges that separate boundary.

The protected assets are:

- the supplied source repository and ignored local files;
- files outside the disposable child workspace;
- the immutable comparison base, index, Git configuration, hooks, and accepted patch;
- host credentials and subscription stores;
- the meaning and confidentiality of retained result artifacts.

The local Claude path is explicitly not an OS filesystem sandbox. The official child retains
`HOME` only to resolve its own login, so it is restricted to trusted repositories and is not a
hosted subscription proxy. Codex adds its official `workspace-write` OS sandbox. Official OpenAI
documentation confirms that this mode restricts writes, disables command network access by
default, and protects `.git` under writable roots; PCA nevertheless independently isolates and
checks its Git metadata. See
<https://learn.chatgpt.com/docs/agent-approvals-security.md>.

## Acceptance matrix

| Surface | Evidence reviewed | Result |
| --- | --- | --- |
| Source isolation | `LocalGitWorkspace --no-hardlinks`; run root outside source; clean-source requirement; streaming snapshot of every non-`.git` file, directory, symlink, and ignored entry; post-backend and terminal comparison | Pass |
| Origin and Git control | clone `.git` moved outside child cwd; `origin` removed; HEAD/config/index/packed refs/attributes hashed; child-created `.git` rejected | Pass |
| Git command injection | isolated `--git-dir`/`--work-tree`; control snapshot checked before Git inspection; `core.fsmonitor=false`, `core.hooksPath=/dev/null`, `--no-ext-diff`, and `--no-textconv` | Pass |
| Patch/path/type guards | untracked rejection; `SafePathPolicy`; cumulative byte/line/file limits; binary, symlink, rename, copy, and non-regular-file rejection | Pass |
| Verification | at least one explicit `--check` required; exact shell-free argv; separate `--unsafe-local-exec`; common deadline; patch collected and compared again after checks | Pass |
| Approval | external modification and host execution require distinct explicit flags; backend is never launched when either prerequisite is absent | Pass |
| Deadline/cancellation | workspace preparation, source hashing, child process, Git inspection, and checks share the task wall-time budget; process-group timeout/cancel regressions pass | Pass |
| Codex lifecycle | `exec --json --ephemeral --ignore-user-config --ignore-rules --sandbox workspace-write --skip-git-repo-check`; bounded positive terminal protocol; cancellation cleanup | Pass |
| Codex auth ownership | only `HOME`/`CODEX_HOME` paths are retained; secret-like parent variables are removed; PCA neither opens nor copies the official store | Pass |
| Claude boundary | exact configured tools `Read,Glob,Grep,Edit`; `acceptEdits`; safe mode; no Bash/Write/WebFetch/WebSearch/MCP/subagent/session persistence; positive single success result | Pass |
| Model protocol separation | `ExternalAgentBackend` owns the delegated loop and is structurally separate from `ModelProvider` | Pass |
| Artifacts | request/events/checkpoint/backend result/patch/test/result retained; text writes use same-directory atomic replacement, `0600`, file and directory sync | Pass |
| Documentation truth | README and stage document call both paths local/private/experimental, distinguish official child auth from PCA ownership, disclaim Claude OS isolation and Cloudflare/subscription relay | Pass |

## Closed blocker reproductions

### Staged/index acceptance bypass — closed

The original implementation compared only the unstaged working tree. A test backend staged a
forbidden file, left one allowed file unstaged, and PCA initially returned `completed / verified`
while omitting the staged content from `changes.patch`.

The final implementation moves Git metadata out of the child cwd and hashes the isolated index and
control files before delegation. `test_external_runner_rejects_hidden_staged_change` now fails the
run before verification.

### Child-controlled Git hook execution — closed

The original post-run `git status` trusted the child-visible repository config. A reproduced
`core.fsmonitor` hook executed outside the declared verification argv.

The final implementation isolates Git metadata, compares config/control hashes before any PCA Git
command, disables fsmonitor/hooks explicitly, and disables external diff/textconv execution.
`test_external_runner_rejects_git_config_before_hook_can_execute` proves the run fails and the hook
marker is never created.

### Source repository mutation — closed

Earlier code checked the clone but not the source. A backend could modify a tracked source file and
still produce a verified clone patch. A first source guard then still missed a pre-existing ignored
`.env`, because ordinary Git status and `git ls-files` do not cover ignored entries.

The final guard requires a clean Git source and snapshots the complete source filesystem except Git
internals, using streaming SHA-256 reads and wall-time checks. It compares that snapshot after the
backend and on every terminal path. Both tracked-source and ignored-source regressions now return
`source_repository_changed` with no verification.

### Implicit final check — closed

After removing `.git` from the child cwd, an inherited default `git diff --check` returned exit 129.
Both external CLI commands now require at least one explicit `--check` and fail with CLI exit 2
before backend launch when it is omitted. PCA already performs its own hardened patch inspection.

### Artifact confidentiality/durability — closed

The first live bundles had `0644` events, patch, and test log files. The final text writer performs a
same-directory atomic replace with `0600`, fsyncs the file and parent directory when durability is
enabled, and tests assert every path in `result.artifacts` has no group/other permission bits.

## Live evidence validation

### Codex CLI

Run `cc56e556d8c94dcb865e4bec05b1e0d4` is current-code evidence:

- outer result: `completed / verified`;
- backend result: `codex-cli / completed`, exit 0, nine normalized events;
- exactly one changed path: `src/tiny_python_bug/calculator.py`;
- exact `pytest -q` check passed;
- retained workspace diff byte-hash equals retained patch hash
  `aabb0491ca737152246996e0d9c1139acfc8fde2083b1c5aa3583f460124e35c`;
- source still exists at request `base_sha` with empty Git status;
- all seven public artifacts are mode `0600`;
- documented event/checkpoint/backend/result hashes match the files.

### Claude Code

Run `aa366fe83d03479eb61c7dfd755b4aa9` is current-code evidence:

- outer result: `completed / verified`;
- backend result: `claude-code / completed`, exit 0, six normalized events and one positive
  `success / is_error=false` terminal;
- the same single allowed file, patch hash, and passing `pytest -q` verification;
- source still exists at request `base_sha` with empty status;
- all seven public artifacts are mode `0600`;
- documented event/checkpoint/backend/result hashes match the files.

### Earlier fail-closed attempts

The retained Codex attempts remain correctly described:

- `a4ce6c30160b4405b45d9f1ac6512282` rejected tracked bytecode outside the path allowlist;
- `d21351ae798942058585bd201f89903f` rejected untracked bytecode;
- the authoritative outer `result.json` is failed with no verification and an empty accepted patch,
  even though each external backend had returned provider-level completion.

The final two release bundles contain no matches for common API-key, authorization bearer,
refresh-token, access-token, or client-secret patterns. No credential store or token value was read
during this review.

## Low-severity limitations

### L1 — The evidence bundle does not independently attest the executable version and argv

The current source, exact-argv unit tests, installed CLI metadata, and stage documentation support
the Codex/Claude invocation claims. The public run bundle itself does not retain a sanitized binary
path/version or exact argv manifest. Copying the evidence directory alone therefore proves the
patch and PCA result, but not every property of the originating live invocation.

This is not a blocker because the evidence is reviewed together with the frozen source delta and
tests. A future milestone could add a sanitized `backend-metadata.json` containing backend name,
CLI version, non-secret argv/capability policy, start/end times, and artifact hashes.

### L2 — Claude's effective tool list is configured, not attested from the init event

PCA passes the exact `Read,Glob,Grep,Edit` allowlist and the installed CLI successfully completed
the live run. The normalized `system/init` event deliberately strips provider payloads and the
backend does not compare an effective tool list reported at runtime. Upstream flag-semantics drift
would therefore be detected only if it changes exit/result/patch behavior.

This remains Low because the path/source/patch acceptance boundary is independent of Claude's text,
and the README already states that PCA does not provide an OS sandbox for Claude. Runtime tool
attestation would still improve future drift detection without retaining raw provider frames.

## Release gates

Final snapshot results:

| Gate | Result |
| --- | --- |
| `uv run pytest -q -p no:cacheprovider` | Pass, 183 tests |
| Focused external runner/Codex/Claude/CLI suite | Pass, 37 tests |
| Four integrity/command-execution blocker regressions | Pass |
| `uv run ruff check --no-cache .` | Pass |
| `uv build --out-dir <temporary-directory>` | Pass, sdist and wheel |
| `git diff --check bc5931c` | Pass |
| Final evidence hash/mode/source/diff audit | Pass |

## Final verdict

**GO.** M5 is release-ready for the documented local/private experimental scope. It does not make
either subscription login a hosted service, does not turn either external CLI into PCA's own agent
loop, and does not provide a Cloudflare execution boundary. Those remain separate milestones.

# M5 Codex CLI backend

Date: 2026-08-21

## Outcome

`CodexCliBackend` is an `ExternalAgentBackend` transport for the user-installed,
official Codex CLI. It is deliberately not a `ModelProvider`: Codex owns its
authentication and agent loop, while the parent runner owns disposable-workspace
creation, approval policy, verification, and patch acceptance.

No live Codex request was made and no credential file or credential value was
read during implementation or verification.

## Verified local CLI contract

The installed executable reports `codex-cli 0.147.0`. Local `codex exec --help`
confirms the invocation surfaces used by the backend:

- `--json` for JSONL events
- `--ephemeral` to avoid persisting session rollout files
- `--ignore-user-config` and `--ignore-rules` for a predictable child policy
- `--sandbox read-only|workspace-write`
- `--color never`
- `-C <directory>` for the prepared workspace
- `-` to read the task from stdin

The backend constructs these arguments directly and never invokes a shell.
`danger-full-access` is rejected. The default is `workspace-write`, intended only
for a disposable workspace prepared by the caller.

## API and orchestration boundary

```python
backend = CodexCliBackend(
    executable="codex",
    sandbox_mode="workspace-write",
    timeout_seconds=300,
)

result = await backend.run(
    task,
    working_directory=disposable_fixed_sha_clone,
    event_sink=sink,
)
```

`working_directory` may also be a constructor default, but the per-run argument
takes precedence so the main coding runner can supply each fixed-SHA disposable
clone. The backend validates that the directory exists, is a directory, and is
not itself a symlink. It does not clone, checkout, verify, or accept changes.

Cancellation and timeouts reuse the existing bounded runtime, which terminates
the child process group. Input size, captured output, individual event size, and
event count are bounded.

## Authentication and data boundary

The child receives a small environment allowlist. Generic API keys, tokens,
passwords, credentials, and auth environment variables are not forwarded.
`HOME` and `CODEX_HOME` paths may be retained because the official CLI must own
and interpret its own authentication state; this backend never opens or copies
that state. Git credential prompting and global/system Git configuration are
disabled for the child.

Raw stderr is never returned. Raw JSONL is not exposed either. Thread/session
IDs, item IDs, command text, command output, and provider error text are dropped.

## Event contract

Normalization is fail-closed:

- only documented lifecycle event names are accepted;
- only an explicit item-type allowlist is accepted;
- only completed, non-empty agent messages become user-visible text;
- other accepted items become metadata-only activity events;
- exactly one thread start, one turn start, and one successful terminal are
  required;
- malformed, unknown, oversized, duplicated-terminal, non-zero-exit, truncated,
  or missing-final-response streams fail with stable terminal reasons.

The current bounded runtime captures the child stream before normalization and
sink delivery. Events are therefore safe and ordered but not yet incremental
while the child is running.

## Verification

Commands:

```text
uv run pytest tests/test_codex_backend.py -o addopts=''
uv run ruff check src/coding_agent/codex_backend.py tests/test_codex_backend.py
git diff --check -- src/coding_agent/codex_backend.py tests/test_codex_backend.py
```

Result: 9 tests passed; Ruff and whitespace checks passed.

The fake-executable suite covers exact argv/cwd/stdin, successful normalization,
external errors without leaked stderr/provider text, duplicate terminals,
unknown protocol events, timeout and cancellation process-tree termination,
secret-environment filtering, input/output bounds, and sandbox rejection.

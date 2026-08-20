# Python Coding Agent

Temporary engineering name for a Python-first coding agent. It works from a fixed Git commit in a
disposable clone, gives a model a small bounded tool set, reruns deterministic checks, and returns a
patch plus an auditable run bundle. The source repository is never edited.

The public product name and Cloudflare deployment are deliberately deferred until the local
execution contract is proven.

## Current capabilities

- Explicit provider-neutral agent loop with step, repetition, and wall-time guards.
- Common `ModelProvider` contract with canonical messages, tool calls, capabilities, usage, and
  errors.
- Adapters for OpenAI-compatible APIs, Anthropic, Gemini, Cloudflare Workers AI, and deterministic
  scripted tests.
- Disposable Git clone pinned to a full base commit.
- Bounded `list_files`, `read_file`, `search_text`, `apply_patch`, `run_check`, and `git_diff` tools.
- Segment-aware path allowlists, traversal/symlink protection, exact command argv, process-group
  timeouts, bounded output capture, and a subprocess environment without model/GitHub credentials.
- JSONL events, atomic checkpoints, final patch, test log, and result artifacts.

## Set up with uv

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
```

The `.venv/` directory is created and managed by `uv`; dependencies are defined in
`pyproject.toml` and locked in `uv.lock`. There is no separate `requirements.txt`.

## Offline proof

Run the deterministic fixture through the real loop and tool execution path:

```bash
uv run python scripts/demo_fixture.py
```

The command prints a completed `RunResult` and retains the run bundle below `runs/`.

## Run with a model

The run directory must be outside the target source repository.

```bash
export OPENAI_API_KEY='...'
export CODING_AGENT_MODEL='your-tool-capable-model'

uv run coding-agent run \
  --repo /absolute/path/to/a/git/repository \
  --task 'Fix the bounded bug and keep the existing behavior.' \
  --allowed-path 'src/**' \
  --allowed-path 'tests/**' \
  --check 'pytest -q' \
  --tool-calling \
  --unsafe-local-exec \
  --run-root /absolute/path/to/coding-agent-runs
```

Provider credentials are read by the coordinator process and are not forwarded to repository
checks. Python checks also run with bytecode writes disabled so an immediate equal-size source
patch cannot accidentally reuse a stale timestamp-based `.pyc` from an earlier verification.

`--unsafe-local-exec` is intentionally noisy: exact argv prevents shell interpolation, but pytest,
build scripts, and other checks still execute repository code on the host. Use it only for a
trusted repository. Untrusted work requires the later Docker/Cloudflare Sandbox backend.

| Provider | CLI value | Credential environment |
|---|---|---|
| OpenAI or compatible endpoint | `openai-compatible` | `OPENAI_API_KEY`, optional `OPENAI_BASE_URL` |
| Anthropic | `anthropic` | `ANTHROPIC_API_KEY`, optional `ANTHROPIC_BASE_URL` |
| Gemini | `gemini` | `GEMINI_API_KEY` or `GOOGLE_API_KEY` |
| Cloudflare Workers AI | `workers-ai` | `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN` |

Model capabilities vary across every provider. Tool calling remains fail-closed unless the
configured model/API path has been verified and `--tool-calling` is supplied. Native Anthropic,
Gemini, and Workers AI credentials are restricted to their official HTTPS hosts; a custom native
endpoint additionally requires the explicit `--allow-custom-provider-endpoint` acknowledgement.

## Run bundle

```text
runs/<run-id>/
  request.json
  events.jsonl
  checkpoint.json
  changes.patch
  test.log
  result.json
  workspace/          # disposable clone
```

`result.json` is successful only after the harness reruns every declared verification command.
Model text alone cannot mark a run successful.

## Boundaries

This milestone does not provide an OS/container sandbox. The disposable clone and Python policy
layer protect the source worktree and narrow tool behavior, but they are not a substitute for
process isolation. Do not use the local backend on hostile repositories. The next cloud milestone
will put the same Python runtime behind a thin Cloudflare Worker and Cloudflare Sandbox.

See [progress.md](progress.md) for current acceptance criteria and
[docs/stages](docs/stages/README.md) for milestone evidence.

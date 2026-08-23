# M13 stage report — live capture of OpenCode / Pi / OMP

Generated 2026-08-23 from real runs on this machine. Purpose: finalize the normalizers in
`src/rivumi/{pi,omp,opencode}_backend.py` against real event streams and record versions,
commands, event coverage, policy notes, and limitations.

## Capture harness

`scripts/m13_capture_runtimes.py` runs each installed CLI exactly as the backend would
(`backend._argv`), in a throwaway temp workspace, on a read-only task
("List the files in the current directory …"), and records raw stdout / stderr / argv /
normalized events under `.artifacts/m13-captures/<runtime>.*`.

Artifacts:
- `pi.jsonl`, `pi.normalized.json`, `pi.stderr.txt`, `pi.meta.json`
- `omp.jsonl`, `omp.normalized.json`, `omp.stderr.txt`, `omp.meta.json`
- `opencode.jsonl` (6 events — see OpenCode below), `opencode.stderr.txt`, `opencode.meta.json`
- `_summary.json`

## Versions (this machine)

| CLI | version | executable |
|---|---|---|
| pi | 0.84.2 | /opt/homebrew/bin/pi |
| omp | omp/18.0.0 | ~/.bun/bin/omp |
| opencode | 1.14.48 | /opt/homebrew/bin/opencode |

## Pi (`pi --mode json`)

- argv (no model): `pi --mode json "<instruction>"`. With model: `pi --mode json --model <id> "<instruction>"`.
- The flag `pi --mode json` is **correct** and emits a structured JSON event stream to stdout.
- Event vocabulary observed: `session`, `agent_start`, `turn_start`, `message_start`,
  `message_end`, `message_update` (`assistantMessageEvent` with `text_delta` / `thinking_delta` /
  `thinking_start|end` / `toolcall_start|delta|end`), `tool_execution_start|update|end`,
  `turn_end`, `agent_end`, `agent_settled`.
- Tool name location: at `toolcall_end` the name is in `assistantMessageEvent.toolCall.name`
  (NOT a top-level `toolName`); `tool_execution_start` carries `toolName` directly. The normalizer
  now reads `toolCall.name` at `toolcall_end`.
- `thinking_delta` / `thinking_start|end` are **intentionally ignored** (reasoning, not
  user-visible text) so the streamed assistant text stays clean.
- Final answer arrives as `message_end` with `message.content` = list of `{type:"text", text}`
  (+ optional `{type:"thinking"}`); `_message_text` extracts only the `text` items.
- Live result: rc=0, 18 events (15 message + 3 tool), malformed=False. Tool name `bash` captured.
- Provider used in capture: OpenRouter `stealth/ox-alpha` (zero-retention free router); pi's
  default provider on this machine is Google (free tier).

## OMP (`omp --mode json`) — Slice 4 divergence check

- argv identical to Pi: `omp --mode json [--model <id>] "<instruction>"`. Capture used Zen
  free model `hy3-free`.
- **Confirmed: OMP shares Pi's exact JSON event vocabulary** (`message_update` /
  `assistantMessageEvent`, `tool_execution_start|end`, `toolName`, `message_end`, …). Minor extra
  fields only (`attribution`, `intent`). No schema divergence in the captured subset.
- Decision: `OmpBackend(PiBackend)` reuse is valid; no separate normalizer needed. Captured tool
  name `read`.

## OpenCode (`opencode run --format json`)

- argv (confirmed against `opencode run --help`): `opencode run --format json [-m <provider/model>]
  "<instruction>"`. The `-m/--model` flag and `--format json` exist as assumed.
- **Error schema confirmed**: `{"type":"error","error":{"name":<str>,"data":{"message":<str>}}}`,
  e.g. `{"type":"error","error":{"name":"UnknownError","data":{"message":"Model not found:
  openrouter/z-ai/glm-5.2:free."}}}`. The normalizer now extracts `error.data.message`.
- **Robustness fix applied**: backends now pass `stdin=subprocess.DEVNULL` (and the capture script
  too). Without this, `opencode run` is a REPL that keeps the session open and blocks on stdin
  after its turn; with it, opencode exits (rc=0) on error. Verified: error runs now terminate
  cleanly instead of hanging.
- **Successful-run capture: DONE.** Pulled local Ollama model `ollama/gemma4` (9.6GB) — the only
  catalog id usable offline here; earlier attempts with `openrouter/z-ai/glm-5.2:free`, `zen/hy3-free`,
  and `9router/ollama/glm-4.7-flash` failed because those API-level ids are not in OpenCode's provider
  catalog / the endpoint was unresponsive — and ran `opencode run --format json --model ollama/gemma4
  "<instruction>"` via the capture harness (`stdin=DEVNULL`). Result: rc=0, malformed=False, 6 events
  across 4 types: `step_start`×2, `tool_use`×1, `step_finish`×2, `text`×1.
  - Event shapes confirmed against the live stream:
    - `step_start`: `{"type":"step_start","part":{"type":"step-start",...}}` — step marker, normalizer emits no event.
    - `tool_use`: `{"type":"tool_use","part":{"type":"tool","tool":"bash","callID":...,"state":{"status":"completed","input":{"command":"ls -a"},"output":...}}}` — normalizer reads the tool name from `part.tool`.
    - `step_finish`: `{"type":"step_finish","part":{"type":"step-finish","reason":"tool-calls"|"stop","tokens":...,"cost":...}}` — step marker.
    - `text`: `{"type":"text","part":{"type":"text","text":"README.md\nsrc\ntests"}}` — normalizer reads assistant text from `part.text`.
  - Normalizer fixed (`OpenCodeBackend._normalize_event`): message text from `part.text`, tool name
    from `part.tool`; `step_start`/`step_finish` are ignored. Verified by `test_opencode_success_schema`.
- Note: OpenCode provider id space diverges from the raw free-llm-models API ids. `opencode models`
  lists only `ollama/gemma4`, `ollama/nemotron-cascade-2`, `9router/ollama/glm-4.7-flash`; a locally
  pulled model not in this catalog (e.g. `qwen3`) is rejected with `Model not found` even though Ollama
  has it. Use a catalog id via `-m`/`--model`.

## Policy / billing notes (from free-llm-models skill, 2026-08-23)

- pi / omp / opencode own their logins; Rivumi never proxies their credentials (arch boundary held).
- Free tiers used for capture: OpenRouter free routers, OpenCode Zen `hy3-free`, Google free tier.
  Coding-agent first-turn prompts are large (~25K–77K tokens); avoid Groq Free Tier (8K TPM) for
  agent runs. Do not send sensitive code to free-tier models that train on data (Zen default).

## Limitations

- OpenCode success-path normalizer verified against a live `ollama/gemma4` stream (see OpenCode above).
- Captures are single read-only turns in an empty dir; multi-turn resume / approval / diff
  reconciliation per runtime still need dedicated live proofs (Slice 2/3/4 remainder).
- Normalizers remain permissive by design; they surface assistant text + tool activity even if a
  runtime adds event subtypes, failing closed only on malformed/truncated streams.

## Recorded-stream integration proofs (added)

`tests/test_external_runner_integration.py` proves per-runtime behavior end-to-end without live LLM
calls. Real captured streams (`.artifacts/m13-captures/*.jsonl`, copied to `tests/fixtures/m13/`)
are normalized by the **real** `PiBackend` / `OmpBackend` / `OpenCodeBackend` normalizers and replayed
through the real `ExternalCodingRunner`. A synthetic workspace edit lets the runner's diff/verification
pipeline reconcile a patch; the event stream stays genuine. Per runtime it asserts:

- normalization yields the expected tool name (`bash` / `read` / `bash`) and assistant message text,
  and is not malformed;
- the runner reaches `verified` with the expected `changed_files` (approval → diff → verify);
- an `external_agent_error` stream surfaces as `FAILED` with the actionable hint naming the backend;
- `request_cancel()` maps to `user_cancelled`.

Cancellation is runtime-agnostic (same runner cooperative-stop path); it is exercised for each backend
to confirm the wiring. Live multi-turn agent sessions (the CLI's own internal loops) are out of scope
for these deterministic proofs.

## Headless CLI entrypoints (`rivumi backend <runtime>`)

Added `rivumi backend opencode|pi|omp` subcommands (`src/rivumi/cli.py`) mapping to the registry
backends, each exposing `--model/-m`, `--check`, `--allowed-path`, `--run-root`, `--task-id`,
`--timeout`, `--allow-external-modify`, and `--unsafe-local-exec`, plus `--task/-t` as a PROMPT alias.
Approve/intent flags match the existing `claude-code`/`codex-cli` surface.

Two latent runtime bugs were exposed by a live headless attempt and fixed:

- `src/rivumi/runtime.py`: `run_bounded_command` unconditionally spawned a `_write_stdin` thread
  whenever `stdin is not None`; for the default `subprocess.DEVNULL` sentinel (an `int`) this crashed
  with `AttributeError: 'int' object has no attribute 'encode'`. The writer is now spawned only for a
  real `str` payload (`isinstance(stdin, str)`).
- `src/rivumi/external_runner.py`: both post-delegation source-integrity audits reused the (possibly
  exhausted) backend wall-clock deadline via `self._remaining(deadline)`. When the external CLI ran
  out of time, the audit immediately timed out itself and **masked** the real `timeout` terminal
  reason as `source_repository_changed`. The audit now uses a dedicated `_SOURCE_INVARIANT_TIMEOUT`
  (30 s) budget, so a backend timeout is correctly reported as `External coding run exceeded its
  wall-time budget.` and any genuine source mutation is still rejected.

Full suite: **511 passed**; `ruff check` clean.

### Known follow-up (out of scope)

A real `opencode` headless edit task (`ollama/gemma4`) completes a trivial prompt but hangs on an
edit-then-approve workload when stdin is `/dev/null` (OpenCode's headless edit path expects
interactive permission approval / its own autonomous flag). Wiring OpenCode's autonomous/approve flag
into `OpenCodeBackend._argv` is a follow-up so `rivumi backend opencode` can make file edits
non-interactively; the audit pipeline itself is verified correct by the recorded-stream tests above.


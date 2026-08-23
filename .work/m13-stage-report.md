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
- `opencode.jsonl` (empty — see OpenCode below), `opencode.stderr.txt`, `opencode.meta.json`
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
- **Successful-run capture: PENDING.** In this environment a valid provider turn did not complete:
  `opencode run` emits the error JSON to stdout and exits (rc=0) when the model is unknown, but
  with a loadable model it keeps the session open and does not return within the timeout. This is
  environmental (the default/selected provider was unresponsive here), not a backend-argv defect.
  The success-path event shapes (text/tool `result`) remain the permissive assumption in
  `OpenCodeBackend._normalize_event`; tighten once a responsive OpenCode provider is available.
- Robustness fix applied: backends now pass `stdin=subprocess.DEVNULL` so a CLI can never block on
  an inherited TTY stdin after its turn.

## Policy / billing notes (from free-llm-models skill, 2026-08-23)

- pi / omp / opencode own their logins; Rivumi never proxies their credentials (arch boundary held).
- Free tiers used for capture: OpenRouter free routers, OpenCode Zen `hy3-free`, Google free tier.
  Coding-agent first-turn prompts are large (~25K–77K tokens); avoid Groq Free Tier (8K TPM) for
  agent runs. Do not send sensitive code to free-tier models that train on data (Zen default).

## Limitations

- OpenCode success-path normalizer unverified against a live stream (provider unresponsive in env).
- Captures are single read-only turns in an empty dir; multi-turn resume / approval / diff
  reconciliation per runtime still need dedicated live proofs (Slice 2/3/4 remainder).
- Normalizers remain permissive by design; they surface assistant text + tool activity even if a
  runtime adds event subtypes, failing closed only on malformed/truncated streams.

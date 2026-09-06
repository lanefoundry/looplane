# TUI Visibility Comparison — looplane vs. reference coding agents

Last updated: 2026-09-07

Context: looplane has two runtime paths.

1. **Sidecar mode** — SDK query() (`scripts/claude-agent-session.mjs`) →
   sidecar frame protocol → Python (`src/looplane/claude_agent_session.py`) →
   TUI (`src/looplane/terminal/projection.py`). Known frame types: `ready`,
   `turn_accepted`, `approval_accepted`, `text_delta`, `thinking_delta`,
   `tool_started`, `tool_completed`, `action_preview_updated`,
   `approval_requested`, `context_usage_updated`, `runtime_model_updated`,
   `turn_completed`, `fatal`.

2. **Native mode** — Python `AgentRunner` (`src/looplane/agent/runner.py`) +
   `model_calls.py` drive the model loop directly; events flow through
   `projection.py` the same way. This path has its own retry/fallback stack
   and emits `model.retry`, `model.fallback`, `model.cache_trace` events.

Reference projects: **pi-mono**, **opencode**, **codex**, **claude-code-source**, **oh-my-pi (omp)**.

---

## 1. How reference agents surface "thinking"/reasoning

| Agent | Event/type carrying thinking | Rendering | Persisted? |
|---|---|---|---|
| **pi-mono** | Assistant message `content` blocks of `type: "thinking"` (Anthropic-native) | `pi-mono/packages/coding-agent/src/modes/interactive/components/assistant-message.ts#thinkingBlocks` — rendered as its own Markdown section in a dedicated `thinkingText` theme color; can be collapsed to one static "hidden thinking" label (`hiddenThinkingLabel`) via a settings toggle (`src/core/settings-manager.ts` has `modelThinkingLevels`/`thinkingBudgets`) | Yes, part of the message content array (session transcript) |
| **opencode** | Internal normalized part `type: "reasoning"`, mapped 1:1 to/from Anthropic's `thinking`/`thinking_delta` in `opencode/packages/llm/src/protocols/anthropic-messages.ts#lowerThinking` (streaming via `Lifecycle.reasoningDelta`/`reasoningEnd`, line ~686-776); OpenAI Responses reasoning summaries handled separately (bolted-title parsing) | `opencode/packages/tui/src/context/thinking.ts#useThinkingMode` — explicit `ThinkingMode = "show" \| "hide"`, **defaults to "hide"**, user cycles via slash command; migrates a legacy `thinking_visibility` boolean | Yes, stored as a `reasoning` part in the transcript regardless of display mode |
| **codex** | Protocol events `AgentReasoning`, `AgentReasoningRawContent` (true raw CoT when the model/provider exposes it), `AgentReasoningSectionBreak` — `codex/codex-rs/protocol/src/protocol.rs#AgentReasoningEvent` (~L2524) | `codex/codex-rs/tui/src/chatwidget` renders reasoning as history cells with streaming deltas (see snapshot tests `reasoning_delta_restores_recreated_status_indicator`, `final_reasoning_then_message_without_deltas_are_rendered`) | Yes, via rollout/thread history (`app-server-protocol/src/protocol/thread_history.rs` handles `AgentReasoning*` payloads) |
| **claude-code-source** | Native Anthropic `thinking` / `redacted_thinking` content blocks, `thinkingConfig` passed into the query (`src/query.ts` ~L662) | `src/components/Message.tsx` (~L524-612): renders `thinking` blocks in a distinct style; **only the last thinking block in a run is expanded by default** (`isLastThinking`/`lastThinkingBlockId`), everything earlier collapses; `redacted_thinking` renders a "thinking hidden for safety" placeholder | Yes, part of message content; verbose-mode toggle controls expansion, not existence |
| **oh-my-pi (omp)** | Native `type: "thinking"` content block (`packages/ai/src/types.ts`), PLUS a unique **cross-provider healing layer** for models that leak reasoning into the visible text channel instead of a structured part — `packages/ai/src/utils/leaked-thinking-stream.ts#wrapLeakedThinkingStream` live-splits leaked fences (`` ```thinking ``, `<think>`, Gemma/Harmony channels) out of the text stream into proper thinking blocks as deltas arrive; gated off only for official first-party Anthropic/OpenAI endpoints (`stream.ts#healLeakedThinking`) which already return structured thinking | `packages/coding-agent/src/modes/components/assistant-message.ts` — live "thinking pulse" (animated dots) rendered in place of a hidden thinking block while it streams, with a **tokens/sec speed badge** (`#thinkingTokens`, `#thinkingRateLive`); `hideThinkingBlock`/`proseOnlyThinking` settings (`config/settings-schema.ts:1381,1391`); per-role **thinking budget levels** (`minimal`→`max`, `thinkingBudgets.*` in tokens, `settings-schema.ts:6006-6016`) plus an `AUTO_THINKING` auto-classifier (`auto-thinking-classifier.test.ts`) that picks a level per turn | Yes, `thinking` content blocks persist in session content array like any other block |

### looplane status: DONE

**Sidecar** — the sidecar now requests thinking (configurable level via `--thinking-level`, defaults `medium`; `THINKING_BUDGETS` map in `claude-agent-session.mjs:76-78`), streams `thinking_delta` / `redacted_thinking` frames (`claude-agent-session.mjs:476-482, 524-528`), and exposes `/thinking` slash command to switch levels at runtime.

**TUI** — `projection.py` tracks `thinking_delta` events per turn (`_runtime_thinking_text`, `_thinking_actions`, `_thinking_started_at`, `_thinking_finalized` at lines 230-233), renders thinking as a collapsible tool-card with elapsed time, and finalizes it on first non-thinking content. `app.py` has the `/thinking` selector UI (`_show_thinking_level_selector`, line 1525) and `_apply_thinking_command` (line 1938) which persists the level and respawns the sidecar.

**Native mode** — `cli_config.py` validates `thinking_level` (line 195). Reasoning tokens are tracked in the session summary (line 1849 of `app.py`).

**Remaining delta vs. references:** no auto-thinking-level classifier like omp; no per-turn thinking token count / speed badge; no leaked-thinking healing for non-Anthropic providers. These are nice-to-haves, not blockers.

---

## 2. Other categories — current gap status

### 2a. stop_reason granularity — DONE

**Sidecar** — `claude-agent-session.mjs` now reads `stop_reason` from the SDK assistant message and sends it as `finish_reason` in the `turn_completed` frame (optional field, backward-compatible). Python-side `_exact_keys` accepts it via `optional` parameter.

**Native mode** — `model.completed` event already carried `finish_reason`; projection now shows "Output truncated (max tokens reached)" notice on `finish_reason == "length"`.

**TUI** — `TurnCompletedEvent` gained a `finish_reason: str | None` field (`conversation_runtime.py`). Projection shows "Completed · output truncated (max tokens)" when `finish_reason == "length"`.

### 2b. Partial/streaming tool-call JSON — GAP (unchanged)

Anthropic's `input_json_delta` stream events exist and are consumed by pi-mono/opencode to show tool arguments filling in live. looplane's sidecar only branches on `delta.type === "text_delta"` / `"thinking_delta"` — `input_json_delta` is invisible; `tool_started` only fires once the full tool_use block is complete, carrying a truncated `summary` string.

### 2c. Retry / rate-limit backoff notices — DONE (native mode)

**Native mode** — `model_calls.py` emits `model.retry` events (line 275) and `model.fallback` (line 285). Projection now handles both: `model.retry` shows "Retrying in Ns (attempt N)" in the status bar and writes error detail to the activity log; `model.fallback` shows "Switching to {model}…" and logs the fallback path.

**Sidecar mode** — retry is handled by the SDK internally; the sidecar has no retry/backoff event. This is an SDK limitation, not something we can fix on our side.

### 2d. Fine-grained sandbox denial reason — GAP (unchanged, by design)

codex threads sandbox-level denial reasons distinct from a human's approval decision. looplane's approval model only covers the user's allow/deny choice — there's no OS-level sandbox (tools are gated by the approval hook and path restrictions), so this gap is a design constraint, not a missing feature.

### 2e. Cache tokens in metrics — DONE

**Data layer** — both sidecar (`inclusiveUsage`, `claude-agent-session.mjs:130`) and native mode (`models.py:1064`) read `cache_creation_input_tokens`. `ContextTelemetry` carries `cached_input_tokens` and exposes `input_cache_hit_rate`.

**TUI** — `RuntimeMetrics.set_metrics` now accepts `cache_hit_percent` and displays it as `⚡N%` after the token counts. Also accepts `reasoning_tokens` and displays as `💭Nk`. Both are wired from `_update_metrics` in `app.py`.

### 2f. Cost / pricing display — GAP

No reference agent in the comparison set shows per-turn or cumulative dollar cost. Some third-party agents (aider, cline) do. Not prioritized.

---

## 3. Categories looplane ALREADY covers — do not misreport as gaps

- **Thinking/reasoning** → DONE (see §1 above).
- **Context window fill / token usage** → `context_usage_updated` frame, built from `inclusiveUsage()` (`claude-agent-session.mjs:112-132`) and `contextTelemetryForResult()` (`:164-184`). Includes `input_tokens`, `cached_input_tokens` (cache read), `cache_creation_input_tokens`, `output_tokens`, `total_tokens`, `context_window`. Rendered by `RuntimeMetrics` as `↑Nk ↓Nk · ctx N%`.
- **Model/provider switch notices** → `runtime_model_updated` frame (`updateRuntimeModel`, `claude-agent-session.mjs:145-151`), fired whenever the SDK reports a different model. Rendered in `RuntimeMetrics` model name segment.
- **Diff preview before apply** → `action_preview_updated` frame + `proposed_changes` on `approval_requested` — wired to `projection.py` for rendering before approval.
- **Tool call activity (start/complete, path, summary)** → `tool_started`/`tool_completed` frames — coarser than live-arg streaming, but the category is covered.
- **Loading/activity indicators** → `RuntimeLoadingIndicator` (🦦 swim animation), `RuntimeStatus` (glimmer text animation + elapsed time + esc hint).
- **Sub-agent/Task delegation** → deliberately forbidden in sidecar mode (`disallowedTools`). Not a gap.
- **MCP/server tools** → deliberately forbidden in sidecar mode (`mcpServers: {}`, `strictMcpConfig: true`). Not a gap.
- **Retry/fallback (native mode)** → events emitted (`model.retry`, `model.fallback`), but not yet rendered in TUI (see §2c).

---

## 4. Summary — gap status

### Completed

| Item | What was done |
|---|---|
| ✅ Thinking/reasoning | Sidecar requests thinking, streams `thinking_delta`, TUI renders collapsible card, `/thinking` command |
| ✅ `model.retry` / `model.fallback` in TUI | Projection handles both events — status bar + activity log |
| ✅ stop_reason granularity | Sidecar sends `finish_reason`, projection shows truncation notice, `TurnCompletedEvent` carries it |
| ✅ Cache tokens in metrics | `⚡N%` cache hit rate in RuntimeMetrics |
| ✅ Reasoning token badge | `💭Nk` reasoning tokens in RuntimeMetrics |
| ✅ Sidecar `finish_reason` | `claude-agent-session.mjs` reads `stop_reason` from SDK and sends in frame |
| ✅ Cost/pricing display | `pricing.py` with per-model rates + `estimate_cost()`, cumulative `$X.XX` in RuntimeMetrics |
| ✅ Leaked-thinking healing | `thinking_healing.py` state-machine filter for `<think>`/`<thinking>` fences, wired in runner for non-Anthropic providers |

### Remaining gaps

| Priority | Gap | Impact | Effort |
|---|---|---|---|
| P3 | **Streaming tool-call JSON** | Nice-to-have responsiveness during long tool builds | L — requires sidecar `input_json_delta` handler + TUI progressive render |
| P3 | **Auto-thinking classifier** | Auto-select thinking level per turn (omp has) | L — needs classifier model + heuristics |

# Coding CLI landscape for Rivumi

Date: 2026-08-22

## Decision

Rivumi owns and implements its native harness. Existing coding CLIs are optional external agents, not Rivumi's underlying harness. Use two execution modes:

1. **Native model mode**: Rivumi owns the agent loop, tools, context, permissions, sessions, and UI; model providers are accessed through their supported APIs.
2. **External coding CLI mode**: Rivumi launches an independent CLI harness and normalizes its events, approvals, session lifecycle, and output. Codex CLI, Claude Code, Gemini CLI, OpenCode, Pi, OMP, and similar agents belong here.

OpenCode, Pi, and OMP are useful architecture references for Rivumi's own harness and may also be exposed as external CLI adapters. Rivumi must not depend on any of them for its native execution path.

## Evidence matrix

| CLI | Harness ownership | Machine interface | Auth / subscription boundary | License / openness | Rivumi fit |
|---|---|---|---|---|---|
| OpenCode | OpenCode | `run --format json`, headless HTTP server, ACP stdio, official TS SDK | Broad API providers; supported OAuth options vary by provider. Do not rely on prohibited Claude/Google OAuth piggybacking. | MIT | Excellent generic open-harness backend |
| Gemini CLI | Google | Headless text/JSON/stream-JSON with tool events and exit codes | Official Google login can consume Gemini CLI quotas, including eligible Google AI plans | Apache-2.0 | Excellent official Google subscription backend |
| GitHub Copilot CLI | GitHub | Programmatic prompt mode and official ACP server | Requires Copilot entitlement; usage is accounted under the Copilot plan/AI credits | Proprietary product; CLI repository public | Excellent official subscription backend |
| Qwen Code | Qwen team | Headless prompt mode, experimental HTTP/SSE daemon, ACP, TS/Python/Java SDKs | API/provider credentials; supports multiple provider protocols and local inference | Apache-2.0 | Excellent open platform, but validate daemon stability |
| Cursor CLI | Cursor | Print mode, JSON/stream-JSON, session resume, ACP command | Cursor login/plan or API key | Proprietary | Good optional commercial backend |
| Goose | Linux Foundation AAIF | `goose run`, JSON/stream-JSON, ACP server, embeddable API | Many API providers; ACP can delegate to other agents/CLIs, which is not equivalent to exposing their subscription as a model API | Apache-2.0 | Strong automation/orchestration option |
| Aider | Aider project | One-shot message mode; Python use is explicitly unofficial | API keys and model providers | Apache-2.0 | Strong precision editor, weaker stable integration contract |
| Amp | Sourcegraph | Non-TUI runner and cloud-centric workflows | Amp subscription/orbs; can link supported account entitlements | Proprietary, explicitly no backcompat promise | Strong human UX, risky adapter contract |
| Crush | Charmbracelet | Primarily TUI; no equally strong documented RPC/structured-event surface found | API providers plus Charm's Hyper offering | FSL-1.1-MIT | Attractive TUI, lower integration priority |

## Recommended sequence

1. Define Rivumi's provider-neutral native harness contract and event schema.
2. Keep the existing Codex CLI and Claude Code CLI adapters as external harnesses.
3. Add Gemini CLI to fill the official Google subscription path.
4. Add OpenCode, Pi, and OMP only as sibling external CLI adapters where their machine interfaces are stable enough.
5. Add GitHub Copilot CLI, Qwen Code, or Cursor CLI based on user demand.

## Direct-use recommendations

- Best open, provider-flexible daily driver: OpenCode.
- Best Google subscription path: Gemini CLI.
- Best GitHub subscription path: GitHub Copilot CLI.
- Best extensible agent platform to watch: Qwen Code.
- Best precise git-aware editor: Aider.
- Best general automation/recipe agent: Goose.
- Worth trying for UX, but not a first integration contract: Cursor CLI, Amp, Crush.

## Primary sources read

- OpenCode CLI: https://opencode.ai/docs/cli/
- OpenCode SDK: https://opencode.ai/docs/sdk/
- OpenCode server: https://opencode.ai/docs/server/
- OpenCode providers: https://opencode.ai/docs/providers/
- Gemini CLI headless mode: https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/headless.md
- GitHub Copilot CLI overview: https://docs.github.com/en/copilot/concepts/agents/about-copilot-cli
- GitHub Copilot CLI repository: https://github.com/github/copilot-cli
- Qwen Code repository: https://github.com/QwenLM/qwen-code
- Goose repository: https://github.com/aaif-goose/goose
- Goose CLI commands: https://goose-docs.ai/docs/guides/goose-cli-commands/
- Goose ACP providers: https://goose-docs.ai/docs/guides/acp-providers
- Cursor CLI overview: https://cursor.com/docs/cli/overview
- Cursor CLI parameters: https://cursor.com/docs/cli/reference/parameters
- Aider scripting: https://aider.chat/docs/scripting.html
- Amp manual: https://ampcode.com/manual
- Crush repository: https://github.com/charmbracelet/crush

## Inferences and cautions

- A CLI accepting a subscription login does not make that subscription a reusable model API. Rivumi must preserve the official CLI harness boundary whenever plan usage depends on it.
- ACP support may mean either “this CLI can be controlled as an agent” or “this CLI delegates to another agent.” These are architecturally different and need distinct adapter metadata.
- Repository stars are only a maintenance/adoption signal; the ranking above primarily weights stable machine interfaces, auth-policy safety, and harness quality.

# Rivumi capability and current-state audit

Date: 2026-08-22

## Executive status

Rivumi currently has two real execution paths, but they are not feature-equivalent:

1. **Rivumi Agent** is an independently implemented, provider-neutral, bounded coding harness. It
   is strongest as a single task/run with deterministic tools, approvals, disposable Git workspace,
   checkpointing, verification, and auditable artifacts. Its model boundary is currently
   non-streaming and it does not yet provide a long-lived native model conversation or compaction.
2. **Claude Code and Codex CLI** have long-lived external conversation runtime implementations.
   Their own harnesses own model/tool execution while Rivumi owns the isolated workspace,
   normalized transcript, approval UI, patch audit, conversation persistence, and final safety
   boundary.

OpenCode, Pi, and OMP are not implemented. They are M13 sibling-runtime plans only.

## Status vocabulary

- **Proven now**: implementation exists and relevant local verification passed in this audit.
- **Historical live proof**: retained real-provider/deployment evidence exists, but was not rerun.
- **Implemented, not live-verified now**: code and contract tests exist without a current external
  service/login smoke.
- **Planned**: documented work with no implementation yet.
- **Absent/deferred**: deliberately unavailable or not currently scheduled.

## Capability matrix

| Area | Capability | Current status | Evidence / boundary |
| --- | --- | --- | --- |
| Native harness | Provider-neutral agent loop | Proven now | `src/rivumi/loop.py`, `src/rivumi/models.py`; full Python suite passed |
| Native harness | Step, wall-time, repeated-action, output, patch, and verification bounds | Proven now | `Limits`, `AgentRunner`, `ToolExecutor`; fault/E2E tests passed |
| Native harness | Tools: list/read/search/replace/apply patch/run check/Git diff | Proven now | `src/rivumi/tools.py`; path/command/patch tests passed |
| Native harness | Disposable pinned Git workspace and unchanged source tree | Proven now | `LocalGitWorkspace`, run/session tests |
| Native harness | Approval classification and fail-closed headless execution | Proven now | `src/rivumi/approvals.py`; approval/CLI tests |
| Native harness | JSONL events, checkpoints, result/patch/test artifacts, resume | Proven now | loop/session/event tests |
| Model APIs | OpenAI-compatible, Anthropic, Gemini, Workers AI, Ollama-compatible transport | Contract-proven | Adapter and mocked HTTP tests passed; no live remote API call in this audit |
| Model APIs | Local Ollama full coding fixture | Historical live proof | M3 retained a 5/5 tiny-fixture result; not a general model-quality claim |
| Model APIs | App-owned Codex OAuth/Responses transport | Implemented, experimental | Contract/refresh tests passed; current live grant was not rerun |
| Native conversation | Long-lived Rivumi Agent model session | Absent | Native path remains `TaskContract` → one `AgentRunner` run |
| Native conversation | Native context compaction | Absent | Compaction belongs to external `ConversationRuntimeSession` implementations |
| Native streaming | Provider token streaming | Absent | `ModelProvider.complete()` is explicitly non-streaming; UI streams run/tool state only |
| External runtime | Long-lived Codex app-server conversation | Proven now by tests | Typed runtime events, approvals, interruption, context usage/compaction, isolated workspace |
| External runtime | Long-lived Claude Agent SDK conversation | Proven now by tests | Pinned sidecar, typed events, approvals, interruption, isolated workspace |
| External runtime | Current real authenticated Codex smoke | Historical live proof | M11 retained `PCA_SMOKE_OK`; not rerun in this audit |
| External runtime | Current real authenticated Claude smoke | Implemented, not live-verified now | Local contract tests passed; no current external invocation |
| External runtime | OpenCode | Planned | No runtime/config/schema/dispatch implementation; M13 only |
| External runtime | Pi | Planned | No runtime/config/schema/dispatch implementation; M13 only |
| External runtime | OMP / Oh My Pi | Planned | No runtime/config/schema/dispatch implementation; M13 only |
| Conversation | Rivumi-owned 0600 durable semantic transcript | Proven now | `ConversationStore` and corruption/lease/resume tests |
| Conversation | Runtime switching with bounded semantic replay | Proven now for Claude/Codex | Conversation controller/TUI tests |
| Conversation | Vendor session IDs excluded from durable state | Proven now by schema/tests | Durable runtime schema stores Rivumi-owned semantic state only |
| TUI | Full-screen Textual UI, onboarding, runtime/model picker, transcript, inline tools/diffs | Proven now by tests | `src/rivumi/tui.py`; TUI pilot tests passed |
| TUI | Inline once/session/deny/cancel approvals | Proven now by tests | Typed approval events and TUI approval tests |
| TUI | Runtime choices | Claude Code, Codex CLI, Rivumi Agent only | CLI/TUI/config are explicitly hard-coded to these three |
| Gateway | Bounded loopback OpenAI Chat-compatible gateway | Proven now by tests | Health/models/non-streaming chat, auth/request bounds |
| Gateway | SSE or remote binding | Deferred | README explicitly defers both |
| Cloudflare | Authenticated Worker, capability DO, model proxy, Sandbox container | Proven locally now | 44 Vitest tests, TypeScript, generated type drift check passed |
| Cloudflare | Real Worker → Sandbox → provider coding run | Historical live proof | M6 retained deployed Groq run evidence; not redeployed now |
| Cloudflare | Hostile-code isolation | Not claimed | Agent and uploaded checks share a networked container |
| Packaging | `rivumi` wheel and source distribution | Proven now | `uv lock --check` and `uv build` passed |
| Product delivery | Current global editable command | Not checked now | `uv run rivumi --help` passed; global install was not refreshed/smoked |

## Current automated verification

Run against the current dirty worktree:

```text
uv run pytest -q                         passed; 419 tests collected
uv run ruff check .                      passed
uv lock --check                          passed
uv build                                 passed; sdist and wheel created
uv run rivumi --help                     passed
npm --prefix cloudflare test -- --run    passed; 44 tests
npm --prefix cloudflare run typecheck    passed
npm --prefix cloudflare run types:check  passed
git diff --check                         passed
```

Not rerun in this audit: live Anthropic/Gemini/OpenAI/Workers AI/Ollama calls, logged-in Claude or
Codex conversations, Cloudflare deploy, Docker reproducibility, real TTY visual screenshots, and
global editable installation.

## Startup performance playbook audit

The six actions in `docs/startup-performance-playbook.md` are all still open.

| Playbook action | Current status | Evidence |
| --- | --- | --- |
| `scripts/bench_startup.sh` with hyperfine/importtime | Absent | Script does not exist |
| Lazy-load heavy CLI modules | Absent | `cli.py` eagerly imports Claude/Codex backends, conversations, OAuth, gateway, models, and uvicorn |
| TUI time-to-editable-composer telemetry | Absent | No `RIVUMI_STARTUP_LOG` implementation outside the playbook |
| Parallelize independent startup work | Unproven/not implemented as a startup design | No recorded dependency graph or paired benchmark |
| Disk cache and single-flight for runtime/workspace discovery | Absent | OAuth refresh has its own single-flight, but startup discovery does not |
| CI median startup regression gate | Absent | No startup benchmark gate |

Fresh five-run import measurements were approximately 0.45–0.52 seconds for
`.venv/bin/python -c 'import rivumi.cli'`. `importtime` still attributes most cumulative cost to the
eager `codex_oauth` → `models` → `openai` chain. The playbook's older ~0.70 second measurement is
directionally consistent but should not be treated as the current baseline; a paired hyperfine
benchmark is still required.

Performance work should precede adding OpenCode/Pi/OMP. Otherwise each new eagerly imported adapter
will enlarge an already expensive startup graph and make the later refactor harder.

## Important limits that should remain explicit

- Native model events are not token streaming.
- Rivumi Agent is not yet a long-lived native conversation with context compaction.
- Provider errors are classified, but there is no general provider retry/backoff policy in the
  native loop; Codex credential refresh is a special case.
- Local disposable clones and exact argv policy are not an OS sandbox.
- Claude/Google consumer subscriptions are not native model API credentials.
- Remote model catalogs are not discovered; only bounded loopback Ollama discovery exists.
- External runtime feature parity must not be assumed. Approval, resume, usage, compaction, model
  switching, MCP, and cancellation need per-adapter capabilities.
- Multi-agent, MCP, RAG, long-term memory, LSP, GitHub writes/push/PR, and broader deployment remain
  deferred future capabilities.

## Delivery and repository state

- HEAD remains the committed M9 documentation baseline (`9a325f6`).
- The Rivumi rename, M10, M11, research/work artifacts, and roadmap updates are still in a large
  uncommitted worktree.
- M10 and M11 implementation/review evidence exists, but both still require article review and
  complete commits before they are closed milestones.
- M12 startup performance and M13 external runtime expansion are both planned and unimplemented.

## Recommended next order

1. Finish M10/M11 article review and create scoped commits for the Rivumi rename and current
   implementation, preserving unrelated worktree changes.
2. Execute M12 as a bounded performance milestone: benchmark script, lazy imports,
   time-to-composer telemetry, then parallel/cache work only where measurement justifies it.
3. Begin M13 Slice 1 by generalizing the existing `ConversationRuntimeSession` and capability model
   without adding a second runtime abstraction.
4. Add OpenCode, Pi, and OMP one at a time with contract tests and explicit live/policy evidence.

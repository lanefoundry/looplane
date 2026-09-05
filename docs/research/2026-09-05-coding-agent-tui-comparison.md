# Coding agent TUI architecture comparison

Date: 2026-09-05  
Scope: compare established coding-agent terminal interfaces across Python, Rust, TypeScript, and Go, while separating the agent runtime, CLI/TUI, and web-product architecture. This is a technical reference for Looplane, not an integration commitment.

## Conclusion

No single language or TUI framework dominates coding agents. The established implementations reviewed here use five materially different approaches:

- Rust with Ratatui/Crossterm for Codex CLI.
- TypeScript/React with Ink or an Ink-derived renderer for Claude Code and Gemini CLI.
- TypeScript with newer dedicated terminal renderers for OpenCode and Pi.
- Python with Textual, Prompt Toolkit, Rich, or combinations of them for gptme, SWE-agent, mini-SWE-agent, and Aider.
- Go with Bubble Tea, Bubbles, and Lip Gloss for Crush.

The framework choice correlates with implementation language and desired rendering model, but it does not decide clipboard reliability, approval semantics, or transcript continuity. Those behaviors are implemented explicitly by each product.

Python remains a credible implementation language for coding-agent harnesses. The strongest current Python references are Aider, mini-SWE-agent, SWE-agent, and gptme. OpenHands also has a Python agent SDK/server, but its current primary product UI is TypeScript/React, so it should not be described as a wholly Python application.

Textual is used by several real coding-agent projects, but it is not the default UI stack for every Python agent:

- gptme provides a dedicated Textual TUI and currently declares `Textual >=8,<9`, the same major release line as Looplane's Textual 8.2.8.
- SWE-agent uses Textual for its trajectory inspector rather than as the entire agent interface.
- mini-SWE-agent declares Textual and Prompt Toolkit alongside Typer and Rich.
- Aider uses Prompt Toolkit and Rich for its primary terminal interaction and implements explicit clipboard commands and monitoring.

The practical conclusion for Looplane is that Textual itself is a defensible choice. Enter handling, selection, mouse capture, and clipboard behavior still need explicit application-level design and regression coverage; framework adoption by peer projects does not provide those guarantees automatically. Replacing Textual would trade the current defects for a different framework's rendering and terminal-compatibility work unless Looplane also changes its product-level interaction model.

## Cross-language TUI comparison

| Product | Main implementation and TUI stack | Viewport model | Selection and clipboard | Approval interaction | Most useful Looplane reference |
|---|---|---|---|---|---|
| [Codex CLI](https://github.com/openai/codex) | Rust; Ratatui and Crossterm | Full-screen alternate buffer by default; `--no-alt-screen` provides inline mode and preserves terminal scrollback | `/copy` and `/raw` use explicit clipboard routing across native backends, tmux, OSC 52, and WSL; inline mode can also use native terminal selection | Native overlay exposes one-time, session/prefix-scoped, and rejection decisions | Rust state machine discipline, explicit inline/full-screen switch, deterministic key routing, and layered clipboard fallbacks |
| Claude Code | TypeScript/React; heavily customized embedded Ink renderer in the inspected reconstructed source snapshot | Supports full-screen virtualized history and a native-scrollback path; composer remains the session continuation point | Snapshot contains app-owned text selection, copy-on-select, OSC 52/native clipboard fallbacks, explicit copy bindings, `/copy`, and conversation export | Permission request is kept at the transcript tail with command/diff context and keyboard-first choices | Typed transcript projection, tool/result correlation, compact permissions, and mature clipboard fallbacks |
| [Gemini CLI](https://github.com/google-gemini/gemini-cli) | TypeScript/React; an explicitly pinned `@jrichman/ink` fork | React/Ink terminal application with alternate-buffer handling; exact defaults can change between releases | Provides a copy mode and uses `clipboardy`; normal terminal selection and app copy mode are distinct paths | `ToolConfirmationMessage` can present a diff and scoped confirmation choices | Official large-scale Ink application, React component composition, and testable confirmation state |
| [OpenCode](https://github.com/anomalyco/opencode) | TypeScript/Bun; OpenTUI Core, Keymap, and Solid | Application-owned full-screen session UI with scroll regions and modal/prompt surfaces | App-owned selection; copy callback writes through a clipboard service; supports selected text, message, session, URL, and debug-info copy actions | Inline permission prompt supports once/always/reject, keyboard navigation, rejection detail, and optional full-screen expansion | Strongest reference for treating selection, clipboard, permission, and transcript as first-class TUI services |
| [Pi](https://github.com/badlogic/pi-mono) | TypeScript; custom `pi-tui` renderer | Explicit `regular` main-screen mode with terminal scrollback and `fullscreen` alternate-screen mode with fixed editor/footer and internal scrolling | Full-screen drag selection, copy-on-select, explicit copy binding, `/copy`, native clipboard helpers, and OSC 52 fallback | Deliberately has no built-in permission popup, so it is not an approval-policy reference | Best reference for offering both native scrollback and application-owned full-screen modes without changing the agent core |
| [Crush](https://github.com/charmbracelet/crush) | Go; Bubble Tea, Bubbles, Lip Gloss, Glamour, and Ultraviolet | Message/update/view architecture; this pass did not verify a single fixed alternate-screen policy for every surface | Declares system clipboard packages; detailed selection and OSC behavior were not audited | Product-specific dialogs and controls built on Bubble Tea messages | Go alternative showing the value of a strict message/update loop and cohesive terminal component ecosystem |
| [gptme](https://github.com/gptme/gptme) | Python; Click/Rich CLI plus optional Textual 8.x TUI | Textual-owned application viewport; separate web client is also available | Web UI has explicit copy actions; no complete Textual selection/clipboard contract was confirmed | TUI controls are Python/Textual-owned | Closest same-language and same-Textual-major implementation reference |
| Looplane | Python; Textual 8.2.8 | Full-screen Textual application with unified M11 conversation transcript | Must explicitly reconcile mouse capture, selection, terminal-native copy, and app copy commands | Compact bottom-adjacent approval surface; policy remains separate from rendering | Preserve the Python harness while adopting explicit transcript, clipboard, and viewport contracts |

### Evidence boundaries

Codex, Gemini CLI, OpenCode, Pi, and Crush are backed by public official repositories. Claude Code is proprietary; the detailed implementation statements above come from the user-provided reconstruction of the Claude Code 2.1.88 npm sourcemap at `/Users/xiaoxu/Projects/coding-agent-reference/claude-code-source`, whose remote is `AprilNEA/claude-code-source`. They are useful source-derived observations, but they are not an official Anthropic source release or a guarantee about newer Claude Code versions. See [M11 Claude Code TUI source reference](m11-claude-code-tui-reference.md) for the bounded audit and file-level evidence.

The local reference checkouts reviewed on 2026-09-05 were:

- `/Users/xiaoxu/Projects/coding-agent-reference/codex` → `openai/codex`
- `/Users/xiaoxu/Projects/coding-agent-reference/claude-code-source` → reconstructed third-party snapshot
- `/Users/xiaoxu/Projects/coding-agent-reference/pi-mono` → `badlogic/pi-mono`
- `/Users/xiaoxu/Projects/coding-agent-reference/opencode` → historical `sst/opencode` remote, now served under `anomalyco/opencode`

Primary framework evidence:

- [Codex Ratatui and Crossterm dependencies](https://github.com/openai/codex/blob/main/codex-rs/tui/Cargo.toml#L73-L90)
- [Codex `--no-alt-screen` option](https://github.com/openai/codex/blob/main/codex-rs/tui/src/cli.rs#L72-L76)
- [Codex clipboard routing](https://github.com/openai/codex/blob/main/codex-rs/tui/src/clipboard_copy.rs)
- [Codex approval options](https://github.com/openai/codex/blob/main/codex-rs/tui/src/bottom_pane/approval_overlay.rs)
- [Claude Code reconstructed-source provenance](https://github.com/AprilNEA/claude-code-source)
- [Gemini CLI Ink fork and React dependencies](https://github.com/google-gemini/gemini-cli/blob/main/package.json)
- [Gemini CLI buffer and mouse routing](https://github.com/google-gemini/gemini-cli/blob/main/packages/cli/src/ui/AppContainer.tsx)
- [Gemini CLI confirmation UI](https://github.com/google-gemini/gemini-cli/blob/main/packages/cli/src/ui/components/messages/ToolConfirmationMessage.tsx)
- [OpenCode OpenTUI/Solid dependencies](https://github.com/anomalyco/opencode/blob/dev/packages/tui/package.json)
- [OpenCode selection and clipboard implementation](https://github.com/anomalyco/opencode/blob/dev/packages/tui/src/util/selection.ts)
- [OpenCode permission prompt](https://github.com/anomalyco/opencode/blob/dev/packages/tui/src/routes/session/permission.tsx)
- [Pi main-screen and alternate-screen renderer documentation](https://github.com/badlogic/pi-mono/blob/main/packages/tui/README.md)
- [Pi full-screen copy and navigation behavior](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/keybindings.md)
- [Crush Bubble Tea/Lip Gloss dependencies](https://github.com/charmbracelet/crush/blob/main/go.mod)

## Near-term implications for Looplane

1. Keep the Python harness and Textual for now. The comparison does not show a framework-level blocker that warrants a rewrite.
2. Treat the existing `--no-alt-screen` option as a real product mode. Consider a persistent screen-mode setting so users can choose native terminal scrollback without remembering a flag.
3. Add explicit copy actions for the last response, a tool result, and the transcript. Route clipboard writes through native local tools where available, tmux-aware handling, and OSC 52 for remote sessions, with honest failure reporting.
4. If full-screen mouse selection is a product requirement, implement and test an application-owned selection model comparable to OpenCode or Pi. This is larger than a Textual CSS/widget adjustment.
5. Preserve Looplane's transcript-adjacent approval presentation while borrowing Codex/OpenCode's explicit authorization scope and expandable details. Enter, number keys, arrows, and Escape should share one tested focus/state machine.

Implementation note (2026-09-05): the first bounded extraction now lives in
`tui_clipboard.py` and `tui_links.py`. It makes a non-empty selection take priority
over the App's `Ctrl+C` stop/exit binding, adds native local clipboard commands while
retaining Textual's terminal-mediated fallback, and allows clicked Markdown links only
for complete HTTP(S) URLs or existing files contained by the active repository. The
broader responsibility inventory and staged decomposition plan are recorded in
[TUI modularization audit](../diagnoses/tui-modularization-audit.md).

## Python coding-agent comparison

| Project | Python boundary | Interaction stack | Activity signal at review time | Relevance to Looplane |
|---|---|---|---|---|
| [Aider](https://github.com/Aider-AI/aider) | Predominantly Python CLI and coding harness; reads and maps repositories, applies edits, runs lint/tests, and integrates with Git | Prompt Toolkit and Rich; optional Streamlit GUI | Repository not archived; last repository push reported 2026-05-22 | Best reference for terminal input, clipboard behavior, precise editing, and Git-centered workflow |
| [mini-SWE-agent](https://github.com/SWE-agent/mini-swe-agent) | Pure Python, deliberately small software-engineering agent built around a Bash tool | Typer, Rich, Textual, and Prompt Toolkit are declared dependencies | Repository not archived; last push reported 2026-09-03 | Best reference for keeping the agent loop and environment interface small |
| [SWE-agent](https://github.com/SWE-agent/SWE-agent) | Predominantly Python agent harness with configurable tools and SWE-ReX execution environments | argparse/Rich CLI, Textual trajectory inspector, and a web trajectory viewer | Repository not archived and recently updated; README directs most new development to mini-SWE-agent | Useful for execution environments, issue-to-patch workflows, and trajectory inspection; less representative of a daily conversational TUI |
| [gptme](https://github.com/gptme/gptme) | Predominantly Python agent, tool system, CLI, server, and TUI; also includes a React web UI | Click/Rich CLI; dedicated Textual TUI; React/Vite web UI | Repository not archived; last push reported 2026-09-05 and v0.33.0 released 2026-08-19 | Closest current reference for a Python coding agent using Textual 8.x |
| [OpenHands](https://github.com/OpenHands/OpenHands) | Current architecture separates a Python software-agent SDK/server from the TypeScript client and product UI | React/TypeScript Agent Canvas; a separate Python OpenHands CLI has used Textual, Rich, and Typer | Main product and SDK repositories were active at review time | Useful reference for separating a Python runtime from a richer client; not evidence that the full product is Python-first |
| [Agentless](https://github.com/OpenAutoCoder/Agentless) | Pure Python research pipeline for localization, repair, patch validation, and testing | Batch/research workflow rather than an interactive TUI | Last repository push reported 2024-12-22 | Historical reference for a constrained issue-to-patch pipeline, not a current TUI peer |

Activity dates above are point-in-time GitHub repository signals, not guarantees of release cadence or long-term maintenance.

## Project notes

### Aider

Aider is the closest established Python reference for a Git-aware terminal coding assistant. Its primary package exposes the `aider` Python entry point, builds a repository map, applies model-produced edits, and can run linting and tests after a change.

Its terminal interaction is based on Prompt Toolkit and Rich rather than Textual. That makes Aider especially useful for comparing the behaviors that motivated this research: selection, paste, copy, input history, and terminal-native text handling. It includes a clipboard watcher plus `/paste`, `/copy`, and `/copy-context` commands instead of relying only on implicit terminal selection.

Primary evidence:

- [Project description and features](https://github.com/Aider-AI/aider)
- [Python package and CLI entry point](https://github.com/Aider-AI/aider/blob/main/pyproject.toml#L1-L30)
- [Prompt Toolkit and Rich terminal layer](https://github.com/Aider-AI/aider/blob/main/aider/io.py#L15-L36)
- [Clipboard watcher](https://github.com/Aider-AI/aider/blob/main/aider/copypaste.py#L4-L30)
- [`/paste` and `/copy` commands](https://github.com/Aider-AI/aider/blob/main/aider/commands.py#L1278-L1322)

### mini-SWE-agent

mini-SWE-agent is a compact Python coding agent from the SWE-agent project. Its main value is architectural clarity: a relatively small agent loop delegates repository inspection, editing, execution, and validation through a constrained environment/tool boundary.

Its package declares Typer, Rich, Textual, and Prompt Toolkit. Dependency presence alone does not prove that every interactive surface uses Textual, so Looplane should treat it as evidence of coexistence among Python terminal libraries rather than a one-framework blueprint.

Primary evidence:

- [Project overview and design](https://github.com/SWE-agent/mini-swe-agent/blob/main/README.md#L22-L38)
- [Python and terminal dependencies](https://github.com/SWE-agent/mini-swe-agent/blob/main/pyproject.toml#L33-L48)

### SWE-agent

SWE-agent is a Python software-engineering harness designed around issue resolution and reproducible execution environments. Its default workflow explicitly instructs the agent to inspect the code, reproduce a problem, modify the source, and rerun verification.

The project includes a Textual trajectory inspector, but its primary value is the agent/environment protocol rather than a conversational full-screen TUI. Its README now directs most new development toward mini-SWE-agent, which should be considered when choosing source code to emulate.

Primary evidence:

- [Current project direction](https://github.com/SWE-agent/SWE-agent/blob/main/README.md#L2-L10)
- [Default read, edit, execute, and verify workflow](https://github.com/SWE-agent/SWE-agent/blob/main/config/default.yaml#L18-L65)
- [Python package, Rich, and Textual dependencies](https://github.com/SWE-agent/SWE-agent/blob/main/pyproject.toml#L41-L68)
- [Textual inspector implementation](https://github.com/SWE-agent/SWE-agent/blob/main/sweagent/run/inspector_cli.py#L12-L17)

### gptme

gptme is a local-first terminal agent capable of shell and Python execution, file reading and writing, web access, plugins, and persistent sessions. Its Python package exposes separate CLI, TUI, server, agent, evaluation, ACP, and MCP entry points.

For Looplane's current UI work, gptme is the most directly comparable reference because its optional TUI explicitly uses Textual `>=8,<9`. Its product also demonstrates that a Python/Textual terminal client can coexist with a separate React/Vite web client without forcing the agent core into TypeScript.

Primary evidence:

- [Terminal/local-first positioning and tools](https://github.com/gptme/gptme/blob/master/README.md#L15-L18)
- [Shell, Python, and file-editing capabilities](https://github.com/gptme/gptme/blob/master/README.md#L137-L179)
- [Python CLI/TUI/server entry points](https://github.com/gptme/gptme/blob/master/pyproject.toml#L23-L43)
- [Textual 8.x dependency](https://github.com/gptme/gptme/blob/master/pyproject.toml#L139-L140)
- [React/Vite web client](https://github.com/gptme/gptme/blob/master/webui/package.json#L1-L16)

### OpenHands

OpenHands should be treated as a multi-component product rather than a Python-only repository. Its software-agent SDK and Agent Server are Python, while the current Agent Canvas/client is TypeScript. A separate OpenHands CLI has declared Textual, Rich, and Typer dependencies.

This makes OpenHands valuable for studying process and protocol boundaries, but weak evidence for choosing a Python UI framework. The relevant lesson is the separation between the agent runtime and its clients.

Primary evidence:

- [Python agent SDK tools and example](https://github.com/OpenHands/software-agent-sdk/blob/main/README.md#L31-L54)
- [Python SDK/server and TypeScript client boundary](https://github.com/OpenHands/software-agent-sdk/blob/main/README.md#L66-L68)
- [Current Agent Canvas architecture](https://github.com/OpenHands/OpenHands/blob/main/README.md#L105-L129)
- [OpenHands CLI terminal dependencies](https://github.com/OpenHands/OpenHands-CLI/blob/main/pyproject.toml#L24-L40)

### Agentless

Agentless is a Python research implementation that decomposes software issue resolution into fault localization, repair, and patch validation. It satisfies the functional definition of a coding agent pipeline but has no current interactive TUI and showed no recent maintenance at review time.

Primary evidence:

- [Localization, repair, and validation workflow](https://github.com/OpenAutoCoder/Agentless/blob/main/README.md#L25-L28)

## Exclusions and cautions

### Open Interpreter

Open Interpreter was historically a well-known Python local code-execution agent. Its current official main line is now predominantly Rust and describes the original Python implementation as community maintained. It should therefore not be used as current evidence for a Python-first coding-agent architecture.

- [Current implementation notice](https://github.com/openinterpreter/openinterpreter/blob/main/README.md#L124)
- [Ratatui/Crossterm terminal stack](https://github.com/openinterpreter/openinterpreter/blob/main/codex-rs/tui/Cargo.toml#L73-L90)

### GPT Pilot

GPT Pilot was a Python coding-agent CLI, but the official repository states that it is no longer actively maintained. Its README also documents a credential-stealing supply-chain incident spanning releases from August 2025 through June 2026. It is unsuitable as a current implementation dependency or baseline.

- [Maintenance and security notice](https://github.com/Pythagora-io/gpt-pilot/blob/main/README.md#L2-L16)

### General agent frameworks

AutoGen, LangGraph, CrewAI, and similar Python projects are agent frameworks. They can support coding agents but are not themselves comparable end-user coding harnesses, so they are outside this note's scope.

## Recommended reference order for Looplane

1. **gptme** for the closest Textual 8.x application structure, lifecycle, streaming output, and coexistence with other clients.
2. **OpenCode** and **Pi** for explicit application-owned selection, clipboard services, and dual viewport choices.
3. **Aider** for clipboard commands, terminal input ergonomics, Git integration, and precise edit workflows without a full-screen framework.
4. **Codex CLI** for deterministic keyboard event routing and a supported inline/full-screen boundary.
5. **Claude Code** for semantic transcript projection, correlated tool rows, permission placement, and clipboard fallback design, subject to the reconstructed-source limitation.
6. **mini-SWE-agent** for a small, legible agent loop and constrained tool boundary.
7. **SWE-agent** for sandboxed execution environments, issue-to-patch runs, and trajectory inspection.
8. **OpenHands** for separating a Python agent service from TypeScript or other client surfaces.

These sources should be mined for bounded patterns, not copied wholesale. Looplane's approval policy, unified M11 conversation model, event projection, and durable runtime boundaries remain product-specific.

## Research method and limits

Candidates had to do more than chat: they needed evidence of repository reading, source modification, command execution, or patch validation. The review separated the model-facing harness from CLI/TUI and web clients, following the principle that the model is only one component of the overall agent system.

Public research used Groundlane to read official GitHub repositories, manifests, source files, and repository metadata on 2026-09-05. The local coding-agent reference checkouts were also inspected directly. No third-party comparison article was used as primary evidence for an open-source project. GitHub language totals and recent-push dates are discovery and maintenance signals only; they do not prove code quality, adoption, or release readiness.

Related internal research:

- [Coding CLI landscape for Looplane](2026-08-22-coding-cli-landscape.md)
- [M11 Claude Code TUI source reference](m11-claude-code-tui-reference.md)

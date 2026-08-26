# M8: First-run onboarding and model selection

## Scope

Replace the raw `Model:` question on an unconfigured terminal with a bounded, provider-aware setup
flow. Keep `pca -p` and `pca exec` strictly non-interactive, preserve the existing agent loop and
credential boundaries, and establish a clean handoff point for the separate full-screen TUI stage.

## Baseline and acceptance criteria

M7 aligned the command grammar with familiar coding agents, but bare `pca` still asked for an
adapter-level model ID after it had already asked for a task. M8 requires setup before the task,
local Ollama model discovery, private non-secret persistence, actionable headless failures, and no
change to `AgentRunner`, approvals, disposable workspaces, or verification.

## References studied

| Reference | Boundary used |
| --- | --- |
| Claude Code 2.1.238 isolated first run | welcome/setup is a distinct state, not a raw model prompt |
| Codex CLI 0.147.0 isolated first run | explicit first-run choice precedes the coding session |
| Pi 0.70.6 isolated first run | missing models are resolved through provider/model UI |
| OpenCode 1.14.48 local help | providers/models are named setup concepts, separate from `run` |
| QuidProQuo harness article | product UI stays outside the provider-neutral model and agent loop |

The required `stealth_fetch` transport returned `Transport closed` while refreshing public Textual
documentation. Reference evidence for this stage therefore comes from the installed CLIs and the
local Ollama API rather than an unapproved fallback fetcher.

## Ideas borrowed

- Treat setup as a first-class state before accepting the coding task.
- Display provider names and discovered local models rather than exposing a blank implementation
  field.
- Keep non-interactive invocation deterministic and prompt-free.
- Make setup repeatable through an explicit config command.

## Adjustments made for this project

Ollama discovery uses only `http://127.0.0.1:11434/api/tags`, disables proxy-environment routing,
requests identity encoding, and bounds time, decoded bytes, count, name length, duplicates, and
terminal control characters. It does not inspect other CLIs or execute repository code.

An explicit CLI/environment provider locks the setup provider rather than becoming a picker
default. Config writes reuse the strict atomic `0600` schema and never contain credentials. Workers
AI readiness requires both account ID and token; Gemini accepts either supported key alias.

## Ideas deliberately not adopted

- M8 does not implement the full-screen UI; that is M9 because live events, approvals, cancellation,
  and resume need a coherent application boundary rather than terminal escape sequences.
- No remote provider model catalog is fetched during onboarding.
- No API key, OAuth token, or subscription credential is stored in CLI config.
- Experimental `openai-codex` is not advertised by the generic picker; it retains explicit flags
  and its separate app-owned grant flow.

## Implementation

- `src/rivumi/cli.py`: bounded discovery, provider/model pickers, readiness hints, headless
  contract, explicit-provider lock, and context/task presentation.
- `tests/test_cli_onboarding.py`: discovery, first-run, cancellation, persistence, TTY/headless,
  provider precedence, and credential-state regressions.
- `README.md`: first-run and strict non-interactive usage.

## Verification evidence

```text
uv run pytest -q tests/test_cli_onboarding.py tests/test_cli.py tests/test_cli_config.py
37 passed

uv run pytest -q
217 passed

uv run ruff check .
All checks passed!

uv build
sdist and wheel built successfully

git diff --check
clean
```

Independent review returned GO after reproducing print-mode behavior in a real pseudo-TTY and
checking explicit provider locking, discovery bounds, partial Workers AI credentials, private
config persistence, and the real two-model local Ollama setup.

## Known limitations

- Setup is line-oriented until M9 introduces the full-screen application.
- Remote provider models are entered by ID rather than discovered.
- Provider defaults are user-wide, not per repository.

## Artifact paths

- Independent review: `docs/research/m8-onboarding-release-review.md`
- Draft practice article:
  `quidproquo/src/content/posts/ai/2026-08-22-python-coding-agent-first-run-onboarding.md`

## Commit

- Implementation: `d15d61f`.
- Documentation/progress closure: this commit.

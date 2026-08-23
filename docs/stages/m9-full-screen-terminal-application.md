# M9: Full-screen terminal application

## Scope

Make bare interactive `pca` a real full-screen coding-agent application without replacing the
Python agent core. The UI owns presentation and input; `AgentRunner` remains the authority for
events, approval durability, disposable workspaces, verification, artifacts, and terminal status.

## Baseline and acceptance criteria

M8 removed the blank `Model:` prompt, but the session still became a sequence of line prompts and
console messages. M9 requires one coherent screen for onboarding, task input, raw live events,
approval, safe cancellation, and the result. `pca -p`, `pca exec`, non-TTY automation, and a plain
fallback must remain unchanged.

## References studied

| Reference | Boundary used |
| --- | --- |
| Claude Code 2.1.238 local terminal | persistent session surface and modal choices |
| Codex CLI 0.147.0 local terminal | repository context, activity, approval, and exit behavior |
| Pi 0.70.6 local terminal | model/session status and persistent composer |
| OpenCode 1.14.48 local terminal | full-screen default with a separate headless run path |
| Textual 8.2.8 installed package | App, ModalScreen, worker, pilot-test, Select, Input, and RichLog APIs |
| Existing PCA EventSink/ApprovalPolicy | raw event and provider-neutral approval integration seams |

The required `stealth_fetch` attempt for current Textual documentation returned `Transport closed`.
The implementation therefore pins the resolved package in `uv.lock` and verifies its actual local
API and behavior rather than silently using another web fetcher.

## Ideas borrowed

- A coding session should keep context, activity, composer, decisions, and result visible together.
- The interactive frontend and headless automation path should be separate entry modes.
- Side-effect approvals need a modal whose dismissal always resolves to a fail-closed decision.
- UI tests should drive the application as a terminal user, not only call rendering helpers.

## Adjustments made for this project

`TextualEventSink` receives the immutable `RunEvent` object. Durable JSONL is still written first by
the existing composite sink; the UI never parses console strings or decides whether a run passed.
`TextualApprovalPolicy` automatically permits read-only tools and turns modify/execute requests into
Once, Session, Deny, or Cancel decisions already understood by the core.

Stop is cooperative. Model waits are pure network work and may be cancelled immediately. Once a
tool/check thread has started, the flag is observed only after it returns and its completed event /
checkpoint is durable; only then is a cancelled result written and the session lease released.

## Ideas deliberately not adopted

- No terminal escape-sequence renderer was written by hand; Textual owns alternate-screen details.
- No multi-agent panes, embedded shell, source editor, fuzzy patching, or direct source write was
  added.
- No TUI-specific event or approval schema was added to the durable protocol.
- `pca resume` still uses the existing validated line-oriented path; replaying a historical event
  stream inside the TUI needs a dedicated read API and remains follow-up work.
- A run is still one bounded task, not an unbounded chat thread.

## Implementation

- `src/rivumi/tui.py`: full-screen app, onboarding and approval modals, raw event sink,
  approval policy, task composer, activity reducer, Stop, and result presentation.
- `src/rivumi/loop.py`: cooperative cancellation at model and side-effect-safe boundaries.
- `src/rivumi/cli.py`: real-TTY route, `--plain`, `PCA_NO_TUI`, and unchanged headless route.
- `tests/test_tui.py`: Textual pilot tests plus a real deferred-stop runner regression.
- `pyproject.toml` / `uv.lock`: reproducible Textual dependency.

## Verification evidence

```text
uv run pytest -o addopts='' -q
232 passed in 31.97s on the final gate (231 during independent review)

uv run ruff check .
All checks passed!

uv lock --check
resolved lock is current

uv build
sdist and wheel built successfully

git diff --check
clean
```

The independent review first reproduced hard `Ctrl+Q` shutdown, an abandoned blocking check,
model-task leakage, Rich-markup approval crashes, stale multi-run success, provider-close cleanup,
and delayed old events. After fixes it reran the original cancellation probe and observed
`tool.started -> tool.completed -> run.cancelled`, then returned GO.

## Known limitations

- Resume does not yet replay or continue inside the full-screen screen.
- Provider responses are non-streaming at the model contract, so live activity is step/tool based
  rather than token-by-token.
- Onboarding discovers only local Ollama models; remote model catalogs remain explicit IDs.
- The full-screen UI hosts one run at a time.

## Artifact paths

- Independent review: `.research/m9-tui-release-review.md`
- Draft practice article:
  `quidproquo/src/content/posts/ai/2026-08-22-python-coding-agent-full-screen-tui.md`
- Real PTY setup: `/tmp/pca-m9-smoke.rMCMsU`

## Commit

- Implementation: `3173ded`.
- Documentation/progress closure: this commit.

# M9 independent release review

Date: 2026-08-22

Verdict: **GO**

## Findings closed during review

- Textual's inherited priority `Ctrl+Q` initially hard-quit the worker. It is now overridden, and
  the TUI also shields the runner so an outer worker cancel becomes `request_cancel()` plus a wait
  for the safe terminal result.
- Direct cancellation after a side-effect started initially risked releasing the writer while a
  blocking check continued. Blocking prepare/tool/check calls now defer cancellation until return,
  then persist completion/checkpoint before `run.cancelled`.
- Model waits now always cancel and await their child model task on outer cancellation.
- Approval previews render untrusted patch/source text with Rich markup disabled.
- A failed later run clears an earlier success, and CLI exit is nonzero through `last_error`.
- Provider close failures no longer skip runner/model/UI cleanup.
- Run generations discard delayed queued events from an earlier run.

## Reproduction evidence

The original hard-cancel probe was rerun 200 ms after `tool.started`. It waited about 0.595 seconds
for the bounded check, the marker existed, and the terminal result was
`cancelled / user_cancelled`. Durable ordering ended:

```text
tool.started -> tool.completed -> run.cancelled
```

## Independent gates

```text
targeted lifecycle/CLI/session: 76 passed
full suite: 231 passed
uv run ruff check .: passed
uv lock --check: passed
git diff --check: passed
uv build: sdist and wheel passed
wheel: tui/cli/loop included; Requires-Dist textual>=8.2.8,<9
```

No files were modified by the reviewer.

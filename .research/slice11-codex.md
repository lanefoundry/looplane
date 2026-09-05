# Wave 1 Slice 1.1: Codex leaf extraction

Status: implementation complete; Codex focused Gate passed.

## Scope and decisions

- Owned facade: `src/looplane/codex_app_server.py`.
- New leaf modules: `src/looplane/runtimes/codex/{parsing,tool_mapper,approval_mapper}.py`.
- Required package initializers created only if missing; existing initializers preserved.
- Tests: `tests/runtimes/codex/test_leaf_helpers.py`.
- Bulk extraction script: `/tmp/slice11-codex-extract.py`. AST locations select existing method bodies without hand-rewriting their behavior; bounded completion helpers receive the existing bound method as a callback.
- Static compatibility methods bind the exact canonical leaf function objects. Dynamic bound methods remain session proxies; subclass/instance monkeypatch dispatch stays intact.
- Public class, imported domain objects, `_PendingApproval`, constants, transport, correlation, and all session state remain in the facade.
- JSON parsing takes count/byte limits as arguments. Incrementing counts, reading frames, routing, and failure handling remain session responsibilities.
- No vendor runner/backend, `loop.py`, `prompts.py`, unrelated repair, staging, or commit changes.

## Validation results

- `uv run ruff check --select I --fix src/looplane/codex_app_server.py src/looplane/runtimes/codex tests/runtimes/codex`: four import-order findings corrected in owned files.
- `uv run ruff format src/looplane/runtimes/codex tests/runtimes/codex`: four files formatted, one unchanged.
- `uv run ruff check src/looplane/codex_app_server.py src/looplane/runtimes/codex tests/runtimes/codex`: passed, exit 0, `All checks passed!`.
- `uv run pytest -q tests/runtimes/codex tests/test_codex_app_server.py tests/test_codex_conversation.py`: all selected tests passed, exit 0. Repository pytest configuration adds another quiet flag, so this invocation prints progress without a final count.
- Tests cover exact byte/character bounds, malformed JSON and error causes, UTF-8 truncation, decision filtering and permission payloads, tool descriptions/statuses, static leaf function identity, existing facade domain object identity, dynamic byte-limit changes, `_bounded`/`uuid4`/`json.loads` monkeypatch behavior, frame counting/failure ownership, and fresh-process leaf imports without the facade or CLI/TUI.
- Existing deterministic fake app-server and isolated conversation tests passed unchanged, including tool/approval lifecycle, malformed/oversized frames, and child shutdown.
- `RuntimeToolStatus` remains available through an explicit exact-object facade re-export. Ruff did not remove this old import surface.

## Changed paths

- `src/looplane/codex_app_server.py`
- `src/looplane/runtimes/__init__.py` (create-only package initializer, if absent at extraction time)
- `src/looplane/runtimes/codex/__init__.py`
- `src/looplane/runtimes/codex/parsing.py`
- `src/looplane/runtimes/codex/tool_mapper.py`
- `src/looplane/runtimes/codex/approval_mapper.py`
- `tests/runtimes/codex/test_leaf_helpers.py`
- `.research/slice11-codex.md`

## Gate boundary and risks

This report owns the Codex portion of Slice 1.1. Repository-wide lint, startup/build, and the combined Wave 1 Slice 1.1 Gate belong to the coordinating task. Fake app-server tests establish deterministic local protocol compatibility, not live vendor execution. Slice 1.2 state/transport extraction is deliberately deferred. Existing permissive/exception edge behavior is characterized rather than changed (for example, newline IDs, boolean command exit codes, unknown description fallback, and unhashable tool-status `TypeError`). No known new blocker remains in the owned scope.

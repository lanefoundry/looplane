# Slice 2.5 frozen production: known extraction defects

Status: production frozen pending explicit correction approval. This note does not
correct production code and does not authorize further implementation or Slice 2.6.

## Evidence boundary

These details come from the already-applied extraction script at
`/tmp/looplane-slice25-extract.py` and the source fragments used to construct it.
No generated source was reread, inspected, parsed, imported, or executed to prepare
this note. No test, lint, build, validation, or Git command was run.

Locations below use module/function names rather than unverified line numbers. The
earlier `.research/slice25-agent.md` describes intended ownership and behavior; it
must not be interpreted as evidence that the generated code parses or functions.

## 1. Transaction preparation: malformed indentation

Location: `src/looplane/agent/subagent_dispatch.py`, inside
`run_dispatch_subagents` -> nested `execute_subagent_transaction` -> the `try`
block preparing `transaction_call`.

The generator replaced the original tuple assignment with:

```python
prepared = await prepare(transaction_call)
            decision, effect, request_id = (
                prepared.decision, prepared.effect, prepared.request_id
            )
```

The first replacement line inherits the original eight-space indentation in the
dedented outer-function body. The following assignment was inserted with twelve
spaces instead of eight. The later `function()` helper adds four spaces uniformly,
leaving `prepared = ...` at twelve spaces and `decision, effect, request_id = ...`
at sixteen spaces in the emitted function. No intervening block opener justifies
that extra level. This is an expected parse-time indentation error, inferred from
the generator rather than observed through a parser run.

Intended correction: align the two assignments within the same `try` suite:

```python
try:
    prepared = await prepare(transaction_call)
    decision, effect, request_id = (
        prepared.decision, prepared.effect, prepared.request_id
    )
except ToolExecutionError as exc:
    # Preserve the existing repeated-action translation and re-raise behavior.
    ...
```

The snippet shows relative indentation only. Preserve the existing exception body.

## 2. Child launch: required runner factory was not forwarded

Location: the same module, `run_dispatch_subagents` -> nested `run_one` ->
`result = await run_subagent_task(...)`.

The canonical `run_subagent_task` signature was changed to require the keyword-only
`runner_factory: SubagentRunnerFactory`. Its invocation inside `run_one` was intended
to receive the factory passed into `run_dispatch_subagents`.

The insertion used an exact string match expecting twelve spaces before
`instruction=instruction` and `subagent_id=agent_id`. Those argument lines have eight
spaces after the extraction helper dedents the outer-function body. The replacement
therefore does not match, leaving the required `runner_factory` keyword absent from
that child-launch call.

Expected consequence after the indentation defect is corrected: child dispatch raises
a missing-required-keyword `TypeError` instead of constructing the child runner.
This is an inferred consequence, not an executed failure.

Intended correction: add `runner_factory=runner_factory` to that existing canonical
`run_subagent_task(...)` call. Preserve all existing child task, model, path, limit,
sandbox, and read-only approval arguments. Do not add a coordinator import or an
implicit factory fallback to the canonical leaf.

## 3. Transaction execution: the callback replacement has the same mismatch

Location: the same module, `run_dispatch_subagents` -> nested
`execute_subagent_transaction` -> the allowed-action `else` branch.

The generator intended to replace:

```python
observation = await self._execute_prepared_tool_call(
    transaction_call,
    effect=effect,
    request_id=request_id,
    deadline=deadline,
)
```

Its exact-match template expects sixteen spaces before the call arguments and twelve
before the closing parenthesis. The dedented body has twelve and eight respectively,
so that replacement also does not match. The subsequent generic substitutions do not
include `self._execute_prepared_tool_call`. Consequently, the extracted free function
is expected to retain that reference to an undefined `self`.

Expected consequence after the earlier defects are corrected: an allowed proposed
transaction reaches an undefined-`self` error instead of the injected executor port.
Again, this follows from the generator; the generated file was not inspected or run.

Intended correction: replace that call with the already-injected typed execution port:

```python
observation = await execute(
    PreparedToolCall(transaction_call, decision, effect, request_id),
    deadline=deadline,
)
```

Keep denial/cancellation handling, fingerprint guards, transaction event ordering,
and the successful-change flag behavior unchanged.

## Handoff constraints

- None of these corrections has been applied.
- This is not an exhaustive defect inventory; no post-write review was performed.
- A successful extraction-script exit establishes only that files were written.
- Slice 2.4's preserved baseline is `.research/slice24-frozen/`; its passing gates
  predate these Slice 2.5 changes.
- After explicit correction approval, the next implementer should correct these
  targeted locations and follow the separately authorized validation scope. The
  required unrun gates remain listed in `.research/slice25-agent.md`.
- Tests, including the old architecture cycle allowance, remain untouched. No
  Slice 2.6 implementation or design work was performed for this note.

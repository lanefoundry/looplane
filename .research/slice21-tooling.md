# Slice 2.1 MCP bridge

Status: complete within assigned scope; ready for main integration.

## Changed paths

- `src/looplane/tools.py`
- `src/looplane/tooling/mcp_bridge.py`
- `tests/tooling/test_mcp_bridge.py`
- `.research/slice21-tooling.md`
- `.research/slice21-focused.log`

Slice 1.1 already extracted built-in definitions. This change completes the remaining Slice 2.1 MCP discovery/bridge extraction. Slice 1.2 files remain frozen. No Slice 2.2 filesystem/search/patching/process changes were made, and tools.py was not reformatted wholesale.

## Substantive owner

`McpBridge` owns the configured server sequence, constructed client registry, latest successfully discovered MCP definitions, remote-tool mappings, resource mappings, prompt mappings, discovery rebuilding and client close traversal. Its resource/prompt declarations preserve exact schemas, descriptions, ordering, read-only and concurrency-safe metadata. Remote client definitions are preserved as their original objects without metadata rewriting.

`McpClient` is a typed structural port for discovery/close and existing executor dispatch operations. `McpToolNames` supplies the existing namespace functions explicitly. This avoids both duplicating namespace policy and importing the concrete MCP client/process implementation in the canonical bridge. Its only runtime project dependency is the shared ToolDefinition contract; the server configuration type import is TYPE_CHECKING-only.

The injected client factory is consumed only during construction and is not stored. A bound ToolExecutor construction method therefore does not make the bridge hold its executor. An independent weak-reference test proves the factory owner can be collected while its bridge remains usable. There are no mixins, executor private-field accesses, or executor callbacks retained by the bridge.

`ToolExecutor` retains concrete stdio/HTTP client construction, workspace/task-home/output-limit arguments, built-in definition composition and run_check allowlist customization, provider-facing refresh change comparison and execution dispatch/error conversion. Dispatch reads the bridge's public mappings. Its old private discovery methods and mapping properties are thin compatibility delegates, without duplicate mapping state.

## Preserved behavior

- Existing `looplane.tools.StdioMcpClient` and `HttpMcpClient` monkeypatch targets still control concrete construction and receive the same keyword arguments and limit precedence.
- Refresh clears mappings in place before rebuilding, removes stale tools, compares the full provider-facing MCP definition metadata, and reuses the configured client instances.
- A discovery exception propagates. Previous successful definitions remain while partially rebuilt mappings reflect the same discovery order as before; no transactional refresh semantics were introduced.
- Per-client ordering remains resource list/read, prompt list/get, then valid remote tool definitions. Namespace parsing retains existing filtering and routing behavior.
- Duplicate server names keep the last constructed client while preserving dictionary insertion order, exactly as the former comprehension did.
- Close traverses the current clients in order, forwards repeated calls, and stops on the first error as before. Construction failure still propagates without introducing implicit cleanup.
- Timeout defaults/caps and harness-only timeout validation, argument validation, bounded output, bounded exception formatting and the unmodified remote-tool error field remain in ToolExecutor dispatch.
- Built-in declarations and their model-facing policy text remain unchanged.

## Validation

```sh
uv run pytest -o addopts='' -q tests/test_tools.py tests/test_mcp_client.py tests/tooling
```

Result: **126 passed in 13.80s**, exit 0. Captured output: `.research/slice21-focused.log`.

The new bridge test module adds 44 cases for exact metadata/order, independent ownership, duplicate configs, discovery failure/recovery, refresh metadata and stale mappings, close/error ordering, original stdio/HTTP factory patches and constructor arguments, all five dispatch routes with timeout budgets and forbidden timeout overrides, argument validation, exception/output bounds, remote-error preservation and isolated canonical imports.

Existing real local stdio MCP integration, mocked HTTP/OAuth MCP, resource/prompt dispatch, dynamic tool discovery, ToolExecutor operations and Slice 1.1 leaf contracts pass in the same run.

```sh
uv run ruff check src/looplane/tools.py src/looplane/tooling/mcp_bridge.py tests/tooling/test_mcp_bridge.py
```

Result: **All checks passed**, exit 0. Only the two new Python files were formatted with Ruff.

## Remaining work and integration boundaries

No known missing Slice 2.1 requirement or focused regression remains. Main owns full-suite/architecture gates, plan updates and scoped commits. No staging or commit commands were run. No web requests were made. Process execution primitives and later filesystem/search/patching extraction remain assigned to other slices/workers.

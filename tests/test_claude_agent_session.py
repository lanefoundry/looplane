from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from textwrap import dedent

import pytest

from rivumi.approvals import ApprovalDecision, ToolEffect
from rivumi.claude_agent_session import ClaudeAgentSession
from rivumi.conversation_runtime import (
    ActionPreviewUpdatedEvent,
    ApprovalRequestedEvent,
    ApprovalResolvedEvent,
    ContextUsageUpdatedEvent,
    ConversationProtocolError,
    RuntimeModelUpdatedEvent,
    RuntimeToolKind,
    RuntimeTurnStatus,
    TextDeltaEvent,
    ToolCompletedEvent,
    ToolStartedEvent,
    TurnCompletedEvent,
    TurnStartedEvent,
)


def _fake_sdk(tmp_path: Path) -> Path:
    sdk = tmp_path / "sdk"
    sdk.mkdir()
    (sdk / "package.json").write_text(
        json.dumps({"name": "@anthropic-ai/claude-agent-sdk", "version": "0.1.77"}),
        encoding="utf-8",
    )
    (sdk / "sdk.mjs").write_text("export const query = () => {};\n", encoding="utf-8")
    return sdk


def _fake_sidecar(tmp_path: Path, behavior: str = "normal") -> Path:
    sidecar = tmp_path / f"fake-claude-sidecar-{behavior}.py"
    sidecar.write_text(
        dedent(
            """\
            import json
            import os
            import subprocess
            import sys
            import time

            behavior = __BEHAVIOR__
            active = None
            turn_number = 0

            def emit(value):
                sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\\n")
                sys.stdout.flush()

            assert "--sdk-path" in sys.argv
            assert "--cwd" in sys.argv
            emit({"type": "ready", "sdk_version": "0.1.77", "setting_sources": []})
            if behavior == "descendant":
                marker = os.environ["RIVUMI_TEST_MARKER"]
                code = (
                    "import pathlib,time; time.sleep(0.8); "
                    f"pathlib.Path({marker!r}).write_text('alive')"
                )
                subprocess.Popen([sys.executable, "-c", code])

            for line in sys.stdin:
                frame = json.loads(line)
                if frame["type"] == "turn":
                    active = frame["turn_id"]
                    turn_number += 1
                    emit({"type": "turn_accepted", "turn_id": active})
                    if behavior == "unknown":
                        emit({"type": "vendor_magic", "turn_id": active, "vendor_id": "secret"})
                    elif behavior in {"agent", "mcp"}:
                        emit({
                            "type": "tool_started", "turn_id": active,
                            "action_id": "action-1",
                            "tool_name": "Agent" if behavior == "agent" else "mcp__evil__run",
                            "summary": "spawn", "path": None,
                        })
                    elif behavior == "oversize":
                        sys.stdout.write("x" * 10000 + "\\n")
                        sys.stdout.flush()
                    elif behavior == "interrupt":
                        pass
                    elif turn_number == 1:
                        emit({"type": "text_delta", "turn_id": active, "text": "Working"})
                        emit({
                            "type": "tool_started", "turn_id": active,
                            "action_id": "action-1", "tool_name": "Bash",
                            "summary": "command: pytest -q", "path": os.getcwd(),
                        })
                        emit({
                            "type": "approval_requested", "turn_id": active,
                            "request_id": "approval-1", "action_id": "action-1",
                            "preview": "command: pytest -q",
                            "proposed_changes": [], "grant_scope": "Bash:pytest -q",
                        })
                    else:
                        emit({"type": "text_delta", "turn_id": active, "text": "Second"})
                        emit({
                            "type": "turn_completed", "turn_id": active,
                            "status": "completed", "error": None,
                        })
                        active = None
                elif frame["type"] == "approval":
                    assert frame["request_id"] == "approval-1"
                    assert frame["decision"] in {"allow_once", "allow_session"}
                    emit({"type": "approval_accepted", "request_id": "approval-1"})
                    emit({
                        "type": "tool_completed", "turn_id": active,
                        "action_id": "action-1", "status": "completed",
                        "summary": "Tool completed", "output": "one passed", "diff": None,
                    })
                    emit({
                        "type": "turn_completed", "turn_id": active,
                        "status": "completed", "error": None,
                    })
                    active = None
                elif frame["type"] == "interrupt":
                    assert frame["turn_id"] == active
                    emit({
                        "type": "turn_completed", "turn_id": active,
                        "status": "interrupted", "error": None,
                    })
                    active = None
                elif frame["type"] == "close":
                    raise SystemExit(0)
                else:
                    raise AssertionError(frame)
            """
        ).replace("__BEHAVIOR__", repr(behavior)),
        encoding="utf-8",
    )
    return sidecar


async def _next(stream):
    return await asyncio.wait_for(anext(stream), timeout=2)


def _session(
    tmp_path: Path,
    *,
    behavior: str = "normal",
    max_frame_bytes: int = 256_000,
    host_env: dict[str, str] | None = None,
) -> ClaudeAgentSession:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return ClaudeAgentSession(
        working_directory=workspace,
        node_executable=sys.executable,
        sidecar_path=_fake_sidecar(tmp_path, behavior),
        sdk_path=_fake_sdk(tmp_path),
        request_timeout_seconds=2,
        shutdown_timeout_seconds=0.2,
        max_frame_bytes=max_frame_bytes,
        host_env=host_env,
    )


@pytest.mark.asyncio
async def test_pinned_node_sidecar_uses_isolated_sdk_and_correlates_permission(
    tmp_path: Path,
) -> None:
    sdk = _fake_sdk(tmp_path)
    (sdk / "sdk.mjs").write_text(
        dedent(
            """\
            export function query({ prompt, options }) {
              if (JSON.stringify(options.settingSources) !== "[]") throw new Error("settings");
              if (options.persistSession !== false) throw new Error("persistence");
              if (Object.keys(options.mcpServers).length !== 0) throw new Error("mcp");
              let turn = 0;
              const stream = (async function* () {
                for await (const message of prompt) {
                  turn += 1;
                  if (turn === 1) {
                    const input = { tool_name: "Bash", tool_input: { command: "pytest -q" } };
                    await options.hooks.PreToolUse[0].hooks[0](
                      input, "vendor-tool-secret", { signal: new AbortController().signal }
                    );
                    const permission = await options.canUseTool(
                      "Bash", input.tool_input,
                      { signal: new AbortController().signal, toolUseID: "vendor-tool-secret" }
                    );
                    if (permission.behavior !== "allow") throw new Error("permission denied");
                    yield {
                      type: "user", parent_tool_use_id: null,
                      message: { content: [{
                        type: "tool_result", tool_use_id: "vendor-tool-secret",
                        content: "one passed", is_error: false
                      }] }
                    };
                  }
                  yield {
                    type: "stream_event",
                    event: { type: "content_block_delta", delta: {
                      type: "text_delta", text: `answer-${turn}`
                    } }
                  };
                  yield { type: "result", subtype: "success", is_error: false };
                }
              })();
              stream.interrupt = async () => {};
              return stream;
            }
            """
        ),
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = ClaudeAgentSession(
        working_directory=workspace,
        node_executable="node",
        sidecar_path=Path(__file__).resolve().parents[1] / "scripts" / "claude-agent-session.mjs",
        sdk_path=sdk,
        request_timeout_seconds=2,
        shutdown_timeout_seconds=0.2,
    )
    await session.start()
    stream = session.events()
    await session.send_turn("run tests")
    assert isinstance(await _next(stream), TurnStartedEvent)
    tool = await _next(stream)
    requested = await _next(stream)
    assert isinstance(tool, ToolStartedEvent)
    assert isinstance(requested, ApprovalRequestedEvent)
    assert requested.approval.action_id == tool.action_id
    assert requested.approval.proposed_changes == ()
    assert requested.approval.grant_scope == "Bash:pytest -q"
    assert "vendor-tool-secret" not in requested.model_dump_json()
    await session.respond_approval(requested.approval.request_id, ApprovalDecision.ALLOW_ONCE)
    assert isinstance(await _next(stream), ApprovalResolvedEvent)
    assert isinstance(await _next(stream), ToolCompletedEvent)
    text = await _next(stream)
    assert isinstance(text, TextDeltaEvent) and text.text == "answer-1"
    assert isinstance(await _next(stream), TurnCompletedEvent)

    await session.send_turn("continue")
    assert isinstance(await _next(stream), TurnStartedEvent)
    text = await _next(stream)
    assert isinstance(text, TextDeltaEvent) and text.text == "answer-2"
    assert isinstance(await _next(stream), TurnCompletedEvent)
    await session.aclose()


@pytest.mark.asyncio
async def test_edit_preview_is_pre_execution_contained_and_context_usage_is_estimated(
    tmp_path: Path,
) -> None:
    sdk = _fake_sdk(tmp_path)
    (sdk / "sdk.mjs").write_text(
        dedent(
            """\
            import fs from "node:fs";
            import path from "node:path";

            export function query({ prompt, options }) {
              let turn = 0;
              const stream = (async function* () {
                for await (const message of prompt) {
                  turn += 1;
                  yield {
                    type: "system", subtype: "init", model: "claude-test"
                  };
                  const writing = turn === 2;
                  const file = path.join(options.cwd, writing ? "notes.txt" : "src/app.py");
                  const toolName = writing ? "Write" : "Edit";
                  const toolInput = writing ? { file_path: file, content: "hello\\n" } : {
                      file_path: file,
                      old_string: "value = 1",
                      new_string: "value = 2",
                      replace_all: false,
                  };
                  const input = { tool_name: toolName, tool_input: toolInput };
                  await options.hooks.PreToolUse[0].hooks[0](
                    input, "vendor-change", { signal: new AbortController().signal }
                  );
                  const permission = await options.canUseTool(
                    toolName, toolInput,
                    { signal: new AbortController().signal, toolUseID: "vendor-change" }
                  );
                  if (permission.behavior !== "allow") throw new Error(permission.message);
                  fs.writeFileSync(file, writing ? "hello\\n" : "value = 2\\n");
                  yield {
                    type: "user", parent_tool_use_id: null,
                    message: { content: [{
                      type: "tool_result", tool_use_id: "vendor-change",
                      content: "updated", is_error: false
                    }] }
                  };
                  yield {
                    type: "assistant", parent_tool_use_id: null,
                    message: {
                      model: "claude-test",
                      usage: {
                        input_tokens: 10, cache_read_input_tokens: 3,
                        cache_creation_input_tokens: 2, output_tokens: 4
                      },
                      content: [{ type: "text", text: "done" }]
                    }
                  };
                  yield {
                    type: "result", subtype: "success", is_error: false,
                    modelUsage: { "claude-test": { contextWindow: 200000 } }
                  };
                }
              })();
              stream.interrupt = async () => {};
              return stream;
            }
            """
        ),
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    changed = workspace / "src" / "app.py"
    changed.write_text("value = 1\n", encoding="utf-8")
    session = ClaudeAgentSession(
        working_directory=workspace,
        node_executable="node",
        sidecar_path=Path(__file__).resolve().parents[1] / "scripts" / "claude-agent-session.mjs",
        sdk_path=sdk,
        request_timeout_seconds=2,
        shutdown_timeout_seconds=0.2,
    )
    assert session.capabilities.token_usage
    assert session.capabilities.proposed_file_preview
    assert not session.capabilities.native_compaction
    await session.start()
    stream = session.events()
    await session.send_turn("edit")
    assert isinstance(await _next(stream), TurnStartedEvent)
    reported_model = await _next(stream)
    assert isinstance(reported_model, RuntimeModelUpdatedEvent)
    assert reported_model.model == "claude-test"
    started = await _next(stream)
    preview = await _next(stream)
    requested = await _next(stream)
    assert isinstance(started, ToolStartedEvent)
    assert isinstance(preview, ActionPreviewUpdatedEvent)
    assert isinstance(requested, ApprovalRequestedEvent)
    assert changed.read_text(encoding="utf-8") == "value = 1\n"
    proposal = preview.proposed_changes[0]
    assert proposal.paths == ("src/app.py",)
    assert "-value = 1" in (proposal.unified_diff or "")
    assert "+value = 2" in (proposal.unified_diff or "")
    assert requested.approval.proposed_changes == preview.proposed_changes
    assert requested.approval.grant_scope == "Edit:src/app.py"
    assert "vendor-change" not in requested.model_dump_json()

    await session.respond_approval(requested.approval.request_id, ApprovalDecision.ALLOW_ONCE)
    assert isinstance(await _next(stream), ApprovalResolvedEvent)
    assert isinstance(await _next(stream), ToolCompletedEvent)
    text = await _next(stream)
    usage = await _next(stream)
    terminal = await _next(stream)
    assert isinstance(text, TextDeltaEvent) and text.text == "done"
    assert isinstance(usage, ContextUsageUpdatedEvent)
    assert usage.telemetry.input_tokens == 15
    assert usage.telemetry.cached_input_tokens == 3
    assert usage.telemetry.output_tokens == 4
    assert usage.telemetry.total_tokens == 19
    assert usage.telemetry.context_window == 200_000
    assert isinstance(terminal, TurnCompletedEvent)
    assert changed.read_text(encoding="utf-8") == "value = 2\n"

    created = workspace / "notes.txt"
    await session.send_turn("write")
    assert isinstance(await _next(stream), TurnStartedEvent)
    assert isinstance(await _next(stream), RuntimeModelUpdatedEvent)
    assert isinstance(await _next(stream), ToolStartedEvent)
    write_preview = await _next(stream)
    write_requested = await _next(stream)
    assert isinstance(write_preview, ActionPreviewUpdatedEvent)
    assert isinstance(write_requested, ApprovalRequestedEvent)
    assert not created.exists()
    write_change = write_preview.proposed_changes[0]
    assert write_change.kind.value == "create"
    assert write_change.paths == ("notes.txt",)
    assert "+hello" in (write_change.unified_diff or "")
    assert write_requested.approval.grant_scope == "Write:notes.txt"
    assert ApprovalDecision.ALLOW_SESSION in write_requested.approval.available_decisions
    await session.respond_approval(
        write_requested.approval.request_id, ApprovalDecision.ALLOW_SESSION
    )
    resolved = await _next(stream)
    assert isinstance(resolved, ApprovalResolvedEvent)
    assert resolved.decision == ApprovalDecision.ALLOW_SESSION
    assert isinstance(await _next(stream), ToolCompletedEvent)
    assert isinstance(await _next(stream), TextDeltaEvent)
    assert isinstance(await _next(stream), ContextUsageUpdatedEvent)
    assert isinstance(await _next(stream), TurnCompletedEvent)
    assert created.read_text(encoding="utf-8") == "hello\n"
    await session.aclose()


@pytest.mark.asyncio
async def test_edit_preimage_change_while_waiting_is_denied(tmp_path: Path) -> None:
    sdk = _fake_sdk(tmp_path)
    (sdk / "sdk.mjs").write_text(
        dedent(
            """\
            import path from "node:path";
            export function query({ prompt, options }) {
              const stream = (async function* () {
                for await (const message of prompt) {
                  const input = { file_path: path.join(options.cwd, "app.py"),
                    old_string: "one", new_string: "two", replace_all: false };
                  await options.hooks.PreToolUse[0].hooks[0](
                    { tool_name: "Edit", tool_input: input }, "vendor-edit",
                    { signal: new AbortController().signal }
                  );
                  const permission = await options.canUseTool("Edit", input, {
                    signal: new AbortController().signal, toolUseID: "vendor-edit"
                  });
                  if (permission.behavior === "allow") throw new Error("stale edit was allowed");
                  yield { type: "result", subtype: "error_during_execution", is_error: true };
                }
              })();
              stream.interrupt = async () => {};
              return stream;
            }
            """
        ),
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    changed = workspace / "app.py"
    changed.write_text("one\n", encoding="utf-8")
    session = ClaudeAgentSession(
        working_directory=workspace,
        node_executable="node",
        sidecar_path=Path(__file__).resolve().parents[1] / "scripts" / "claude-agent-session.mjs",
        sdk_path=sdk,
        request_timeout_seconds=2,
        shutdown_timeout_seconds=0.2,
    )
    await session.start()
    stream = session.events()
    await session.send_turn("edit")
    requested = None
    while requested is None:
        event = await _next(stream)
        if isinstance(event, ApprovalRequestedEvent):
            requested = event
    changed.write_text("three\n", encoding="utf-8")
    await session.respond_approval(requested.approval.request_id, ApprovalDecision.ALLOW_ONCE)
    assert isinstance(await _next(stream), ApprovalResolvedEvent)
    failed_tool = await _next(stream)
    terminal = await _next(stream)
    assert isinstance(failed_tool, ToolCompletedEvent)
    assert failed_tool.status.value == "failed"
    assert isinstance(terminal, TurnCompletedEvent)
    assert terminal.status == RuntimeTurnStatus.FAILED
    assert changed.read_text(encoding="utf-8") == "three\n"
    await session.aclose()


@pytest.mark.asyncio
async def test_write_through_symlink_outside_workspace_is_denied_without_approval(
    tmp_path: Path,
) -> None:
    sdk = _fake_sdk(tmp_path)
    (sdk / "sdk.mjs").write_text(
        dedent(
            """\
            import path from "node:path";
            export function query({ prompt, options }) {
              const stream = (async function* () {
                for await (const message of prompt) {
                  const input = {
                    file_path: path.join(options.cwd, "escape.txt"), content: "unsafe\\n"
                  };
                  await options.hooks.PreToolUse[0].hooks[0](
                    { tool_name: "Write", tool_input: input }, "vendor-write",
                    { signal: new AbortController().signal }
                  );
                  const permission = await options.canUseTool("Write", input, {
                    signal: new AbortController().signal, toolUseID: "vendor-write"
                  });
                  if (permission.behavior === "allow") throw new Error("symlink write was allowed");
                  yield { type: "result", subtype: "error_during_execution", is_error: true };
                }
              })();
              stream.interrupt = async () => {};
              return stream;
            }
            """
        ),
        encoding="utf-8",
    )
    outside = tmp_path / "outside.txt"
    outside.write_text("safe\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "escape.txt").symlink_to(outside)
    session = ClaudeAgentSession(
        working_directory=workspace,
        node_executable="node",
        sidecar_path=Path(__file__).resolve().parents[1] / "scripts" / "claude-agent-session.mjs",
        sdk_path=sdk,
        request_timeout_seconds=2,
        shutdown_timeout_seconds=0.2,
    )
    await session.start()
    stream = session.events()
    await session.send_turn("escape")
    received = []
    while True:
        event = await _next(stream)
        received.append(event)
        if isinstance(event, TurnCompletedEvent):
            break
    assert not any(isinstance(event, ApprovalRequestedEvent) for event in received)
    assert received[-1].status == RuntimeTurnStatus.FAILED
    assert outside.read_text(encoding="utf-8") == "safe\n"
    await session.aclose()


@pytest.mark.asyncio
async def test_read_search_tools_are_approval_free_but_cannot_escape_workspace(
    tmp_path: Path,
) -> None:
    sdk = _fake_sdk(tmp_path)
    (sdk / "sdk.mjs").write_text(
        dedent(
            """\
            import path from "node:path";
            export function query({ prompt, options }) {
              let turn = 0;
              const stream = (async function* () {
                for await (const message of prompt) {
                  turn += 1;
                  const cases = [
                    ["Read", { file_path: path.join(options.cwd, "inside.txt") }, true],
                    ["Glob", { pattern: "*.txt" }, true],
                    ["Grep", { pattern: "safe", path: "." }, true],
                    ["Read", { file_path: "../outside.txt" }, false],
                    ["Glob", { pattern: "*", path: path.join(options.cwd, "escape") }, false],
                    ["Grep", { pattern: "safe", path: path.resolve(options.cwd, "../") }, false],
                  ];
                  const [toolName, toolInput, shouldAllow] = cases[turn - 1];
                  const vendorID = `vendor-read-${turn}`;
                  const hook = await options.hooks.PreToolUse[0].hooks[0](
                    { tool_name: toolName, tool_input: toolInput }, vendorID,
                    { signal: new AbortController().signal }
                  );
                  const deniedByHook =
                    hook?.hookSpecificOutput?.permissionDecision === "deny";
                  if (deniedByHook === shouldAllow) {
                    throw new Error("unexpected containment result");
                  }
                  if (shouldAllow) {
                    const permission = await options.canUseTool(toolName, toolInput, {
                      signal: new AbortController().signal, toolUseID: vendorID
                    });
                    if (permission.behavior !== "allow") throw new Error("contained read denied");
                    yield {
                      type: "user", parent_tool_use_id: null,
                      message: { content: [{
                        type: "tool_result", tool_use_id: vendorID,
                        content: "safe", is_error: false
                      }] }
                    };
                    yield { type: "result", subtype: "success", is_error: false };
                  } else {
                    yield {
                      type: "result", subtype: "error_during_execution", is_error: true
                    };
                  }
                }
              })();
              stream.interrupt = async () => {};
              return stream;
            }
            """
        ),
        encoding="utf-8",
    )
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    outside_dir = tmp_path / "outside-dir"
    outside_dir.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "inside.txt").write_text("safe\n", encoding="utf-8")
    (workspace / "escape").symlink_to(outside_dir, target_is_directory=True)
    session = ClaudeAgentSession(
        working_directory=workspace,
        node_executable="node",
        sidecar_path=Path(__file__).resolve().parents[1] / "scripts" / "claude-agent-session.mjs",
        sdk_path=sdk,
        request_timeout_seconds=2,
        shutdown_timeout_seconds=0.2,
    )
    await session.start()
    stream = session.events()
    for index in range(6):
        await session.send_turn(f"read case {index}")
        received = []
        while True:
            event = await _next(stream)
            received.append(event)
            if isinstance(event, TurnCompletedEvent):
                break
        assert not any(isinstance(event, ApprovalRequestedEvent) for event in received)
        expected = RuntimeTurnStatus.COMPLETED if index < 3 else RuntimeTurnStatus.FAILED
        assert received[-1].status == expected
    await session.aclose()


@pytest.mark.asyncio
async def test_long_lived_session_streams_approval_and_second_turn(tmp_path: Path) -> None:
    session = _session(tmp_path)
    await session.start()
    stream = session.events()
    first_turn = await session.send_turn("inspect then test")

    started = await _next(stream)
    text = await _next(stream)
    tool = await _next(stream)
    requested = await _next(stream)
    assert isinstance(started, TurnStartedEvent)
    assert isinstance(text, TextDeltaEvent) and text.text == "Working"
    assert isinstance(tool, ToolStartedEvent)
    assert tool.kind == RuntimeToolKind.COMMAND
    assert tool.effect == ToolEffect.EXECUTE
    assert isinstance(requested, ApprovalRequestedEvent)
    assert requested.approval.action_id == tool.action_id
    assert ApprovalDecision.ALLOW_SESSION in requested.approval.available_decisions
    assert "vendor" not in requested.model_dump_json()

    await session.respond_approval(requested.approval.request_id, ApprovalDecision.ALLOW_ONCE)
    assert isinstance(await _next(stream), ApprovalResolvedEvent)
    completed_tool = await _next(stream)
    completed_turn = await _next(stream)
    assert isinstance(completed_tool, ToolCompletedEvent)
    assert completed_tool.output == "one passed"
    assert isinstance(completed_turn, TurnCompletedEvent)
    assert completed_turn.status == RuntimeTurnStatus.COMPLETED

    second_turn = await session.send_turn("what happened?")
    assert second_turn != first_turn
    assert isinstance(await _next(stream), TurnStartedEvent)
    second_text = await _next(stream)
    assert isinstance(second_text, TextDeltaEvent) and second_text.text == "Second"
    assert isinstance(await _next(stream), TurnCompletedEvent)
    await session.aclose()


@pytest.mark.asyncio
async def test_session_approval_and_duplicate_response_fail_closed(tmp_path: Path) -> None:
    session = _session(tmp_path)
    await session.start()
    stream = session.events()
    await session.send_turn("run tests")
    requested = None
    while requested is None:
        event = await _next(stream)
        if isinstance(event, ApprovalRequestedEvent):
            requested = event
    await session.respond_approval(requested.approval.request_id, ApprovalDecision.ALLOW_SESSION)
    with pytest.raises(ConversationProtocolError, match="stale or duplicate"):
        await session.respond_approval(requested.approval.request_id, ApprovalDecision.ALLOW_ONCE)
    await session.aclose()


@pytest.mark.asyncio
async def test_interrupt_emits_correlated_terminal(tmp_path: Path) -> None:
    session = _session(tmp_path, behavior="interrupt")
    await session.start()
    stream = session.events()
    turn_id = await session.send_turn("wait")
    assert isinstance(await _next(stream), TurnStartedEvent)
    await session.interrupt(turn_id)
    terminal = await _next(stream)
    assert isinstance(terminal, TurnCompletedEvent)
    assert terminal.status == RuntimeTurnStatus.INTERRUPTED
    with pytest.raises(ConversationProtocolError, match="already terminal"):
        await session.interrupt(turn_id)
    await session.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("behavior", ["unknown", "agent", "mcp", "oversize"])
async def test_unknown_agent_or_oversized_frames_fail_closed(tmp_path: Path, behavior: str) -> None:
    session = _session(tmp_path, behavior=behavior, max_frame_bytes=2_000)
    await session.start()
    stream = session.events()
    await session.send_turn("break protocol")
    if behavior != "oversize":
        assert isinstance(await _next(stream), TurnStartedEvent)
    with pytest.raises(ConversationProtocolError):
        await _next(stream)
    await session.aclose()
    assert session._process is not None and session._process.returncode is not None


@pytest.mark.asyncio
async def test_aclose_reaps_sidecar_descendants_and_is_idempotent(tmp_path: Path) -> None:
    marker = tmp_path / "grandchild-finished"
    host_env = {"PATH": os.environ["PATH"], "RIVUMI_TEST_MARKER": str(marker)}
    # The production environment intentionally strips RIVUMI_TEST_MARKER, so put
    # the marker directly into the fake sidecar source for this lifecycle test.
    sidecar = _fake_sidecar(tmp_path, "descendant")
    source = sidecar.read_text(encoding="utf-8").replace(
        'marker = os.environ["RIVUMI_TEST_MARKER"]', f"marker = {str(marker)!r}"
    )
    sidecar.write_text(source, encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = ClaudeAgentSession(
        working_directory=workspace,
        node_executable=sys.executable,
        sidecar_path=sidecar,
        sdk_path=_fake_sdk(tmp_path),
        shutdown_timeout_seconds=0.2,
        host_env=host_env,
    )
    await session.start()
    process = session._process
    assert process is not None and process.returncode is None
    await session.aclose()
    await session.aclose()
    await asyncio.sleep(1)
    assert process.returncode is not None
    assert not marker.exists()


def test_sdk_dependency_is_pinned_and_symlink_workspace_is_rejected(tmp_path: Path) -> None:
    sdk = _fake_sdk(tmp_path)
    package = json.loads((sdk / "package.json").read_text(encoding="utf-8"))
    package["version"] = "0.1.78"
    (sdk / "package.json").write_text(json.dumps(package), encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = ClaudeAgentSession(
        working_directory=workspace,
        node_executable=sys.executable,
        sidecar_path=_fake_sidecar(tmp_path),
        sdk_path=sdk,
    )
    with pytest.raises(FileNotFoundError, match="0.1.77"):
        session._resolve_sdk()

    symlink = tmp_path / "workspace-link"
    os.symlink(workspace, symlink)
    with pytest.raises(ValueError, match="symlink"):
        ClaudeAgentSession(working_directory=symlink)

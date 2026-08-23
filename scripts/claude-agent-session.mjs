#!/usr/bin/env node

// Pinned JSONL bridge for @anthropic-ai/claude-agent-sdk 0.1.77.
// Vendor session/tool identifiers stay in this process and never cross stdout.

import fs from "node:fs";
import crypto from "node:crypto";
import path from "node:path";
import readline from "node:readline";
import { pathToFileURL } from "node:url";

const SDK_VERSION = "0.1.77";
const MAX_FRAME_BYTES = 256_000;
const MAX_TEXT = 64_000;
const MAX_CHANGE_SOURCE_BYTES = 2_000_000;
const KNOWN_TOOLS = new Set(["Read", "Glob", "Grep", "Bash", "Edit", "Write"]);

function parseArgs(argv) {
  const result = {};
  for (let index = 0; index < argv.length; index += 2) {
    const key = argv[index];
    const value = argv[index + 1];
    if (!key?.startsWith("--") || value === undefined) throw new Error("invalid arguments");
    result[key.slice(2)] = value;
  }
  if (!result["sdk-path"] || !result.cwd) throw new Error("sdk-path and cwd are required");
  return result;
}

function emit(value) {
  const payload = JSON.stringify(value);
  if (Buffer.byteLength(payload) + 1 > MAX_FRAME_BYTES) {
    throw new Error("outbound frame exceeds bound");
  }
  process.stdout.write(`${payload}\n`);
}

function bounded(value, limit = MAX_TEXT) {
  if (typeof value !== "string") return "";
  return value.replaceAll("\0", "").slice(0, limit);
}

class AsyncInput {
  constructor() {
    this.values = [];
    this.waiters = [];
    this.closed = false;
  }
  push(value) {
    if (this.closed) throw new Error("input is closed");
    const waiter = this.waiters.shift();
    if (waiter) waiter({ value, done: false });
    else this.values.push(value);
  }
  close() {
    this.closed = true;
    for (const waiter of this.waiters.splice(0)) waiter({ value: undefined, done: true });
  }
  [Symbol.asyncIterator]() { return this; }
  next() {
    if (this.values.length) return Promise.resolve({ value: this.values.shift(), done: false });
    if (this.closed) return Promise.resolve({ value: undefined, done: true });
    return new Promise((resolve) => this.waiters.push(resolve));
  }
}

const args = parseArgs(process.argv.slice(2));
const sdkRoot = fs.realpathSync(args["sdk-path"]);
const packageJson = JSON.parse(fs.readFileSync(path.join(sdkRoot, "package.json"), "utf8"));
if (packageJson.name !== "@anthropic-ai/claude-agent-sdk" || packageJson.version !== SDK_VERSION) {
  throw new Error(`Claude Agent SDK ${SDK_VERSION} is required`);
}
const { query } = await import(pathToFileURL(path.join(sdkRoot, "sdk.mjs")).href);

let activeTurn = null;
let agentQuery = null;
let closing = false;
let nextAction = 1;
let nextApproval = 1;
let sawPartialText = false;
let interruptRequested = false;
let latestContextTelemetry = null;
let latestContextModel = null;
let reportedRuntimeModel = null;
let emittedRuntimeModelTurn = null;
let emittedRuntimeModel = null;
const input = new AsyncInput();
const abortController = new AbortController();
const actions = new Map(); // vendor toolUseID -> Rivumi-local action record
const pendingApprovals = new Map(); // Rivumi request ID -> resolver

function classify(toolName, toolInput) {
  if (!KNOWN_TOOLS.has(toolName) || toolName.startsWith("mcp__") || toolName === "Agent") {
    return null;
  }
  const candidatePath = toolInput?.file_path ?? toolInput?.path ?? null;
  const safePath = typeof candidatePath === "string" ? bounded(candidatePath, 4096) : null;
  let summary = toolName;
  if (toolName === "Bash") summary = `command: ${bounded(toolInput?.command, 15_990)}`;
  else if (safePath) summary = `${toolName.toLowerCase()}: ${safePath}`;
  else if (typeof toolInput?.pattern === "string") {
    summary = `${toolName.toLowerCase()}: ${bounded(toolInput.pattern, 15_990)}`;
  }
  return { toolName, summary: bounded(summary, 16_000), path: safePath };
}

function safeInteger(value, label) {
  if (!Number.isSafeInteger(value) || value < 0) throw new Error(`invalid ${label}`);
  return value;
}

function inclusiveUsage(usage) {
  if (!usage || typeof usage !== "object" || Array.isArray(usage)) return null;
  const uncached = safeInteger(usage.input_tokens ?? 0, "input token count");
  const cacheRead = safeInteger(usage.cache_read_input_tokens ?? 0, "cached token count");
  const cacheCreated = safeInteger(
    usage.cache_creation_input_tokens ?? 0,
    "cache creation token count",
  );
  const output = safeInteger(usage.output_tokens ?? 0, "output token count");
  const input = uncached + cacheRead + cacheCreated;
  if (!Number.isSafeInteger(input + output)) throw new Error("token count exceeds safe integer range");
  return {
    accuracy: "estimated",
    input_tokens: input,
    cached_input_tokens: cacheRead,
    output_tokens: output,
    reasoning_output_tokens: 0,
    total_tokens: input + output,
    context_window: null,
  };
}

function captureAssistantUsage(message) {
  const telemetry = inclusiveUsage(message?.message?.usage);
  if (telemetry) latestContextTelemetry = telemetry;
  if (typeof message?.message?.model === "string") {
    updateRuntimeModel(message.message.model);
    latestContextModel = reportedRuntimeModel;
  } else {
    latestContextModel = null;
  }
}

function updateRuntimeModel(value) {
  if (typeof value !== "string") throw new Error("runtime model is missing");
  const model = bounded(value, 256);
  if (!model.trim()) throw new Error("runtime model is invalid");
  reportedRuntimeModel = model;
  emitRuntimeModel();
}

function emitRuntimeModel() {
  if (
    !activeTurn ||
    !reportedRuntimeModel ||
    (emittedRuntimeModelTurn === activeTurn && emittedRuntimeModel === reportedRuntimeModel)
  ) return;
  emit({ type: "runtime_model_updated", turn_id: activeTurn, model: reportedRuntimeModel });
  emittedRuntimeModelTurn = activeTurn;
  emittedRuntimeModel = reportedRuntimeModel;
}

function contextTelemetryForResult(message) {
  if (!latestContextTelemetry) return null;
  let contextWindow = null;
  const modelUsage = message?.modelUsage;
  if (
    latestContextModel &&
    modelUsage &&
    typeof modelUsage === "object" &&
    !Array.isArray(modelUsage) &&
    modelUsage[latestContextModel]
  ) {
    contextWindow = safeInteger(
      modelUsage[latestContextModel].contextWindow,
      "context window",
    );
    if (contextWindow < 1 || latestContextTelemetry.total_tokens > contextWindow) {
      contextWindow = null;
    }
  }
  return { ...latestContextTelemetry, context_window: contextWindow };
}

function digest(value) {
  return crypto.createHash("sha256").update(value).digest("hex");
}

function containedTarget(candidate, { allowRoot = false, forbidFinalSymlink = true } = {}) {
  if (typeof candidate !== "string" || !candidate || candidate.includes("\0")) {
    throw new Error("file path is missing or invalid");
  }
  const root = fs.realpathSync(args.cwd);
  const target = path.resolve(root, candidate);
  const relative = path.relative(root, target);
  if (
    (!relative && !allowRoot) ||
    relative === ".." ||
    relative.startsWith(`..${path.sep}`) ||
    path.isAbsolute(relative)
  ) {
    throw new Error("file path is outside the isolated workspace");
  }
  let existing = target;
  while (!fs.existsSync(existing)) {
    const parent = path.dirname(existing);
    if (parent === existing) throw new Error("file path has no contained existing parent");
    existing = parent;
  }
  const realExisting = fs.realpathSync(existing);
  const realRelative = path.relative(root, realExisting);
  if (realRelative === ".." || realRelative.startsWith(`..${path.sep}`) || path.isAbsolute(realRelative)) {
    throw new Error("file path resolves outside the isolated workspace");
  }
  if (forbidFinalSymlink && fs.existsSync(target) && fs.lstatSync(target).isSymbolicLink()) {
    throw new Error("file access through symbolic links is forbidden");
  }
  return { target, relative: relative ? relative.split(path.sep).join("/") : "." };
}

function validateToolContainment(toolName, toolInput) {
  if (["Read", "Edit", "Write"].includes(toolName)) {
    containedTarget(toolInput?.file_path);
  } else if (["Glob", "Grep"].includes(toolName) && toolInput?.path !== undefined) {
    containedTarget(toolInput.path, { allowRoot: true, forbidFinalSymlink: false });
  }
}

function readTextFile(target, { missing = false } = {}) {
  let bytes;
  try {
    bytes = fs.readFileSync(target);
  } catch (error) {
    if (missing && error?.code === "ENOENT") return { exists: false, bytes: Buffer.alloc(0), text: "" };
    throw error;
  }
  if (bytes.length > MAX_CHANGE_SOURCE_BYTES) throw new Error("file is too large for a safe preview");
  if (bytes.includes(0)) throw new Error("binary file changes cannot be previewed safely");
  const text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  return { exists: true, bytes, text };
}

function unifiedDiff(relative, before, after) {
  const beforeLines = before ? before.replace(/\n$/, "").split("\n") : [];
  const afterLines = after ? after.replace(/\n$/, "").split("\n") : [];
  const lines = [
    `--- a/${relative}`,
    `+++ b/${relative}`,
    `@@ -${beforeLines.length ? 1 : 0},${beforeLines.length} +${afterLines.length ? 1 : 0},${afterLines.length} @@`,
    ...beforeLines.map((line) => `-${line}`),
    ...afterLines.map((line) => `+${line}`),
  ];
  const complete = `${lines.join("\n")}\n`;
  const originalBytes = Buffer.byteLength(complete);
  if (originalBytes <= MAX_TEXT) {
    return { text: complete, originalBytes, truncated: false };
  }
  const suffix = "\n… proposed diff truncated …\n";
  const budget = MAX_TEXT - Buffer.byteLength(suffix);
  let shown = Buffer.from(complete).subarray(0, budget).toString("utf8");
  while (Buffer.byteLength(shown) > budget) shown = shown.slice(0, -1);
  return { text: shown + suffix, originalBytes, truncated: true };
}

function exactGrantScope(toolName, toolInput, relative = null) {
  let material;
  if (relative) material = `${toolName}:${relative}`;
  else if (toolName === "Bash") {
    material = `Bash:${typeof toolInput?.command === "string" ? toolInput.command : ""}`;
  }
  else material = `${toolName}:${JSON.stringify(toolInput)}`;
  return material.length <= 4096 ? material : `${toolName}:sha256:${digest(material)}`;
}

function proposedChange(action, toolInput) {
  if (!["Edit", "Write"].includes(action.toolName)) return null;
  const location = containedTarget(toolInput?.file_path);
  const current = readTextFile(location.target, { missing: action.toolName === "Write" });
  let after;
  if (action.toolName === "Write") {
    if (typeof toolInput?.content !== "string") throw new Error("Write content is invalid");
    after = toolInput.content;
  } else {
    if (typeof toolInput?.old_string !== "string" || typeof toolInput?.new_string !== "string") {
      throw new Error("Edit replacement is invalid");
    }
    const occurrences = current.text.split(toolInput.old_string).length - 1;
    if (!toolInput.old_string || occurrences === 0) throw new Error("Edit preimage does not match");
    if (!toolInput.replace_all && occurrences !== 1) throw new Error("Edit preimage is ambiguous");
    after = toolInput.replace_all
      ? current.text.split(toolInput.old_string).join(toolInput.new_string)
      : current.text.replace(toolInput.old_string, toolInput.new_string);
  }
  if (Buffer.byteLength(after) > MAX_CHANGE_SOURCE_BYTES) {
    throw new Error("proposed file content is too large for a safe preview");
  }
  const rendered = unifiedDiff(location.relative, current.text, after);
  action.preimage = `${current.exists ? "1" : "0"}:${digest(current.bytes)}`;
  action.target = location.target;
  action.proposedChanges = [{
    change_id: `${action.actionID}-change`,
    action_id: action.actionID,
    kind: current.exists ? "update" : "create",
    paths: [location.relative],
    summary: `${action.toolName.toLowerCase()}: ${location.relative}`,
    unified_diff: rendered.text,
    original_diff_bytes: rendered.originalBytes,
    truncated: rendered.truncated,
  }];
  action.grantScope = exactGrantScope(action.toolName, toolInput, location.relative);
  return action.proposedChanges;
}

function preimageIsCurrent(action) {
  if (!action.preimage || !action.target) return true;
  try {
    const location = containedTarget(action.target);
    if (location.target !== action.target) return false;
    const current = readTextFile(action.target, { missing: true });
    return action.preimage === `${current.exists ? "1" : "0"}:${digest(current.bytes)}`;
  } catch {
    return false;
  }
}

function ensureAction(toolName, toolInput, vendorID) {
  if (!activeTurn || typeof vendorID !== "string") throw new Error("uncorrelated tool use");
  const classified = classify(toolName, toolInput);
  if (!classified) throw new Error("forbidden or unknown tool requested");
  let action = actions.get(vendorID);
  if (!action) {
    action = { ...classified, actionID: `action-${nextAction++}`, turnID: activeTurn };
    actions.set(vendorID, action);
    emit({
      type: "tool_started",
      turn_id: activeTurn,
      action_id: action.actionID,
      tool_name: action.toolName,
      summary: action.summary,
      path: action.path,
    });
  }
  return action;
}

async function preToolUse(inputValue, vendorID) {
  const toolName = inputValue.tool_name;
  const toolInput = inputValue.tool_input ?? {};
  try {
    validateToolContainment(toolName, toolInput);
    ensureAction(toolName, toolInput, vendorID);
  } catch (error) {
    return {
      hookSpecificOutput: {
        hookEventName: "PreToolUse",
        permissionDecision: "deny",
        permissionDecisionReason: bounded(error?.message || "Unsafe file access"),
      },
    };
  }
  return {};
}

async function canUseTool(toolName, toolInput, options) {
  let action;
  try {
    validateToolContainment(toolName, toolInput);
    action = ensureAction(toolName, toolInput, options.toolUseID);
  } catch (error) {
    return {
      behavior: "deny",
      message: bounded(error?.message || "Rivumi denied an unknown or forbidden tool"),
      interrupt: true,
    };
  }
  if (["Read", "Glob", "Grep"].includes(toolName)) {
    return { behavior: "allow", updatedInput: toolInput, toolUseID: options.toolUseID };
  }
  try {
    proposedChange(action, toolInput);
    action.grantScope ??= exactGrantScope(toolName, toolInput);
  } catch (error) {
    return { behavior: "deny", message: bounded(error?.message || "Unsafe file change"), interrupt: false };
  }
  const requestID = `approval-${nextApproval++}`;
  if (action.proposedChanges) {
    emit({
      type: "action_preview_updated",
      turn_id: activeTurn,
      action_id: action.actionID,
      proposed_changes: action.proposedChanges,
    });
  }
  emit({
    type: "approval_requested",
    turn_id: activeTurn,
    request_id: requestID,
    action_id: action.actionID,
    preview: action.summary,
    proposed_changes: action.proposedChanges ?? [],
    grant_scope: action.grantScope,
  });
  const decision = await new Promise((resolve, reject) => {
    pendingApprovals.set(requestID, { resolve, reject });
    options.signal.addEventListener("abort", () => reject(new Error("approval aborted")), {
      once: true,
    });
  });
  if (decision === "allow_once" || decision === "allow_session") {
    if (!preimageIsCurrent(action)) {
      return { behavior: "deny", message: "File changed while awaiting approval", interrupt: false };
    }
    return { behavior: "allow", updatedInput: toolInput, toolUseID: options.toolUseID };
  }
  return {
    behavior: "deny",
    message: decision === "cancel" ? "User cancelled the turn" : "User denied this tool",
    interrupt: decision === "cancel",
  };
}

function toolResult(message) {
  const content = message?.message?.content;
  if (!Array.isArray(content)) return;
  for (const block of content) {
    if (block?.type !== "tool_result" || typeof block.tool_use_id !== "string") continue;
    const action = actions.get(block.tool_use_id);
    if (!action) throw new Error("tool result has no matching start");
    let output = null;
    if (typeof block.content === "string") output = bounded(block.content);
    else if (Array.isArray(block.content)) {
      output = bounded(
        block.content
          .filter((item) => item?.type === "text" && typeof item.text === "string")
          .map((item) => item.text)
          .join(""),
      );
    }
    emit({
      type: "tool_completed",
      turn_id: action.turnID,
      action_id: action.actionID,
      status: block.is_error ? "failed" : "completed",
      summary: block.is_error ? "Tool failed" : "Tool completed",
      output,
      diff: null,
    });
    actions.delete(block.tool_use_id);
  }
}

function assistantFallback(message) {
  captureAssistantUsage(message);
  if (sawPartialText) return;
  const content = message?.message?.content;
  if (!Array.isArray(content)) return;
  for (const block of content) {
    if (block?.type === "text" && typeof block.text === "string" && block.text) {
      emit({ type: "text_delta", turn_id: activeTurn, text: bounded(block.text) });
    } else if (block?.type === "tool_use") {
      ensureAction(block.name, block.input ?? {}, block.id);
    } else if (block?.type === "mcp_tool_use" || block?.type === "server_tool_use") {
      throw new Error("MCP/server tools are forbidden");
    }
  }
}

async function consumeSdkMessages() {
  agentQuery = query({
    prompt: input,
    options: {
      cwd: fs.realpathSync(args.cwd),
      model: args.model,
      settingSources: [],
      persistSession: false,
      includePartialMessages: true,
      tools: ["Read", "Glob", "Grep", "Bash", "Edit", "Write"],
      allowedTools: ["Read", "Glob", "Grep"],
      disallowedTools: ["Agent", "Task", "WebFetch", "WebSearch"],
      mcpServers: {},
      strictMcpConfig: true,
      permissionMode: "default",
      abortController,
      canUseTool,
      hooks: { PreToolUse: [{ matcher: "*", hooks: [preToolUse] }] },
    },
  });
  emit({ type: "ready", sdk_version: SDK_VERSION, setting_sources: [] });
  for await (const message of agentQuery) {
    if (message.type === "stream_event") {
      const event = message.event;
      if (event?.type === "content_block_delta" && event.delta?.type === "text_delta") {
        const text = bounded(event.delta.text);
        if (text) {
          sawPartialText = true;
          emit({ type: "text_delta", turn_id: activeTurn, text });
        }
      }
    } else if (message.type === "assistant") {
      if (message.parent_tool_use_id !== null) throw new Error("subagent output is forbidden");
      assistantFallback(message);
    } else if (message.type === "user") {
      toolResult(message);
    } else if (message.type === "result") {
      const success = message.subtype === "success" && message.is_error === false;
      const terminalStatus = interruptRequested ? "interrupted" : success ? "completed" : "failed";
      for (const [vendorID, action] of [...actions.entries()]) {
        if (action.turnID !== activeTurn) continue;
        if (success && !interruptRequested) throw new Error("successful tool has no result");
        emit({
          type: "tool_completed",
          turn_id: action.turnID,
          action_id: action.actionID,
          status: interruptRequested ? "interrupted" : "failed",
          summary: interruptRequested ? "Tool interrupted" : "Tool failed",
          output: null,
          diff: null,
        });
        actions.delete(vendorID);
      }
      const telemetry = contextTelemetryForResult(message);
      if (telemetry) emit({ type: "context_usage_updated", turn_id: activeTurn, telemetry });
      emit({
        type: "turn_completed",
        turn_id: activeTurn,
        status: terminalStatus,
        error: terminalStatus === "failed" ? "Claude turn failed" : null,
      });
      activeTurn = null;
      sawPartialText = false;
      interruptRequested = false;
      latestContextTelemetry = null;
      latestContextModel = null;
    } else if (message.type === "system") {
      if (message.subtype === "init") updateRuntimeModel(message.model);
    } else if (message.type === "tool_progress" || message.type === "auth_status") {
      // Explicitly ignored: these frames contain vendor IDs or opaque metadata.
    } else {
      throw new Error("unknown SDK message type");
    }
  }
}

const sdkTask = consumeSdkMessages().catch((error) => {
  if (!closing) {
    emit({
      type: "fatal",
      turn_id: activeTurn ?? "session",
      error: "Claude Agent SDK failed",
    });
  }
  process.exitCode = 1;
});

const reader = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
reader.on("line", async (line) => {
  try {
    if (Buffer.byteLength(line) + 1 > MAX_FRAME_BYTES) throw new Error("input frame too large");
    const frame = JSON.parse(line);
    if (!frame || typeof frame !== "object" || Array.isArray(frame)) throw new Error("bad frame");
    if (frame.type === "turn") {
      if (activeTurn) throw new Error("turn already active");
      if (typeof frame.turn_id !== "string" || typeof frame.text !== "string") {
        throw new Error("invalid turn");
      }
      activeTurn = frame.turn_id;
      sawPartialText = false;
      interruptRequested = false;
      emit({ type: "turn_accepted", turn_id: activeTurn });
      emitRuntimeModel();
      input.push({
        type: "user",
        message: { role: "user", content: frame.text },
        parent_tool_use_id: null,
        session_id: "",
      });
    } else if (frame.type === "approval") {
      const pending = pendingApprovals.get(frame.request_id);
      if (!pending || !["allow_once", "allow_session", "deny", "cancel"].includes(frame.decision)) {
        throw new Error("stale or invalid approval");
      }
      pendingApprovals.delete(frame.request_id);
      emit({ type: "approval_accepted", request_id: frame.request_id });
      pending.resolve(frame.decision);
    } else if (frame.type === "interrupt") {
      if (frame.turn_id !== activeTurn || !agentQuery) throw new Error("invalid interrupt");
      interruptRequested = true;
      await agentQuery.interrupt();
    } else if (frame.type === "close") {
      closing = true;
      input.close();
      abortController.abort();
      await agentQuery?.return?.();
      reader.close();
    } else {
      throw new Error("unknown input frame");
    }
  } catch (error) {
    emit({
      type: "fatal",
      turn_id: activeTurn ?? "session",
      error: "Claude sidecar protocol failed",
    });
    process.exitCode = 1;
    input.close();
    abortController.abort();
    await agentQuery?.return?.();
    reader.close();
  }
});

await sdkTask;

import { DurableObject } from "cloudflare:workers";

export type RunSessionStatus = "queued" | "running" | "completed" | "failed" | "cancelled";

export interface RunSessionRequestSummary {
  instruction: string;
  model: string;
  allowedPaths: string[];
  checks: string[][];
  fileCount: number;
}

export interface RunSessionExecution {
  success: boolean;
  exitCode: number;
}

export interface RunSessionPendingApproval {
  requestId: string;
  actionId: string;
  effect: string;
  reason: string;
  policyReason: string;
  preview: string;
  requestedAt: number;
}

export interface RunSessionApprovalDecision {
  requestId: string;
  decision: string;
  decidedAt: number;
}

export interface RunSessionSnapshot {
  runId: string;
  status: RunSessionStatus;
  model: string;
  createdAt: number;
  updatedAt: number;
  request: RunSessionRequestSummary;
  cancelRequested: boolean;
  execution?: RunSessionExecution;
  summary?: string;
  terminalReason?: string;
  error?: string;
  artifactKeys?: string[];
  pendingApprovals?: RunSessionPendingApproval[];
  approvalDecisions?: RunSessionApprovalDecision[];
}

interface RunSessionRecord extends RunSessionSnapshot {
  output?: Record<string, unknown>;
  artifacts?: Record<string, string>;
  eventLines?: string[];
  eventBytes?: number;
}

interface RunSessionSubscriber {
  controller: ReadableStreamDefaultController<Uint8Array>;
  heartbeatId: ReturnType<typeof setInterval>;
}

const MAX_LIVE_EVENT_LINES = 10_000;
const MAX_LIVE_EVENT_BYTES = 1_000_000;
const MAX_LIVE_EVENT_LINE_BYTES = 64_000;
const MAX_PENDING_APPROVALS = 32;
const MAX_APPROVAL_DECISIONS = 256;
const APPROVAL_DECISIONS = new Set(["allow_once", "allow_session", "deny", "cancel"]);
const SSE_HEARTBEAT_MS = 15_000;
const encoder = new TextEncoder();

function json(value: unknown, status = 200): Response {
  return Response.json(value, {
    status,
    headers: { "cache-control": "no-store", "x-content-type-options": "nosniff" },
  });
}

function text(value: string, status = 200, contentType = "text/plain; charset=utf-8"): Response {
  return new Response(value, {
    status,
    headers: {
      "cache-control": "no-store",
      "content-type": contentType,
      "x-content-type-options": "nosniff",
    },
  });
}

function sseHeaders(): HeadersInit {
  return {
    "cache-control": "no-store",
    "content-type": "text/event-stream; charset=utf-8",
    "x-content-type-options": "nosniff",
  };
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

async function parseBody(request: Request): Promise<Record<string, unknown> | null> {
  try {
    const value: unknown = await request.json();
    return isObject(value) ? value : null;
  } catch {
    return null;
  }
}

function stringArray(value: unknown): string[] | null {
  return Array.isArray(value) && value.every((item) => typeof item === "string")
    ? value
    : null;
}

function stringMatrix(value: unknown): string[][] | null {
  return Array.isArray(value) &&
    value.every((row) => Array.isArray(row) && row.every((item) => typeof item === "string"))
    ? value
    : null;
}

function recordFromCreateBody(body: Record<string, unknown>): RunSessionRecord | null {
  const request = isObject(body.request) ? body.request : null;
  const allowedPaths = request === null ? null : stringArray(request.allowedPaths);
  const checks = request === null ? null : stringMatrix(request.checks);
  if (
    typeof body.runId !== "string" ||
    typeof body.model !== "string" ||
    typeof body.createdAt !== "number" ||
    !Number.isInteger(body.createdAt) ||
    request === null ||
    typeof request.instruction !== "string" ||
    request.instruction.length > 32_000 ||
    allowedPaths === null ||
    checks === null ||
    typeof request.fileCount !== "number" ||
    !Number.isInteger(request.fileCount) ||
    request.fileCount < 0
  ) {
    return null;
  }
  return {
    runId: body.runId,
    status: "queued",
    model: body.model,
    createdAt: body.createdAt,
    updatedAt: body.createdAt,
    request: {
      instruction: request.instruction,
      model: body.model,
      allowedPaths,
      checks,
      fileCount: request.fileCount,
    },
    cancelRequested: false,
  };
}

function snapshot(record: RunSessionRecord): RunSessionSnapshot {
  const {
    output: _output,
    artifacts: _artifacts,
    eventLines: _eventLines,
    eventBytes: _eventBytes,
    ...visible
  } = record;
  return visible;
}

function terminalStatus(record: RunSessionRecord): boolean {
  return record.status === "completed" || record.status === "failed" || record.status === "cancelled";
}

function validateEventLines(value: unknown, expectedRunId: string): string[] | null {
  if (!Array.isArray(value) || value.length < 1 || value.length > 128) return null;
  const lines: string[] = [];
  for (const item of value) {
    if (
      typeof item !== "string" ||
      item.length < 2 ||
      encoder.encode(item).byteLength > MAX_LIVE_EVENT_LINE_BYTES
    ) {
      return null;
    }
    if (!item.endsWith("\n") || item.slice(0, -1).includes("\n") || item.includes("\0")) {
      return null;
    }
    let parsed: unknown;
    try {
      parsed = JSON.parse(item);
    } catch {
      return null;
    }
    if (
      !isObject(parsed) ||
      typeof parsed.event_type !== "string" ||
      !parsed.event_type ||
      typeof parsed.run_id !== "string" ||
      !parsed.run_id ||
      parsed.task_id !== expectedRunId ||
      typeof parsed.sequence !== "number" ||
      !Number.isInteger(parsed.sequence) ||
      parsed.sequence < 0 ||
      (parsed.data !== undefined && !isObject(parsed.data))
    ) {
      return null;
    }
    lines.push(item);
  }
  return lines;
}

function lastEventId(request: Request): number | undefined {
  const value = request.headers.get("last-event-id");
  if (value === null || !/^(0|[1-9][0-9]*)$/u.test(value)) return undefined;
  const sequence = Number(value);
  return Number.isSafeInteger(sequence) ? sequence : undefined;
}

function eventSequence(value: unknown): number | undefined {
  return isObject(value) && typeof value.sequence === "number" && Number.isInteger(value.sequence)
    ? value.sequence
    : undefined;
}

function approvalRequestFromEvent(
  parsed: Record<string, unknown>,
  requestedAt: number,
): RunSessionPendingApproval | null {
  const data = isObject(parsed.data) ? parsed.data : {};
  if (
    typeof data.request_id !== "string" ||
    typeof data.action_id !== "string" ||
    typeof data.effect !== "string" ||
    typeof data.reason !== "string"
  ) {
    return null;
  }
  return {
    requestId: data.request_id,
    actionId: data.action_id,
    effect: data.effect,
    reason: data.reason,
    policyReason: typeof data.policy_reason === "string" ? data.policy_reason.slice(0, 2_000) : "",
    preview: typeof data.preview === "string" ? data.preview.slice(0, 16_000) : "",
    requestedAt,
  };
}

function approvalRequestIdFromEvent(parsed: Record<string, unknown>): string | null {
  const data = isObject(parsed.data) ? parsed.data : {};
  return typeof data.request_id === "string" && data.request_id ? data.request_id : null;
}

function applyApprovalEvents(record: RunSessionRecord, lines: string[], now: number): boolean {
  let changed = false;
  let pending = record.pendingApprovals ?? [];
  for (const line of lines) {
    let parsed: unknown;
    try {
      parsed = JSON.parse(line);
    } catch {
      continue;
    }
    if (!isObject(parsed)) continue;
    if (parsed.event_type === "approval.requested") {
      const approval = approvalRequestFromEvent(parsed, now);
      if (approval === null) continue;
      pending = pending.filter((item) => item.requestId !== approval.requestId);
      pending.push(approval);
      if (pending.length > MAX_PENDING_APPROVALS) pending = pending.slice(-MAX_PENDING_APPROVALS);
      changed = true;
    } else if (
      parsed.event_type === "approval.resolved" ||
      parsed.event_type === "approval.abandoned"
    ) {
      const requestId = approvalRequestIdFromEvent(parsed);
      if (requestId === null) continue;
      pending = pending.filter((item) => item.requestId !== requestId);
      changed = true;
    }
  }
  if (changed) {
    record.pendingApprovals = pending;
  }
  return changed;
}

function validateApprovalDecisionBody(body: Record<string, unknown> | null): string | null {
  const decision = body?.decision;
  return typeof decision === "string" && APPROVAL_DECISIONS.has(decision) ? decision : null;
}

function formatServerSentEvents(ndjson: string, afterSequence?: number): string {
  const frames: string[] = [];
  for (const line of ndjson.split("\n")) {
    if (line === "") continue;
    let parsed: unknown;
    try {
      parsed = JSON.parse(line);
    } catch {
      frames.push(`event: message\ndata: ${JSON.stringify(line)}\n\n`);
      continue;
    }
    const sequence = eventSequence(parsed);
    if (afterSequence !== undefined && sequence !== undefined && sequence <= afterSequence) {
      continue;
    }
    const eventName =
      isObject(parsed) && typeof parsed.event_type === "string" && parsed.event_type
        ? parsed.event_type
        : "message";
    frames.push(`${sequence === undefined ? "" : `id: ${sequence}\n`}event: ${eventName}\ndata: ${line}\n\n`);
  }
  return frames.join("");
}

export class RunSession extends DurableObject<unknown> {
  private readonly subscribers = new Map<number, RunSessionSubscriber>();
  private nextSubscriberId = 0;

  constructor(ctx: DurableObjectState, env: unknown) {
    super(ctx, env);
  }

  private streamEvents(record: RunSessionRecord, request: Request): Response {
    const subscriberId = this.nextSubscriberId;
    this.nextSubscriberId += 1;
    const events = record.eventLines?.join("") ?? record.artifacts?.events ?? "";
    const afterSequence = lastEventId(request);
    const terminal = terminalStatus(record);
    const subscribers = this.subscribers;
    const stream = new ReadableStream<Uint8Array>({
      start: (controller) => {
        const replay = formatServerSentEvents(events, afterSequence);
        if (replay.length > 0) controller.enqueue(encoder.encode(replay));
        if (terminal) {
          controller.close();
          return;
        }
        const heartbeatId = setInterval(() => {
          try {
            controller.enqueue(encoder.encode(": heartbeat\n\n"));
          } catch {
            const subscriber = subscribers.get(subscriberId);
            if (subscriber !== undefined) clearInterval(subscriber.heartbeatId);
            subscribers.delete(subscriberId);
          }
        }, SSE_HEARTBEAT_MS);
        subscribers.set(subscriberId, { controller, heartbeatId });
      },
      cancel: () => {
        const subscriber = subscribers.get(subscriberId);
        if (subscriber !== undefined) clearInterval(subscriber.heartbeatId);
        subscribers.delete(subscriberId);
      },
    });
    return new Response(stream, {
      headers: sseHeaders(),
    });
  }

  private broadcast(lines: string[]): void {
    if (this.subscribers.size === 0) return;
    const payload = encoder.encode(formatServerSentEvents(lines.join("")));
    if (payload.byteLength === 0) return;
    for (const [id, subscriber] of this.subscribers) {
      try {
        subscriber.controller.enqueue(payload);
      } catch {
        clearInterval(subscriber.heartbeatId);
        this.subscribers.delete(id);
      }
    }
  }

  private closeSubscribers(): void {
    for (const [id, subscriber] of this.subscribers) {
      try {
        subscriber.controller.close();
      } catch {
        // Client already disconnected or the stream was already closed.
      }
      clearInterval(subscriber.heartbeatId);
      this.subscribers.delete(id);
    }
  }

  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    const path = url.pathname;
    const now = Date.now();

    if (request.method === "POST" && path === "/create") {
      const body = await parseBody(request);
      const record = body === null ? null : recordFromCreateBody(body);
      if (record === null) return json({ error: "invalid_run_session_create" }, 400);
      let conflict = false;
      await this.ctx.storage.transaction(async (transaction) => {
        const existing = await transaction.get<RunSessionRecord>("session");
        if (existing !== undefined) {
          conflict = true;
          return;
        }
        await transaction.put("session", record);
      });
      if (conflict) return json({ error: "run_session_exists" }, 409);
      return json({ ok: true }, 201);
    }

    if (request.method === "POST" && path === "/running") {
      let missing = false;
      let illegal = false;
      await this.ctx.storage.transaction(async (transaction) => {
        const record = await transaction.get<RunSessionRecord>("session");
        if (record === undefined) {
          missing = true;
          return;
        }
        if (terminalStatus(record)) {
          illegal = true;
          return;
        }
        record.status = "running";
        record.updatedAt = now;
        await transaction.put("session", record);
      });
      if (missing) return json({ error: "run_not_found" }, 404);
      if (illegal) return json({ error: "illegal_run_transition" }, 409);
      return json({ ok: true });
    }

    if (request.method === "POST" && path === "/complete") {
      const body = await parseBody(request);
      if (
        body === null ||
        !isObject(body.execution) ||
        typeof body.execution.success !== "boolean" ||
        typeof body.execution.exitCode !== "number" ||
        !Number.isInteger(body.execution.exitCode) ||
        !isObject(body.output) ||
        !isObject(body.artifacts) ||
        !Object.values(body.artifacts).every((value) => typeof value === "string")
      ) {
        return json({ error: "invalid_run_session_complete" }, 400);
      }
      const executionSource = body.execution;
      if (!isObject(executionSource)) {
        return json({ error: "invalid_run_session_complete" }, 400);
      }
      const execution: RunSessionExecution = {
        success: executionSource.success as boolean,
        exitCode: executionSource.exitCode as number,
      };
      const output = body.output as Record<string, unknown>;
      const artifacts = body.artifacts as Record<string, string>;
      const result = isObject(output.result) ? output.result : {};
      const status =
        result.status === "completed" || result.status === "failed" || result.status === "cancelled"
          ? result.status
          : execution.success
            ? "completed"
            : "failed";
      let missing = false;
      let illegal = false;
      let completed = false;
      await this.ctx.storage.transaction(async (transaction) => {
        const record = await transaction.get<RunSessionRecord>("session");
        if (record === undefined) {
          missing = true;
          return;
        }
        if (terminalStatus(record)) {
          illegal = true;
          return;
        }
        record.status = status;
        record.updatedAt = now;
        record.execution = execution;
        if (typeof result.summary === "string") record.summary = result.summary;
        if (typeof result.terminal_reason === "string") {
          record.terminalReason = result.terminal_reason;
        }
        record.output = output;
        record.artifacts = {
          ...artifacts,
          events: (record.eventLines?.join("") || artifacts.events) ?? "",
        };
        record.pendingApprovals = [];
        record.artifactKeys = Object.keys(record.artifacts).sort();
        await transaction.put("session", record);
        completed = true;
      });
      if (missing) return json({ error: "run_not_found" }, 404);
      if (illegal) return json({ error: "illegal_run_transition" }, 409);
      if (completed) this.closeSubscribers();
      return json({ ok: true });
    }

    if (request.method === "POST" && path === "/append-events") {
      const body = await parseBody(request);
      if (body === null || !Array.isArray(body.lines)) {
        return json({ error: "invalid_run_session_events" }, 400);
      }
      let missing = false;
      let illegal = false;
      let malformed = false;
      let storedLines = 0;
      let acceptedLines: string[] = [];
      await this.ctx.storage.transaction(async (transaction) => {
        const record = await transaction.get<RunSessionRecord>("session");
        if (record === undefined) {
          missing = true;
          return;
        }
        if (terminalStatus(record)) {
          illegal = true;
          return;
        }
        const lines = validateEventLines(body.lines, record.runId);
        if (lines === null) {
          malformed = true;
          return;
        }
        const existing = record.eventLines ?? [];
        const existingBytes = record.eventBytes ?? 0;
        const newBytes = lines.reduce(
          (total, line) => total + encoder.encode(line).byteLength,
          0,
        );
        if (
          existing.length + lines.length > MAX_LIVE_EVENT_LINES ||
          existingBytes + newBytes > MAX_LIVE_EVENT_BYTES
        ) {
          illegal = true;
          return;
        }
        record.eventLines = [...existing, ...lines];
        record.eventBytes = existingBytes + newBytes;
        storedLines = record.eventLines.length;
        acceptedLines = lines;
        applyApprovalEvents(record, lines, now);
        record.updatedAt = now;
        await transaction.put("session", record);
      });
      if (missing) return json({ error: "run_not_found" }, 404);
      if (malformed) return json({ error: "invalid_run_session_events" }, 400);
      if (illegal) return json({ error: "run_session_events_rejected" }, 409);
      this.broadcast(acceptedLines);
      return json({ ok: true, lines: storedLines });
    }

    if (request.method === "POST" && path === "/fail") {
      const body = await parseBody(request);
      if (body === null || typeof body.error !== "string" || body.error.length < 1) {
        return json({ error: "invalid_run_session_failure" }, 400);
      }
      const error = body.error;
      let missing = false;
      let failed = false;
      await this.ctx.storage.transaction(async (transaction) => {
        const record = await transaction.get<RunSessionRecord>("session");
        if (record === undefined) {
          missing = true;
          return;
        }
        if (!terminalStatus(record)) {
          record.status = "failed";
          failed = true;
        }
        record.pendingApprovals = [];
        record.error = error;
        record.updatedAt = now;
        await transaction.put("session", record);
      });
      if (missing) return json({ error: "run_not_found" }, 404);
      if (failed) this.closeSubscribers();
      return json({ ok: true });
    }

    if (request.method === "POST" && path === "/cancel") {
      let missing = false;
      let terminal = false;
      let cancelled = false;
      let status: RunSessionStatus = "cancelled";
      await this.ctx.storage.transaction(async (transaction) => {
        const record = await transaction.get<RunSessionRecord>("session");
        if (record === undefined) {
          missing = true;
          return;
        }
        record.cancelRequested = true;
        if (terminalStatus(record)) {
          terminal = true;
        } else {
          record.status = "cancelled";
          cancelled = true;
        }
        record.pendingApprovals = [];
        status = record.status;
        record.updatedAt = now;
        await transaction.put("session", record);
      });
      if (missing) return json({ error: "run_not_found" }, 404);
      if (cancelled) this.closeSubscribers();
      return json({ ok: true, status }, terminal ? 200 : 202);
    }

    if (request.method === "GET" && path === "/status") {
      const record = await this.ctx.storage.get<RunSessionRecord>("session");
      if (record === undefined) return json({ error: "run_not_found" }, 404);
      return json(snapshot(record));
    }

    if (request.method === "GET" && path === "/events") {
      const record = await this.ctx.storage.get<RunSessionRecord>("session");
      if (record === undefined) return json({ error: "run_not_found" }, 404);
      const events = record.eventLines?.join("") ?? record.artifacts?.events ?? "";
      if (url.searchParams.get("stream") === "1" || url.searchParams.get("stream") === "true") {
        return this.streamEvents(record, request);
      }
      return text(events, 200, "application/x-ndjson; charset=utf-8");
    }

    if (request.method === "GET" && path === "/approvals") {
      const record = await this.ctx.storage.get<RunSessionRecord>("session");
      if (record === undefined) return json({ error: "run_not_found" }, 404);
      return json({
        pending: record.pendingApprovals ?? [],
        decisions: record.approvalDecisions ?? [],
      });
    }

    const approvalMatch = /^\/approvals\/([A-Za-z0-9_-]+)$/u.exec(path);
    if (request.method === "POST" && approvalMatch !== null) {
      const body = await parseBody(request);
      const decision = validateApprovalDecisionBody(body);
      if (decision === null) return json({ error: "invalid_approval_decision" }, 400);
      const requestId = approvalMatch[1]!;
      let missing = false;
      let absent = false;
      let terminal = false;
      await this.ctx.storage.transaction(async (transaction) => {
        const record = await transaction.get<RunSessionRecord>("session");
        if (record === undefined) {
          missing = true;
          return;
        }
        if (terminalStatus(record)) {
          terminal = true;
          return;
        }
        const pending = record.pendingApprovals ?? [];
        if (!pending.some((approval) => approval.requestId === requestId)) {
          absent = true;
          return;
        }
        record.pendingApprovals = pending.filter((approval) => approval.requestId !== requestId);
        record.approvalDecisions = [
          ...(record.approvalDecisions ?? []),
          { requestId, decision, decidedAt: now },
        ].slice(-MAX_APPROVAL_DECISIONS);
        record.updatedAt = now;
        await transaction.put("session", record);
      });
      if (missing) return json({ error: "run_not_found" }, 404);
      if (terminal) return json({ error: "run_is_terminal" }, 409);
      if (absent) return json({ error: "approval_not_found" }, 404);
      return json({ ok: true, requestId, decision });
    }

    const artifactMatch = /^\/artifacts\/([A-Za-z0-9_]+)$/u.exec(path);
    if (request.method === "GET" && artifactMatch !== null) {
      const record = await this.ctx.storage.get<RunSessionRecord>("session");
      if (record === undefined) return json({ error: "run_not_found" }, 404);
      const artifact = record.artifacts?.[artifactMatch[1]!];
      if (artifact === undefined) return json({ error: "artifact_not_found" }, 404);
      return text(artifact);
    }

    return json({ error: "not_found" }, 404);
  }
}

function stub(namespace: DurableObjectNamespace<RunSession>, runId: string): DurableObjectStub {
  return namespace.get(namespace.idFromName(runId));
}

export async function createRunSession(
  namespace: DurableObjectNamespace<RunSession>,
  runId: string,
  request: RunSessionRequestSummary,
  createdAt: number,
): Promise<void> {
  const response = await stub(namespace, runId).fetch("https://run-session.internal/create", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ runId, model: request.model, request, createdAt }),
  });
  if (response.status !== 201) throw new Error("run session creation failed");
}

export async function markRunSessionRunning(
  namespace: DurableObjectNamespace<RunSession>,
  runId: string,
): Promise<void> {
  const response = await stub(namespace, runId).fetch("https://run-session.internal/running", {
    method: "POST",
  });
  if (response.status !== 200) throw new Error("run session transition failed");
}

export async function completeRunSession(
  namespace: DurableObjectNamespace<RunSession>,
  runId: string,
  execution: RunSessionExecution,
  output: Record<string, unknown>,
): Promise<void> {
  const artifacts = isObject(output.artifacts) ? output.artifacts : {};
  const response = await stub(namespace, runId).fetch("https://run-session.internal/complete", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ execution, output, artifacts }),
  });
  if (response.status !== 200) throw new Error("run session completion failed");
}

export async function appendRunSessionEvents(
  namespace: DurableObjectNamespace<RunSession>,
  runId: string,
  lines: string[],
): Promise<void> {
  const response = await stub(namespace, runId).fetch("https://run-session.internal/append-events", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ lines }),
  });
  if (response.status !== 200) {
    throw new Error("run session event append failed");
  }
}

export async function failRunSession(
  namespace: DurableObjectNamespace<RunSession>,
  runId: string,
  error: string,
): Promise<void> {
  const response = await stub(namespace, runId).fetch("https://run-session.internal/fail", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ error }),
  });
  if (response.status !== 200 && response.status !== 404) {
    throw new Error("run session failure recording failed");
  }
}

export async function cancelRunSession(
  namespace: DurableObjectNamespace<RunSession>,
  runId: string,
): Promise<{ status: RunSessionStatus; terminal: boolean }> {
  const response = await stub(namespace, runId).fetch("https://run-session.internal/cancel", {
    method: "POST",
  });
  if (response.status === 404) throw new Error("run not found");
  if (response.status !== 200 && response.status !== 202) {
    throw new Error("run session cancellation failed");
  }
  const body = (await response.json()) as { status?: RunSessionStatus };
  return { status: body.status ?? "cancelled", terminal: response.status === 200 };
}

export async function getRunSession(
  namespace: DurableObjectNamespace<RunSession>,
  runId: string,
): Promise<Response> {
  return await stub(namespace, runId).fetch("https://run-session.internal/status");
}

export async function getRunSessionEvents(
  namespace: DurableObjectNamespace<RunSession>,
  runId: string,
  stream = false,
  lastEventId?: string,
): Promise<Response> {
  const headers = new Headers();
  if (lastEventId !== undefined) headers.set("last-event-id", lastEventId);
  return await stub(namespace, runId).fetch(
    `https://run-session.internal/events${stream ? "?stream=1" : ""}`,
    { headers },
  );
}

export async function getRunSessionArtifact(
  namespace: DurableObjectNamespace<RunSession>,
  runId: string,
  name: string,
): Promise<Response> {
  return await stub(namespace, runId).fetch(`https://run-session.internal/artifacts/${name}`);
}

export async function getRunSessionApprovals(
  namespace: DurableObjectNamespace<RunSession>,
  runId: string,
): Promise<Response> {
  return await stub(namespace, runId).fetch("https://run-session.internal/approvals");
}

export async function submitRunSessionApproval(
  namespace: DurableObjectNamespace<RunSession>,
  runId: string,
  approvalId: string,
  decision: string,
): Promise<Response> {
  return await stub(namespace, runId).fetch(
    `https://run-session.internal/approvals/${approvalId}`,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ decision }),
    },
  );
}

import type { Sandbox } from "@cloudflare/sandbox";
import type {
  CapabilityConsumeResult,
  RunCapability as RunCapabilityDO,
} from "./capability-do";
import type {
  RunSession as RunSessionDO,
  RunSessionExecution,
  RunSessionRequestSummary,
} from "./run-session-do";

export const LIMITS = {
  requestBytes: 768_000,
  fileCount: 32,
  fileBytes: 64_000,
  sourceBytes: 512_000,
  instructionChars: 32_000,
  allowedPaths: 32,
  checks: 4,
  modelBodyBytes: 256_000,
  modelResponseBytes: 1_000_000,
  eventAppendBodyBytes: 128_000,
  eventAppendLines: 128,
  eventLineBytes: 64_000,
  liveEventBytes: 1_000_000,
  liveEventLines: 10_000,
  runResponseBytes: 1_500_000,
  runTokenSeconds: 300,
  sandboxTimeoutMs: 240_000,
  destroyTimeoutMs: 5_000,
} as const;

export const ALLOWED_CHECK_ARGV = [
  ["git", "diff", "--check"],
  ["python3", "-m", "pytest", "-q"],
  ["python3", "-m", "compileall", "-q", "."],
  ["python3", "-m", "unittest", "discover"],
] as const;

const TEXT_EXTENSIONS = new Set([
  ".py",
  ".toml",
  ".md",
  ".txt",
  ".json",
  ".yaml",
  ".yml",
  ".ini",
  ".cfg",
]);
const FIXED_COMMAND = "/usr/local/bin/rivumi-sandbox-run";
const encoder = new TextEncoder();

export interface Env {
  Sandbox: DurableObjectNamespace<Sandbox>;
  RUN_CAPABILITIES: DurableObjectNamespace<RunCapabilityDO>;
  RUN_SESSIONS: DurableObjectNamespace<RunSessionDO>;
  CONTROL_PLANE_TOKEN: string;
  RUN_TOKEN_SECRET: string;
  OPENAI_API_KEY: string;
  OPENAI_MODEL: string;
  MODEL_API_URL: string;
}

interface SourceFile {
  path: string;
  content: string;
}

interface ValidatedRun {
  instruction: string;
  model: string;
  files: SourceFile[];
  allowedPaths: string[];
  checks: string[][];
  limits: { maxSteps: number; wallTimeSeconds: number };
}

export interface SandboxHandle {
  mkdir(path: string, options?: { recursive?: boolean }): Promise<{ success: boolean }>;
  writeFile(path: string, content: string): Promise<{ success: boolean }>;
  exec(
    command: string,
    options?: { timeout?: number; cwd?: string; env?: Record<string, string | undefined> },
  ): Promise<{ success: boolean; exitCode: number; stdout: string; stderr: string }>;
  readFileStream(path: string): Promise<ReadableStream<Uint8Array>>;
  destroy(): Promise<void>;
}

export interface WorkerDependencies {
  getSandbox(env: Env, id: string): SandboxHandle;
  fetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response>;
  now(): number;
  randomUUID(): string;
  activateCapability(
    env: Env,
    runId: string,
    model: string,
    expiresAt: number,
    maxRequests: number,
  ): Promise<void>;
  checkCapability(env: Env, runId: string, model: string): Promise<CapabilityConsumeResult>;
  consumeCapability(env: Env, runId: string, model: string): Promise<CapabilityConsumeResult>;
  revokeCapability(env: Env, runId: string): Promise<void>;
  createRunSession(
    env: Env,
    runId: string,
    request: RunSessionRequestSummary,
    createdAt: number,
  ): Promise<void>;
  markRunSessionRunning(env: Env, runId: string): Promise<void>;
  completeRunSession(
    env: Env,
    runId: string,
    execution: RunSessionExecution,
    output: Record<string, unknown>,
  ): Promise<void>;
  appendRunSessionEvents(env: Env, runId: string, lines: string[]): Promise<void>;
  failRunSession(env: Env, runId: string, error: string): Promise<void>;
  cancelRunSession(
    env: Env,
    runId: string,
  ): Promise<{ status: string; terminal: boolean }>;
  getRunSession(env: Env, runId: string): Promise<Response>;
  getRunSessionEvents(
    env: Env,
    runId: string,
    stream?: boolean,
    lastEventId?: string,
  ): Promise<Response>;
  getRunSessionArtifact(env: Env, runId: string, name: string): Promise<Response>;
  getRunSessionApprovals(env: Env, runId: string): Promise<Response>;
  getRunSessionApproval(env: Env, runId: string, approvalId: string): Promise<Response>;
  submitRunSessionApproval(
    env: Env,
    runId: string,
    approvalId: string,
    decision: string,
  ): Promise<Response>;
  decodeFileStream(stream: ReadableStream<Uint8Array>): AsyncIterable<string | Uint8Array>;
  queueBackgroundRun(run: () => Promise<void>): void;
}

export class RequestProblem extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
  ) {
    super(code);
  }
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function rejectUnknownKeys(value: Record<string, unknown>, allowed: readonly string[]): void {
  const keys = Object.keys(value);
  if (keys.some((key) => !allowed.includes(key))) {
    throw new RequestProblem(400, "unknown_field");
  }
}

function utf8Bytes(value: string): number {
  return encoder.encode(value).byteLength;
}

function validateRelativeFilePath(value: unknown): string {
  if (typeof value !== "string" || value.length < 1 || value.length > 160) {
    throw new RequestProblem(400, "invalid_source_path");
  }
  if (value.includes("\\") || value.includes("\0") || value.startsWith("/")) {
    throw new RequestProblem(400, "invalid_source_path");
  }
  const parts = value.split("/");
  if (parts.some((part) => !part || part === "." || part === ".." || part === ".git")) {
    throw new RequestProblem(400, "invalid_source_path");
  }
  if (!parts.every((part) => /^[A-Za-z0-9._-]+$/.test(part))) {
    throw new RequestProblem(400, "invalid_source_path");
  }
  const filename = parts.at(-1)!;
  const dot = filename.lastIndexOf(".");
  const extension = dot >= 0 ? filename.slice(dot).toLowerCase() : "";
  if (!TEXT_EXTENSIONS.has(extension)) {
    throw new RequestProblem(400, "unsupported_source_type");
  }
  return value;
}

function validateAllowedPath(value: unknown, files: readonly SourceFile[]): string {
  if (typeof value !== "string" || value.length < 1 || value.length > 160) {
    throw new RequestProblem(400, "invalid_allowed_path");
  }
  if (value.includes("\\") || value.includes("\0") || value.startsWith("/")) {
    throw new RequestProblem(400, "invalid_allowed_path");
  }
  const isTree = value.endsWith("/**");
  const base = isTree ? value.slice(0, -3) : value;
  const parts = base.split("/");
  if (
    !base ||
    parts.some((part) => !part || part === "." || part === ".." || part === ".git") ||
    !parts.every((part) => /^[A-Za-z0-9._-]+$/.test(part))
  ) {
    throw new RequestProblem(400, "invalid_allowed_path");
  }
  const coversSource = isTree
    ? files.some((file) => file.path.startsWith(`${base}/`))
    : files.some((file) => file.path === base);
  if (!coversSource) throw new RequestProblem(400, "unbound_allowed_path");
  return value;
}

function argvAllowed(value: unknown): value is string[] {
  return (
    Array.isArray(value) &&
    value.every((part) => typeof part === "string") &&
    ALLOWED_CHECK_ARGV.some(
      (allowed) => allowed.length === value.length && allowed.every((part, i) => part === value[i]),
    )
  );
}

function validateRunIdPathSegment(value: string): string {
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/u.test(value)) {
    throw new RequestProblem(404, "run_not_found");
  }
  return value;
}

function validateArtifactName(value: string): string {
  if (!(ARTIFACT_KEYS as readonly string[]).includes(value)) {
    throw new RequestProblem(404, "artifact_not_found");
  }
  return value;
}

function validateEventAppendBody(value: unknown, expectedRunId: string): string[] {
  if (!isObject(value)) throw new RequestProblem(400, "invalid_event_append");
  rejectUnknownKeys(value, ["lines"]);
  if (
    !Array.isArray(value.lines) ||
    value.lines.length < 1 ||
    value.lines.length > LIMITS.eventAppendLines
  ) {
    throw new RequestProblem(400, "invalid_event_append");
  }
  const lines: string[] = [];
  for (const line of value.lines) {
    if (
      typeof line !== "string" ||
      line.length < 2 ||
      utf8Bytes(line) > LIMITS.eventLineBytes ||
      !line.endsWith("\n") ||
      line.slice(0, -1).includes("\n") ||
      line.includes("\0")
    ) {
      throw new RequestProblem(400, "invalid_event_append");
    }
    let parsed: unknown;
    try {
      parsed = JSON.parse(line);
    } catch {
      throw new RequestProblem(400, "invalid_event_append");
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
      throw new RequestProblem(400, "invalid_event_append");
    }
    lines.push(line);
  }
  return lines;
}

export function validateRunRequest(value: unknown, expectedModel: string): ValidatedRun {
  if (!isObject(value)) throw new RequestProblem(400, "invalid_json_object");
  rejectUnknownKeys(value, ["instruction", "model", "files", "allowedPaths", "checks", "limits"]);
  if (
    typeof value.instruction !== "string" ||
    !value.instruction.trim() ||
    value.instruction.length > LIMITS.instructionChars ||
    value.instruction.includes("\0")
  ) {
    throw new RequestProblem(400, "invalid_instruction");
  }
  if (typeof value.model !== "string" || value.model !== expectedModel) {
    throw new RequestProblem(400, "model_not_allowed");
  }
  if (!Array.isArray(value.files) || value.files.length < 1 || value.files.length > LIMITS.fileCount) {
    throw new RequestProblem(400, "invalid_files");
  }
  const files: SourceFile[] = [];
  const paths = new Set<string>();
  let sourceBytes = 0;
  for (const item of value.files) {
    if (!isObject(item)) throw new RequestProblem(400, "invalid_file");
    rejectUnknownKeys(item, ["path", "content"]);
    const path = validateRelativeFilePath(item.path);
    if (paths.has(path)) throw new RequestProblem(400, "duplicate_source_path");
    if (typeof item.content !== "string" || item.content.includes("\0")) {
      throw new RequestProblem(400, "invalid_file_content");
    }
    const bytes = utf8Bytes(item.content);
    if (bytes > LIMITS.fileBytes) throw new RequestProblem(413, "source_file_too_large");
    sourceBytes += bytes;
    if (sourceBytes > LIMITS.sourceBytes) throw new RequestProblem(413, "source_tree_too_large");
    paths.add(path);
    files.push({ path, content: item.content });
  }
  if (
    !Array.isArray(value.allowedPaths) ||
    value.allowedPaths.length < 1 ||
    value.allowedPaths.length > LIMITS.allowedPaths
  ) {
    throw new RequestProblem(400, "invalid_allowed_paths");
  }
  const allowedPaths = value.allowedPaths.map((item) => validateAllowedPath(item, files));
  if (new Set(allowedPaths).size !== allowedPaths.length) {
    throw new RequestProblem(400, "duplicate_allowed_path");
  }
  if (!Array.isArray(value.checks) || value.checks.length < 1 || value.checks.length > LIMITS.checks) {
    throw new RequestProblem(400, "invalid_checks");
  }
  if (!value.checks.every(argvAllowed)) throw new RequestProblem(400, "check_argv_not_allowed");
  const checks = value.checks.map((argv) => [...argv]);
  if (new Set(checks.map((argv) => JSON.stringify(argv))).size !== checks.length) {
    throw new RequestProblem(400, "duplicate_check");
  }
  const limits = value.limits === undefined ? {} : value.limits;
  if (!isObject(limits)) throw new RequestProblem(400, "invalid_limits");
  rejectUnknownKeys(limits, ["maxSteps", "wallTimeSeconds"]);
  const maxSteps = limits.maxSteps ?? 12;
  const wallTimeSeconds = limits.wallTimeSeconds ?? 180;
  if (!Number.isInteger(maxSteps) || (maxSteps as number) < 1 || (maxSteps as number) > 20) {
    throw new RequestProblem(400, "invalid_max_steps");
  }
  if (
    typeof wallTimeSeconds !== "number" ||
    !Number.isInteger(wallTimeSeconds) ||
    wallTimeSeconds < 1 ||
    wallTimeSeconds > 220
  ) {
    throw new RequestProblem(400, "invalid_wall_time");
  }
  return {
    instruction: value.instruction.trim(),
    model: value.model,
    files,
    allowedPaths,
    checks,
    limits: { maxSteps: maxSteps as number, wallTimeSeconds },
  };
}

function bytesToBase64Url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/u, "");
}

function base64UrlToBytes(value: string): Uint8Array {
  if (!/^[A-Za-z0-9_-]+$/u.test(value)) throw new RequestProblem(401, "invalid_run_token");
  const padded = value.replaceAll("-", "+").replaceAll("_", "/").padEnd(Math.ceil(value.length / 4) * 4, "=");
  try {
    return Uint8Array.from(atob(padded), (character) => character.charCodeAt(0));
  } catch {
    throw new RequestProblem(401, "invalid_run_token");
  }
}

async function hmac(secret: string, payload: Uint8Array): Promise<Uint8Array> {
  if (utf8Bytes(secret) < 32) throw new Error("RUN_TOKEN_SECRET must contain at least 32 bytes");
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const source = payload.buffer.slice(
    payload.byteOffset,
    payload.byteOffset + payload.byteLength,
  ) as ArrayBuffer;
  return new Uint8Array(await crypto.subtle.sign("HMAC", key, source));
}

function constantTimeEqual(left: Uint8Array, right: Uint8Array): boolean {
  let difference = left.length ^ right.length;
  const length = Math.max(left.length, right.length);
  for (let index = 0; index < length; index += 1) {
    difference |= (left[index % left.length] ?? 0) ^ (right[index % right.length] ?? 0);
  }
  return difference === 0;
}

type RunTokenAudience =
  | "/internal/v1/chat/completions"
  | "/internal/v1/runs/events"
  | "/internal/v1/runs/approvals";

interface RunCapability {
  v: 1;
  aud: RunTokenAudience;
  runId: string;
  model: string;
  iat: number;
  exp: number;
}

export async function createRunToken(
  secret: string,
  runId: string,
  model: string,
  nowSeconds: number,
  audience: RunTokenAudience = "/internal/v1/chat/completions",
): Promise<string> {
  const payload: RunCapability = {
    v: 1,
    aud: audience,
    runId,
    model,
    iat: nowSeconds,
    exp: nowSeconds + LIMITS.runTokenSeconds,
  };
  const encoded = bytesToBase64Url(encoder.encode(JSON.stringify(payload)));
  const signature = await hmac(secret, encoder.encode(encoded));
  return `${encoded}.${bytesToBase64Url(signature)}`;
}

export async function verifyRunToken(
  secret: string,
  token: string,
  nowSeconds: number,
  expectedAudience: RunTokenAudience = "/internal/v1/chat/completions",
): Promise<RunCapability> {
  const parts = token.split(".");
  if (parts.length !== 2) throw new RequestProblem(401, "invalid_run_token");
  const expected = await hmac(secret, encoder.encode(parts[0]!));
  const supplied = base64UrlToBytes(parts[1]!);
  if (!constantTimeEqual(expected, supplied)) throw new RequestProblem(401, "invalid_run_token");
  let value: unknown;
  try {
    value = JSON.parse(new TextDecoder().decode(base64UrlToBytes(parts[0]!)));
  } catch {
    throw new RequestProblem(401, "invalid_run_token");
  }
  if (!isObject(value)) throw new RequestProblem(401, "invalid_run_token");
  if (
    value.v !== 1 ||
    value.aud !== expectedAudience ||
    typeof value.runId !== "string" ||
    typeof value.model !== "string" ||
    !Number.isInteger(value.iat) ||
    !Number.isInteger(value.exp)
  ) {
    throw new RequestProblem(401, "invalid_run_token");
  }
  const capability = value as unknown as RunCapability;
  if (
    capability.iat > nowSeconds + 5 ||
    capability.exp <= nowSeconds ||
    capability.exp - capability.iat !== LIMITS.runTokenSeconds
  ) {
    throw new RequestProblem(401, "expired_run_token");
  }
  return capability;
}

async function readJsonBounded(request: Request, maxBytes: number): Promise<unknown> {
  const declared = request.headers.get("content-length");
  if (declared !== null && (!/^\d+$/u.test(declared) || Number(declared) > maxBytes)) {
    throw new RequestProblem(413, "request_too_large");
  }
  if (!request.headers.get("content-type")?.toLowerCase().startsWith("application/json")) {
    throw new RequestProblem(415, "json_required");
  }
  const bytes = await readStreamBounded(request.body, maxBytes, 413, "request_too_large");
  try {
    return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
  } catch {
    throw new RequestProblem(400, "invalid_json");
  }
}

async function readStreamBounded(
  body: ReadableStream<Uint8Array> | null,
  maxBytes: number,
  status: number,
  code: string,
): Promise<Uint8Array> {
  if (body === null) return new Uint8Array();
  const reader = body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > maxBytes) {
        await reader.cancel(code);
        throw new RequestProblem(status, code);
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  const result = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    result.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return result;
}

async function readSandboxFileBounded(
  stream: ReadableStream<Uint8Array>,
  decode: (stream: ReadableStream<Uint8Array>) => AsyncIterable<string | Uint8Array>,
  maxBytes: number,
): Promise<Uint8Array> {
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    for await (const chunk of decode(stream)) {
      const bytes = typeof chunk === "string" ? encoder.encode(chunk) : chunk;
      total += bytes.byteLength;
      if (total > maxBytes) {
        try {
          await stream.cancel("sandbox_response_too_large");
        } catch {
          // The decoder may currently hold the stream lock; iterator teardown remains bounded.
        }
        throw new RequestProblem(502, "sandbox_response_too_large");
      }
      chunks.push(bytes);
    }
  } catch (error) {
    if (error instanceof RequestProblem) throw error;
    throw new RequestProblem(502, "sandbox_response_stream_invalid");
  }
  const result = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    result.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return result;
}

function bearer(request: Request): string | null {
  const value = request.headers.get("authorization");
  return value?.startsWith("Bearer ") ? value.slice(7) : null;
}

function secretsEqual(expected: string, supplied: string | null): boolean {
  if (!supplied || utf8Bytes(expected) < 16) return false;
  return constantTimeEqual(encoder.encode(expected), encoder.encode(supplied));
}

function json(value: unknown, status = 200): Response {
  return Response.json(value, {
    status,
    headers: { "cache-control": "no-store", "x-content-type-options": "nosniff" },
  });
}

export function validatedModelApiUrl(value: string): string {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new Error("MODEL_API_URL must be an absolute HTTPS URL");
  }
  if (
    url.protocol !== "https:" ||
    !url.hostname ||
    url.username ||
    url.password ||
    url.search ||
    url.hash ||
    !url.pathname.endsWith("/chat/completions") ||
    url.pathname.includes("//") ||
    url.pathname.endsWith("/")
  ) {
    throw new Error("MODEL_API_URL must be a credential-free HTTPS chat-completions endpoint");
  }
  return url.toString();
}

function requireSdkSuccess(result: { success: boolean }, operation: string): void {
  if (result.success !== true) {
    throw new RequestProblem(502, `sandbox_${operation}_failed`);
  }
}

export async function destroySandboxBounded(
  sandbox: SandboxHandle,
  timeoutMs: number = LIMITS.destroyTimeoutMs,
): Promise<void> {
  let timeoutId: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<never>((_resolve, reject) => {
    timeoutId = setTimeout(
      () => reject(new RequestProblem(500, "sandbox_cleanup_timeout")),
      timeoutMs,
    );
  });
  try {
    await Promise.race([sandbox.destroy(), timeout]);
  } catch (error) {
    if (error instanceof RequestProblem) throw error;
    throw new RequestProblem(500, "sandbox_cleanup_failed");
  } finally {
    if (timeoutId !== undefined) clearTimeout(timeoutId);
  }
}

export async function revokeCapabilityBounded(
  dependencies: WorkerDependencies,
  env: Env,
  runId: string,
  timeoutMs: number = LIMITS.destroyTimeoutMs,
): Promise<void> {
  let timeoutId: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<never>((_resolve, reject) => {
    timeoutId = setTimeout(
      () => reject(new RequestProblem(500, "sandbox_cleanup_timeout")),
      timeoutMs,
    );
  });
  try {
    await Promise.race([dependencies.revokeCapability(env, runId), timeout]);
  } catch (error) {
    if (error instanceof RequestProblem) throw error;
    throw new RequestProblem(500, "sandbox_cleanup_failed");
  } finally {
    if (timeoutId !== undefined) clearTimeout(timeoutId);
  }
}

const ARTIFACT_KEYS = ["request", "events", "checkpoint", "patch", "test_log", "result"] as const;

function hasExactKeys(value: Record<string, unknown>, expected: readonly string[]): boolean {
  const keys = Object.keys(value);
  return keys.length === expected.length && keys.every((key) => expected.includes(key));
}

function validateArtifactMap(
  value: unknown,
  { contents }: { contents: boolean },
): Record<string, string> {
  if (!isObject(value) || !hasExactKeys(value, ARTIFACT_KEYS)) {
    throw new RequestProblem(502, "invalid_sandbox_response");
  }
  const result: Record<string, string> = {};
  for (const key of ARTIFACT_KEYS) {
    const item = value[key];
    if (typeof item !== "string" || (!contents && (item.length < 1 || item.length > 1_024))) {
      throw new RequestProblem(502, "invalid_sandbox_response");
    }
    result[key] = item;
  }
  return result;
}

function validateUsage(value: unknown): Record<string, number | null> {
  const keys = [
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "reasoning_tokens",
    "provider_total_tokens",
    "total_tokens",
  ] as const;
  if (!isObject(value) || !hasExactKeys(value, keys)) {
    throw new RequestProblem(502, "invalid_sandbox_response");
  }
  const result: Record<string, number | null> = {};
  for (const key of keys) {
    const item = value[key];
    if (item === null && key === "provider_total_tokens") {
      result[key] = null;
    } else if (typeof item === "number" && Number.isInteger(item) && item >= 0) {
      result[key] = item;
    } else {
      throw new RequestProblem(502, "invalid_sandbox_response");
    }
  }
  return result;
}

function validateVerification(value: unknown): Record<string, unknown>[] {
  const keys = ["name", "argv", "ok", "exit_code", "duration_seconds", "output"] as const;
  if (!Array.isArray(value) || value.length > 16) {
    throw new RequestProblem(502, "invalid_sandbox_response");
  }
  return value.map((item) => {
    if (
      !isObject(item) ||
      !hasExactKeys(item, keys) ||
      typeof item.name !== "string" ||
      !Array.isArray(item.argv) ||
      !item.argv.every((part) => typeof part === "string") ||
      typeof item.ok !== "boolean" ||
      (item.exit_code !== null &&
        (typeof item.exit_code !== "number" || !Number.isInteger(item.exit_code))) ||
      typeof item.duration_seconds !== "number" ||
      item.duration_seconds < 0 ||
      typeof item.output !== "string"
    ) {
      throw new RequestProblem(502, "invalid_sandbox_response");
    }
    return item;
  });
}

function validateSandboxResponse(
  value: unknown,
  runId: string,
  expectedSuccess: boolean,
  expected: Pick<ValidatedRun, "allowedPaths" | "checks">,
): Record<string, unknown> {
  if (
    isObject(value) &&
    hasExactKeys(value, ["ok", "error"]) &&
    value.ok === false &&
    (value.error === "sandbox_entrypoint_failed" || value.error === "sandbox_agent_failed")
  ) {
    throw new RequestProblem(502, value.error);
  }
  if (
    !isObject(value) ||
    !hasExactKeys(value, ["ok", "result", "artifacts"]) ||
    value.ok !== expectedSuccess ||
    !isObject(value.result)
  ) {
    throw new RequestProblem(502, "sandbox_response_envelope_invalid");
  }
  const runResult = value.result;
  const resultKeys = [
    "run_id",
    "task_id",
    "status",
    "summary",
    "changed_files",
    "verification",
    "usage",
    "terminal_reason",
    "artifacts",
  ] as const;
  if (
    !hasExactKeys(runResult, resultKeys) ||
    typeof runResult.run_id !== "string" ||
    runResult.run_id.length < 1 ||
    runResult.run_id.length > 128 ||
    runResult.task_id !== runId ||
    (expectedSuccess
      ? runResult.status !== "completed"
      : runResult.status !== "failed" && runResult.status !== "cancelled") ||
    typeof runResult.summary !== "string" ||
    typeof runResult.terminal_reason !== "string" ||
    !runResult.terminal_reason ||
    !Array.isArray(runResult.changed_files) ||
    runResult.changed_files.length > LIMITS.fileCount ||
    !runResult.changed_files.every((path) => {
      try {
        validateRelativeFilePath(path);
        return true;
      } catch {
        return false;
      }
    })
  ) {
    throw new RequestProblem(502, "sandbox_result_invalid");
  }
  let verification: Record<string, unknown>[];
  try {
    verification = validateVerification(runResult.verification);
  } catch {
    throw new RequestProblem(502, "sandbox_verification_invalid");
  }
  const seenChecks = new Set<number>();
  for (const item of verification) {
    const name = item.name as string;
    const match = /^check-([1-9][0-9]*)$/u.exec(name);
    const index = match === null ? -1 : Number(match[1]) - 1;
    const expectedArgv = expected.checks[index];
    const argv = item.argv as string[];
    if (
      index < 0 ||
      expectedArgv === undefined ||
      seenChecks.has(index) ||
      argv.length !== expectedArgv.length ||
      !expectedArgv.every((part, position) => part === argv[position])
    ) {
      throw new RequestProblem(502, "sandbox_verification_mismatch");
    }
    seenChecks.add(index);
  }
  if (
    expectedSuccess &&
    (verification.length !== expected.checks.length ||
      verification.some((item) => item.ok !== true || item.exit_code !== 0))
  ) {
    throw new RequestProblem(502, "sandbox_verification_mismatch");
  }
  const changedFiles = runResult.changed_files as string[];
  if (
    changedFiles.some(
      (path) =>
        !expected.allowedPaths.some((allowed) =>
          allowed.endsWith("/**")
            ? path.startsWith(`${allowed.slice(0, -3)}/`)
            : path === allowed,
        ),
    )
  ) {
    throw new RequestProblem(502, "sandbox_changed_files_mismatch");
  }
  let usage: Record<string, number | null>;
  let resultArtifacts: Record<string, string>;
  let bundledArtifacts: Record<string, string>;
  try {
    usage = validateUsage(runResult.usage);
    resultArtifacts = validateArtifactMap(runResult.artifacts, { contents: false });
    bundledArtifacts = validateArtifactMap(value.artifacts, { contents: true });
  } catch {
    throw new RequestProblem(502, "sandbox_artifacts_invalid");
  }
  const validatedResult = {
    ...runResult,
    verification,
    usage,
    artifacts: resultArtifacts,
  };
  return {
    ok: expectedSuccess,
    result: validatedResult,
    artifacts: bundledArtifacts,
  };
}

interface BackgroundRun {
  runId: string;
  input: ValidatedRun;
  runToken: string;
  eventToken: string;
  approvalToken: string;
  proxyUrl: string;
  expiresAt: number;
  runnerRequest: Record<string, unknown>;
}

async function executeRunInSandbox(
  env: Env,
  dependencies: WorkerDependencies,
  run: BackgroundRun,
): Promise<void> {
  let sandbox: SandboxHandle | undefined;
  let capabilityActivated = false;
  try {
    await dependencies.activateCapability(
      env,
      run.runId,
      run.input.model,
      run.expiresAt,
      run.input.limits.maxSteps + 2,
    );
    capabilityActivated = true;
    sandbox = dependencies.getSandbox(env, run.runId);
    await dependencies.markRunSessionRunning(env, run.runId);
    requireSdkSuccess(
      await sandbox.mkdir("/workspace/source", { recursive: true }),
      "mkdir",
    );
    for (const file of run.input.files) {
      const slash = file.path.lastIndexOf("/");
      if (slash >= 0) {
        requireSdkSuccess(
          await sandbox.mkdir(`/workspace/source/${file.path.slice(0, slash)}`, {
            recursive: true,
          }),
          "mkdir",
        );
      }
      requireSdkSuccess(
        await sandbox.writeFile(`/workspace/source/${file.path}`, file.content),
        "write",
      );
    }
    requireSdkSuccess(
      await sandbox.writeFile("/workspace/request.json", JSON.stringify(run.runnerRequest)),
      "write",
    );
    requireSdkSuccess(
      await sandbox.writeFile("/workspace/.rivumi-run-token", run.runToken),
      "write",
    );
    requireSdkSuccess(
      await sandbox.writeFile("/workspace/.rivumi-event-token", run.eventToken),
      "write",
    );
    requireSdkSuccess(
      await sandbox.writeFile("/workspace/.rivumi-approval-token", run.approvalToken),
      "write",
    );
    const execution = await sandbox.exec(FIXED_COMMAND, {
      cwd: "/workspace",
      timeout: LIMITS.sandboxTimeoutMs,
      env: {
        RIVUMI_MODEL_ID: run.input.model,
        RIVUMI_MODEL_GATEWAY_URL: run.proxyUrl,
        RIVUMI_MAX_BUNDLE_BYTES: "1000000",
      },
    });
    const terminalSuccess =
      execution.success === true && execution.exitCode === 0
        ? true
        : execution.success === false && execution.exitCode === 1
          ? false
          : undefined;
    if (terminalSuccess === undefined) {
      throw new RequestProblem(502, "sandbox_exec_failed");
    }
    let resultStream: ReadableStream<Uint8Array>;
    try {
      resultStream = await sandbox.readFileStream("/workspace/response.json");
    } catch {
      throw new RequestProblem(502, "sandbox_read_failed");
    }
    const resultBytes = await readSandboxFileBounded(
      resultStream,
      dependencies.decodeFileStream,
      LIMITS.runResponseBytes,
    );
    let result: unknown;
    try {
      result = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(resultBytes));
    } catch {
      throw new RequestProblem(502, "sandbox_response_json_invalid");
    }
    const output = validateSandboxResponse(result, run.runId, terminalSuccess, run.input);
    const executionSummary = { success: execution.success, exitCode: execution.exitCode };
    await dependencies.completeRunSession(env, run.runId, executionSummary, output);
  } catch (error) {
    const code = error instanceof RequestProblem ? error.code : "internal_error";
    await dependencies.failRunSession(env, run.runId, code).catch(() => undefined);
  } finally {
    let cleanupFailed = false;
    if (capabilityActivated) {
      try {
        await revokeCapabilityBounded(dependencies, env, run.runId);
      } catch {
        cleanupFailed = true;
      }
    }
    if (sandbox !== undefined) {
      try {
        await destroySandboxBounded(sandbox);
      } catch {
        cleanupFailed = true;
      }
    }
    if (cleanupFailed) {
      await dependencies
        .failRunSession(env, run.runId, "sandbox_cleanup_failed")
        .catch(() => undefined);
    }
  }
}

async function handleRun(request: Request, env: Env, dependencies: WorkerDependencies): Promise<Response> {
  if (!secretsEqual(env.CONTROL_PLANE_TOKEN, bearer(request))) {
    throw new RequestProblem(401, "unauthorized");
  }
  const input = validateRunRequest(await readJsonBounded(request, LIMITS.requestBytes), env.OPENAI_MODEL);
  const runId = dependencies.randomUUID();
  const nowSeconds = Math.floor(dependencies.now() / 1000);
  const requestSummary: RunSessionRequestSummary = {
    instruction: input.instruction,
    model: input.model,
    allowedPaths: input.allowedPaths,
    checks: input.checks,
    fileCount: input.files.length,
  };
  const runToken = await createRunToken(env.RUN_TOKEN_SECRET, runId, input.model, nowSeconds);
  const eventToken = await createRunToken(
    env.RUN_TOKEN_SECRET,
    runId,
    input.model,
    nowSeconds,
    "/internal/v1/runs/events",
  );
  const approvalToken = await createRunToken(
    env.RUN_TOKEN_SECRET,
    runId,
    input.model,
    nowSeconds,
    "/internal/v1/runs/approvals",
  );
  const runnerRequest = {
    task_id: runId,
    instruction: input.instruction,
    allowed_paths: input.allowedPaths,
    verification: input.checks.map((argv, index) => ({ name: `check-${index + 1}`, argv })),
    limits: {
      max_steps: input.limits.maxSteps,
      wall_time_seconds: input.limits.wallTimeSeconds,
      max_tool_output_bytes: 200_000,
      max_patch_bytes: 100_000,
    },
  };
  await dependencies.createRunSession(env, runId, requestSummary, dependencies.now());
  dependencies.queueBackgroundRun(() =>
    executeRunInSandbox(env, dependencies, {
      runId,
      input,
      runToken,
      eventToken,
      approvalToken,
      proxyUrl: `${new URL(request.url).origin}/internal/v1`,
      expiresAt: (nowSeconds + LIMITS.runTokenSeconds) * 1_000,
      runnerRequest,
    }),
  );
  return json(
    {
      runId,
      status: "queued",
      links: {
        status: `/v1/runs/${runId}`,
        events: `/v1/runs/${runId}/events`,
        approvals: `/v1/runs/${runId}/approvals`,
      },
    },
    202,
  );
}

async function handleRunResource(
  request: Request,
  env: Env,
  dependencies: WorkerDependencies,
): Promise<Response> {
  if (!secretsEqual(env.CONTROL_PLANE_TOKEN, bearer(request))) {
    throw new RequestProblem(401, "unauthorized");
  }
  const url = new URL(request.url);
  const match = /^\/v1\/runs\/([^/]+)(?:\/(events|artifacts\/[^/]+|approvals(?:\/[^/]+)?|cancel))?$/u.exec(url.pathname);
  if (match === null) throw new RequestProblem(404, "not_found");
  const runId = validateRunIdPathSegment(match[1]!);
  const resource = match[2];
  if (resource === undefined) {
    if (request.method !== "GET") throw new RequestProblem(405, "method_not_allowed");
    return await dependencies.getRunSession(env, runId);
  }
  if (resource === "events") {
    if (request.method !== "GET") throw new RequestProblem(405, "method_not_allowed");
    const stream = url.searchParams.get("stream") === "1" || url.searchParams.get("stream") === "true";
    return await dependencies.getRunSessionEvents(
      env,
      runId,
      stream,
      request.headers.get("last-event-id") ?? undefined,
    );
  }
  if (resource === "cancel") {
    if (request.method !== "POST") throw new RequestProblem(405, "method_not_allowed");
    const cancellation = await dependencies.cancelRunSession(env, runId).catch((error) => {
      if (error instanceof Error && error.message === "run not found") {
        throw new RequestProblem(404, "run_not_found");
      }
      throw error;
    });
    if (!cancellation.terminal) {
      await dependencies.revokeCapability(env, runId);
      await destroySandboxBounded(dependencies.getSandbox(env, runId));
    }
    return json(
      { ok: true, status: cancellation.status },
      cancellation.terminal ? 200 : 202,
    );
  }
  if (resource === "approvals") {
    if (request.method !== "GET") throw new RequestProblem(405, "method_not_allowed");
    return await dependencies.getRunSessionApprovals(env, runId);
  }
  const approval = /^approvals\/([^/]+)$/u.exec(resource);
  if (approval !== null) {
    if (request.method !== "POST") throw new RequestProblem(405, "method_not_allowed");
    const body = await readJsonBounded(request, 8_192);
    if (!isObject(body) || typeof body.decision !== "string") {
      throw new RequestProblem(400, "invalid_approval_decision");
    }
    return await dependencies.submitRunSessionApproval(
      env,
      runId,
      validateRunIdPathSegment(approval[1]!),
      body.decision,
    );
  }
  const artifact = /^artifacts\/([^/]+)$/u.exec(resource);
  if (artifact !== null) {
    if (request.method !== "GET") throw new RequestProblem(405, "method_not_allowed");
    return await dependencies.getRunSessionArtifact(
      env,
      runId,
      validateArtifactName(artifact[1]!),
    );
  }
  throw new RequestProblem(404, "not_found");
}

function validateModelBody(value: unknown, capability: RunCapability, expectedModel: string): Record<string, unknown> {
  if (!isObject(value)) throw new RequestProblem(400, "invalid_model_request");
  rejectUnknownKeys(value, ["model", "messages", "tools"]);
  if (value.model !== capability.model || value.model !== expectedModel) {
    throw new RequestProblem(400, "model_not_allowed");
  }
  if (!Array.isArray(value.messages) || value.messages.length < 1 || value.messages.length > 64) {
    throw new RequestProblem(400, "invalid_messages");
  }
  for (const message of value.messages) {
    if (!isObject(message) || typeof message.role !== "string") {
      throw new RequestProblem(400, "invalid_messages");
    }
  }
  if (value.tools !== undefined && (!Array.isArray(value.tools) || value.tools.length > 16)) {
    throw new RequestProblem(400, "invalid_tools");
  }
  return { ...value, stream: false, max_tokens: 4096 };
}

async function handleModelProxy(request: Request, env: Env, dependencies: WorkerDependencies): Promise<Response> {
  const token = bearer(request);
  if (!token) throw new RequestProblem(401, "invalid_run_token");
  const capability = await verifyRunToken(
    env.RUN_TOKEN_SECRET,
    token,
    Math.floor(dependencies.now() / 1000),
    "/internal/v1/chat/completions",
  );
  const body = validateModelBody(
    await readJsonBounded(request, LIMITS.modelBodyBytes),
    capability,
    env.OPENAI_MODEL,
  );
  const consumption = await dependencies.consumeCapability(
    env,
    capability.runId,
    capability.model,
  );
  if (consumption === "inactive" || consumption === "expired") {
    throw new RequestProblem(401, "inactive_run_token");
  }
  if (consumption === "exhausted") {
    throw new RequestProblem(429, "model_request_budget_exhausted");
  }
  if (!env.OPENAI_API_KEY) throw new Error("OPENAI_API_KEY is not configured");
  const upstream = await dependencies.fetch(validatedModelApiUrl(env.MODEL_API_URL), {
    method: "POST",
    headers: { authorization: `Bearer ${env.OPENAI_API_KEY}`, "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  const declared = upstream.headers.get("content-length");
  if (declared !== null && Number(declared) > LIMITS.modelResponseBytes) {
    throw new RequestProblem(502, "model_response_too_large");
  }
  const contentType = upstream.headers.get("content-type")?.toLowerCase() ?? "";
  if (!contentType.startsWith("application/json")) {
    await upstream.body?.cancel();
    throw new RequestProblem(502, "invalid_model_response");
  }
  const responseBytes = await readStreamBounded(
    upstream.body,
    LIMITS.modelResponseBytes,
    502,
    "model_response_too_large",
  );
  try {
    JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(responseBytes));
  } catch {
    throw new RequestProblem(502, "invalid_model_response");
  }
  const responseBody = responseBytes.buffer.slice(
    responseBytes.byteOffset,
    responseBytes.byteOffset + responseBytes.byteLength,
  ) as ArrayBuffer;
  return new Response(responseBody, {
    status: upstream.status,
    headers: {
      "content-type": upstream.headers.get("content-type") ?? "application/json",
      "cache-control": "no-store",
      "x-content-type-options": "nosniff",
    },
  });
}

async function handleInternalRunEvents(
  request: Request,
  env: Env,
  dependencies: WorkerDependencies,
): Promise<Response> {
  const token = bearer(request);
  if (!token) throw new RequestProblem(401, "invalid_run_token");
  const capability = await verifyRunToken(
    env.RUN_TOKEN_SECRET,
    token,
    Math.floor(dependencies.now() / 1000),
    "/internal/v1/runs/events",
  );
  const match = /^\/internal\/v1\/runs\/([^/]+)\/events$/u.exec(new URL(request.url).pathname);
  if (match === null || match[1] !== capability.runId) {
    throw new RequestProblem(401, "invalid_run_token");
  }
  if (capability.model !== env.OPENAI_MODEL) {
    throw new RequestProblem(400, "model_not_allowed");
  }
  const lines = validateEventAppendBody(
    await readJsonBounded(request, LIMITS.eventAppendBodyBytes),
    capability.runId,
  );
  const capabilityStatus = await dependencies.checkCapability(
    env,
    capability.runId,
    capability.model,
  );
  if (capabilityStatus === "inactive" || capabilityStatus === "expired") {
    throw new RequestProblem(401, "inactive_run_token");
  }
  if (capabilityStatus === "exhausted") {
    throw new RequestProblem(429, "model_request_budget_exhausted");
  }
  await dependencies.appendRunSessionEvents(env, capability.runId, lines);
  return json({ ok: true });
}

async function handleInternalRunApproval(
  request: Request,
  env: Env,
  dependencies: WorkerDependencies,
): Promise<Response> {
  if (request.method !== "GET") throw new RequestProblem(405, "method_not_allowed");
  const token = bearer(request);
  if (!token) throw new RequestProblem(401, "invalid_run_token");
  const capability = await verifyRunToken(
    env.RUN_TOKEN_SECRET,
    token,
    Math.floor(dependencies.now() / 1000),
    "/internal/v1/runs/approvals",
  );
  const match = /^\/internal\/v1\/runs\/([^/]+)\/approvals\/([^/]+)$/u.exec(
    new URL(request.url).pathname,
  );
  if (match === null || match[1] !== capability.runId) {
    throw new RequestProblem(401, "invalid_run_token");
  }
  if (capability.model !== env.OPENAI_MODEL) {
    throw new RequestProblem(400, "model_not_allowed");
  }
  const approvalId = validateRunIdPathSegment(match[2]!);
  const capabilityStatus = await dependencies.checkCapability(
    env,
    capability.runId,
    capability.model,
  );
  if (capabilityStatus === "inactive" || capabilityStatus === "expired") {
    throw new RequestProblem(401, "inactive_run_token");
  }
  if (capabilityStatus === "exhausted") {
    throw new RequestProblem(429, "model_request_budget_exhausted");
  }
  return await dependencies.getRunSessionApproval(env, capability.runId, approvalId);
}

export async function handleRequest(
  request: Request,
  env: Env,
  dependencies: WorkerDependencies,
): Promise<Response> {
  try {
    const path = new URL(request.url).pathname;
    if (path === "/healthz") {
      if (request.method !== "GET") throw new RequestProblem(405, "method_not_allowed");
      return json({ ok: true, service: "rivumi-control-plane" });
    }
    if (path === "/v1/runs") {
      if (request.method !== "POST") throw new RequestProblem(405, "method_not_allowed");
      return await handleRun(request, env, dependencies);
    }
    if (path.startsWith("/v1/runs/")) {
      return await handleRunResource(request, env, dependencies);
    }
    if (path === "/internal/v1/chat/completions") {
      if (request.method !== "POST") throw new RequestProblem(405, "method_not_allowed");
      return await handleModelProxy(request, env, dependencies);
    }
    if (/^\/internal\/v1\/runs\/[^/]+\/events$/u.test(path)) {
      if (request.method !== "POST") throw new RequestProblem(405, "method_not_allowed");
      return await handleInternalRunEvents(request, env, dependencies);
    }
    if (/^\/internal\/v1\/runs\/[^/]+\/approvals\/[^/]+$/u.test(path)) {
      return await handleInternalRunApproval(request, env, dependencies);
    }
    throw new RequestProblem(404, "not_found");
  } catch (error) {
    if (error instanceof RequestProblem) return json({ error: error.code }, error.status);
    return json({ error: "internal_error" }, 500);
  }
}

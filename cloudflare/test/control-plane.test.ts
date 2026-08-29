import { describe, expect, it, vi } from "vitest";
import {
  createRunToken,
  destroySandboxBounded,
  handleRequest,
  LIMITS,
  revokeCapabilityBounded,
  validatedModelApiUrl,
  verifyRunToken,
  type Env,
  type SandboxHandle,
  type WorkerDependencies,
} from "../src/control-plane";

const now = Date.UTC(2026, 7, 21, 0, 0, 0);
const controlToken = "control-plane-token-with-enough-bytes";
const runSecret = "run-token-secret-with-at-least-thirty-two-bytes";
const providerSecret = "provider-secret-must-stay-in-worker";
const model = "gpt-5-mini";

function env(): Env {
  return {
    Sandbox: {} as Env["Sandbox"],
    RUN_CAPABILITIES: {} as Env["RUN_CAPABILITIES"],
    RUN_SESSIONS: {} as Env["RUN_SESSIONS"],
    CONTROL_PLANE_TOKEN: controlToken,
    RUN_TOKEN_SECRET: runSecret,
    OPENAI_API_KEY: providerSecret,
    OPENAI_MODEL: model,
    MODEL_API_URL: "https://api.openai.com/v1/chat/completions",
  };
}

const runId = "11111111-1111-4111-8111-111111111111";

function validSandboxResponse(status: "completed" | "failed" | "cancelled" = "completed"): Record<string, unknown> {
  const artifactPaths = {
    request: "/workspace/runs/run/request.json",
    events: "/workspace/runs/run/events.jsonl",
    checkpoint: "/workspace/runs/run/checkpoint.json",
    patch: "/workspace/runs/run/changes.patch",
    test_log: "/workspace/runs/run/test.log",
    result: "/workspace/runs/run/result.json",
  };
  return {
    ok: status === "completed",
    result: {
      run_id: "agent-run-id",
      task_id: runId,
      status,
      summary: "fixed",
      changed_files: ["hello.py"],
      verification: [
        {
          name: "check-1",
          argv: ["git", "diff", "--check"],
          ok: true,
          exit_code: 0,
          duration_seconds: 0.1,
          output: "",
        },
      ],
      usage: {
        input_tokens: 10,
        output_tokens: 5,
        cached_input_tokens: 0,
        reasoning_tokens: 0,
        provider_total_tokens: 15,
        total_tokens: 15,
      },
      terminal_reason: "verified",
      artifacts: artifactPaths,
    },
    artifacts: {
      request: "{}",
      events: "{}\n",
      checkpoint: "{}",
      patch: "diff --git a/hello.py b/hello.py\n",
      test_log: "ok",
      result: "{}",
    },
  };
}

function requestBody(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    instruction: "Change hello.py without touching other files.",
    model,
    files: [{ path: "hello.py", content: "print('hello')\n" }],
    allowedPaths: ["hello.py"],
    checks: [["git", "diff", "--check"]],
    ...overrides,
  };
}

function request(path: string, body: unknown, token = controlToken): Request {
  return new Request(`https://control.example${path}`, {
    method: "POST",
    headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

function getRequest(path: string, token = controlToken, headers: Record<string, string> = {}): Request {
  return new Request(`https://control.example${path}`, {
    headers: { authorization: `Bearer ${token}`, ...headers },
  });
}

function formatServerSentEvents(ndjson: string, lastEventId?: string): string {
  const afterSequence =
    lastEventId !== undefined && /^(0|[1-9][0-9]*)$/u.test(lastEventId)
      ? Number(lastEventId)
      : undefined;
  return ndjson
    .split("\n")
    .filter((line) => line.length > 0)
    .map((line) => {
      const parsed = JSON.parse(line) as { event_type?: string; sequence?: number };
      if (
        afterSequence !== undefined &&
        typeof parsed.sequence === "number" &&
        Number.isInteger(parsed.sequence) &&
        parsed.sequence <= afterSequence
      ) {
        return "";
      }
      const idLine =
        typeof parsed.sequence === "number" && Number.isInteger(parsed.sequence)
          ? `id: ${parsed.sequence}\n`
          : "";
      return `${idLine}event: ${parsed.event_type ?? "message"}\ndata: ${line}\n\n`;
    })
    .join("");
}

function sandbox(response: unknown = validSandboxResponse()): SandboxHandle & {
  mkdir: ReturnType<typeof vi.fn>;
  writeFile: ReturnType<typeof vi.fn>;
  exec: ReturnType<typeof vi.fn>;
  readFileStream: ReturnType<typeof vi.fn>;
  destroy: ReturnType<typeof vi.fn>;
} {
  return {
    mkdir: vi.fn().mockResolvedValue({ success: true }),
    writeFile: vi.fn().mockResolvedValue({ success: true }),
    exec: vi.fn().mockResolvedValue({ success: true, exitCode: 0, stdout: "", stderr: "" }),
    readFileStream: vi.fn().mockResolvedValue(
      new ReadableStream<Uint8Array>({
        start(controller) {
          controller.enqueue(new TextEncoder().encode(JSON.stringify(response)));
          controller.close();
        },
      }),
    ),
    destroy: vi.fn().mockResolvedValue(undefined),
  };
}

function dependencies(handle: SandboxHandle): WorkerDependencies {
  const sessions = new Map<string, Record<string, any>>();
  return {
    getSandbox: vi.fn().mockReturnValue(handle),
    fetch: vi.fn(),
    now: () => now,
    randomUUID: () => runId,
    activateCapability: vi.fn().mockResolvedValue(undefined),
    checkCapability: vi.fn().mockResolvedValue("ok"),
    consumeCapability: vi.fn().mockResolvedValue("ok"),
    revokeCapability: vi.fn().mockResolvedValue(undefined),
    createRunSession: vi.fn().mockImplementation(async (_env, id, summary, createdAt) => {
      sessions.set(id, {
        runId: id,
        status: "queued",
        model: summary.model,
        createdAt,
        updatedAt: createdAt,
        request: summary,
        cancelRequested: false,
      });
    }),
    markRunSessionRunning: vi.fn().mockImplementation(async (_env, id) => {
      const record = sessions.get(id);
      if (record === undefined) throw new Error("run not found");
      record.status = "running";
      record.updatedAt = now + 1;
    }),
    completeRunSession: vi.fn().mockImplementation(async (_env, id, execution, output) => {
      const record = sessions.get(id);
      if (record === undefined) throw new Error("run not found");
      const result = output.result as Record<string, unknown>;
      record.status = result.status;
      record.execution = execution;
      record.summary = result.summary;
      record.terminalReason = result.terminal_reason;
      record.output = output;
      record.artifacts = {
        ...(output.artifacts as Record<string, string>),
        events: record.eventLines?.join("") ?? (output.artifacts as Record<string, string>).events,
      };
      record.artifactKeys = Object.keys(record.artifacts).sort();
      record.updatedAt = now + 2;
    }),
    appendRunSessionEvents: vi.fn().mockImplementation(async (_env, id, lines) => {
      const record = sessions.get(id);
      if (record === undefined) throw new Error("run not found");
      record.eventLines = [...(record.eventLines ?? []), ...lines];
      record.updatedAt = now + 1;
    }),
    failRunSession: vi.fn().mockImplementation(async (_env, id, error) => {
      const record = sessions.get(id);
      if (record !== undefined) {
        record.status = "failed";
        record.error = error;
        record.updatedAt = now + 3;
      }
    }),
    cancelRunSession: vi.fn().mockImplementation(async (_env, id) => {
      const record = sessions.get(id);
      if (record === undefined) throw new Error("run not found");
      const terminal = ["completed", "failed", "cancelled"].includes(record.status);
      record.cancelRequested = true;
      if (!terminal) record.status = "cancelled";
      return { status: record.status, terminal };
    }),
    getRunSession: vi.fn().mockImplementation(async (_env, id) => {
      const record = sessions.get(id);
      if (record === undefined) return Response.json({ error: "run_not_found" }, { status: 404 });
      const {
        output: _output,
        artifacts: _artifacts,
        eventLines: _eventLines,
        eventBytes: _eventBytes,
        ...visible
      } = record;
      return Response.json(visible, {
        headers: { "cache-control": "no-store", "x-content-type-options": "nosniff" },
      });
    }),
    getRunSessionEvents: vi.fn().mockImplementation(async (_env, id, stream = false, lastEventId) => {
      const record = sessions.get(id);
      if (record === undefined) return Response.json({ error: "run_not_found" }, { status: 404 });
      const events = record.eventLines?.join("") ?? record.artifacts?.events ?? "";
      return new Response(stream ? formatServerSentEvents(events, lastEventId) : events, {
        headers: {
          "cache-control": "no-store",
          "content-type": stream
            ? "text/event-stream; charset=utf-8"
            : "application/x-ndjson; charset=utf-8",
          "x-content-type-options": "nosniff",
        },
      });
    }),
    getRunSessionArtifact: vi.fn().mockImplementation(async (_env, id, name) => {
      const record = sessions.get(id);
      if (record === undefined) return Response.json({ error: "run_not_found" }, { status: 404 });
      const artifact = record.artifacts?.[name];
      if (artifact === undefined) {
        return Response.json({ error: "artifact_not_found" }, { status: 404 });
      }
      return new Response(artifact, {
        headers: {
          "cache-control": "no-store",
          "content-type": "text/plain; charset=utf-8",
          "x-content-type-options": "nosniff",
        },
      });
    }),
    decodeFileStream: async function* (stream) {
      const reader = stream.getReader();
      try {
        while (true) {
          const { done, value } = await reader.read();
          if (done) return;
          yield value;
        }
      } finally {
        reader.releaseLock();
      }
    },
  };
}

describe("POST /v1/runs", () => {
  it("exposes a minimal unauthenticated health route", async () => {
    const response = await handleRequest(
      new Request("https://control.example/healthz"),
      env(),
      dependencies(sandbox()),
    );
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ ok: true, service: "rivumi-control-plane" });
  });

  it("requires control-plane authentication before allocating a sandbox", async () => {
    const handle = sandbox();
    const deps = dependencies(handle);
    const response = await handleRequest(request("/v1/runs", requestBody(), "wrong"), env(), deps);

    expect(response.status).toBe(401);
    expect(deps.getSandbox).not.toHaveBeenCalled();
  });

  it("stages bounded text, runs only the fixed entrypoint, reads the result, and destroys", async () => {
    const handle = sandbox();
    const deps = dependencies(handle);
    const response = await handleRequest(request("/v1/runs", requestBody()), env(), deps);

    expect(response.status).toBe(201);
    expect(handle.writeFile).toHaveBeenCalledWith(
      "/workspace/source/hello.py",
      "print('hello')\n",
    );
    expect(handle.exec).toHaveBeenCalledTimes(1);
    const [command, options] = handle.exec.mock.calls[0]!;
    expect(command).toBe("/usr/local/bin/rivumi-sandbox-run");
    expect(options.env).toMatchObject({
      RIVUMI_MODEL_ID: model,
      RIVUMI_MODEL_GATEWAY_URL: "https://control.example/internal/v1",
    });
    expect(JSON.stringify(options.env)).not.toContain(providerSecret);
    expect(options.env).not.toHaveProperty("RIVUMI_RUN_TOKEN");
    expect(options.env).not.toHaveProperty("RIVUMI_EVENT_TOKEN");
    const tokenWrite = handle.writeFile.mock.calls.find(
      ([path]) => path === "/workspace/.rivumi-run-token",
    );
    const eventTokenWrite = handle.writeFile.mock.calls.find(
      ([path]) => path === "/workspace/.rivumi-event-token",
    );
    expect(tokenWrite?.[1]).toEqual(expect.any(String));
    expect(eventTokenWrite?.[1]).toEqual(expect.any(String));
    const modelCapability = JSON.parse(
      Buffer.from((tokenWrite?.[1] as string).split(".")[0]!, "base64url").toString("utf8"),
    ) as Record<string, unknown>;
    const eventCapability = JSON.parse(
      Buffer.from((eventTokenWrite?.[1] as string).split(".")[0]!, "base64url").toString("utf8"),
    ) as Record<string, unknown>;
    expect(modelCapability.aud).toBe("/internal/v1/chat/completions");
    expect(eventCapability.aud).toBe("/internal/v1/runs/events");
    expect(handle.readFileStream).toHaveBeenCalledWith("/workspace/response.json");
    expect(deps.activateCapability).toHaveBeenCalledWith(
      expect.anything(),
      runId,
      model,
      now + LIMITS.runTokenSeconds * 1_000,
      14,
    );
    expect(deps.revokeCapability).toHaveBeenCalledWith(expect.anything(), runId);
    expect(handle.destroy).toHaveBeenCalledTimes(1);
  });

  it.each([
    { files: [{ path: "../escape.py", content: "x = 1" }] },
    { allowedPaths: ["missing.py"] },
    { checks: [["sh", "-c", "pytest"]] },
    { model: "another-model" },
  ])("rejects an unsafe contract before sandbox allocation: %j", async (override) => {
    const handle = sandbox();
    const deps = dependencies(handle);
    const response = await handleRequest(request("/v1/runs", requestBody(override)), env(), deps);

    expect(response.status).toBe(400);
    expect(deps.getSandbox).not.toHaveBeenCalled();
  });

  it("enforces per-file UTF-8 bounds", async () => {
    const handle = sandbox();
    const deps = dependencies(handle);
    const response = await handleRequest(
      request("/v1/runs", requestBody({ files: [{ path: "big.txt", content: "界".repeat(LIMITS.fileBytes) }] })),
      env(),
      deps,
    );

    expect(response.status).toBe(413);
    expect(deps.getSandbox).not.toHaveBeenCalled();
  });

  it("cancels an oversized streamed ingress body without relying on Content-Length", async () => {
    const handle = sandbox();
    const deps = dependencies(handle);
    const cancel = vi.fn();
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new Uint8Array(500_000));
        controller.enqueue(new Uint8Array(300_001));
      },
      cancel,
    });
    const streamedRequest = new Request("https://control.example/v1/runs", {
      method: "POST",
      headers: { authorization: `Bearer ${controlToken}`, "content-type": "application/json" },
      body,
      duplex: "half",
    } as RequestInit & { duplex: "half" });

    const response = await handleRequest(streamedRequest, env(), deps);

    expect(response.status).toBe(413);
    expect(cancel).toHaveBeenCalled();
    expect(deps.getSandbox).not.toHaveBeenCalled();
  });

  it("destroys the sandbox when execution fails", async () => {
    const handle = sandbox();
    handle.exec.mockRejectedValue(new Error("container unavailable"));
    const deps = dependencies(handle);
    const response = await handleRequest(request("/v1/runs", requestBody()), env(), deps);

    expect(response.status).toBe(500);
    expect(deps.revokeCapability).toHaveBeenCalledWith(expect.anything(), runId);
    expect(handle.destroy).toHaveBeenCalledTimes(1);
  });

  it.each(["mkdir", "writeFile", "exec"] as const)(
    "fails closed when Sandbox %s returns success false",
    async (operation) => {
      const handle = sandbox();
      handle[operation].mockResolvedValue({
        success: false,
        exitCode: 1,
        stdout: "",
        stderr: "failed",
      });
      const deps = dependencies(handle);

      const response = await handleRequest(request("/v1/runs", requestBody()), env(), deps);

      expect(response.status).toBe(502);
      expect(deps.revokeCapability).toHaveBeenCalledWith(expect.anything(), runId);
      expect(handle.destroy).toHaveBeenCalledTimes(1);
    },
  );

  it("returns a strictly matched terminal agent failure with full artifacts", async () => {
    const handle = sandbox(validSandboxResponse("failed"));
    handle.exec.mockResolvedValue({
      success: false,
      exitCode: 1,
      stdout: "",
      stderr: "",
    });

    const response = await handleRequest(
      request("/v1/runs", requestBody()),
      env(),
      dependencies(handle),
    );
    const body = (await response.json()) as Record<string, any>;

    expect(response.status).toBe(201);
    expect(body.execution).toEqual({ success: false, exitCode: 1 });
    expect(body.output.ok).toBe(false);
    expect(body.output.result.status).toBe("failed");
  });

  it("rejects exit zero paired with a failed terminal response", async () => {
    const handle = sandbox(validSandboxResponse("failed"));
    const response = await handleRequest(
      request("/v1/runs", requestBody()),
      env(),
      dependencies(handle),
    );

    expect(response.status).toBe(502);
  });

  it("fails closed when the bounded result stream cannot be opened", async () => {
    const handle = sandbox();
    handle.readFileStream.mockRejectedValue(new Error("missing response"));

    const response = await handleRequest(
      request("/v1/runs", requestBody()),
      env(),
      dependencies(handle),
    );

    expect(response.status).toBe(502);
    expect(handle.destroy).toHaveBeenCalledTimes(1);
  });

  it("decodes the Sandbox SDK SSE stream before parsing the response JSON", async () => {
    const handle = sandbox();
    handle.readFileStream.mockResolvedValue(
      new ReadableStream<Uint8Array>({
        start(controller) {
          controller.enqueue(new TextEncoder().encode("event: file_chunk\ndata: encoded\n\n"));
          controller.close();
        },
      }),
    );
    const deps = dependencies(handle);
    deps.decodeFileStream = vi.fn().mockImplementation(async function* () {
      yield new TextEncoder().encode(JSON.stringify(validSandboxResponse()));
    });

    const response = await handleRequest(request("/v1/runs", requestBody()), env(), deps);

    expect(response.status).toBe(201);
    expect(deps.decodeFileStream).toHaveBeenCalledTimes(1);
  });

  it("bounds the decoded Sandbox file rather than its transport framing", async () => {
    const handle = sandbox();
    const deps = dependencies(handle);
    deps.decodeFileStream = vi.fn().mockImplementation(async function* () {
      yield new Uint8Array(LIMITS.runResponseBytes);
      yield new Uint8Array(1);
    });

    const response = await handleRequest(request("/v1/runs", requestBody()), env(), deps);

    expect(response.status).toBe(502);
    expect(await response.json()).toEqual({ error: "sandbox_response_too_large" });
  });

  it.each(["sandbox_entrypoint_failed", "sandbox_agent_failed"])(
    "preserves the bounded Sandbox failure class: %s",
    async (error) => {
      const handle = sandbox({ ok: false, error });
      handle.exec.mockResolvedValue({ success: false, exitCode: 1, stdout: "", stderr: "" });

      const response = await handleRequest(
        request("/v1/runs", requestBody()),
        env(),
        dependencies(handle),
      );

      expect(response.status).toBe(502);
      expect(await response.json()).toEqual({ error });
    },
  );

  it.each([
    { ...validSandboxResponse(), injected: "value" },
    { ok: true, artifacts: validSandboxResponse().artifacts },
    { ...validSandboxResponse(), ok: false },
    {
      ...validSandboxResponse(),
      result: { ...(validSandboxResponse().result as Record<string, unknown>), task_id: "other" },
    },
  ])("rejects a forged or malformed Sandbox response: %j", async (forged) => {
    const handle = sandbox(forged);
    const response = await handleRequest(
      request("/v1/runs", requestBody()),
      env(),
      dependencies(handle),
    );

    expect(response.status).toBe(502);
    expect(handle.destroy).toHaveBeenCalledTimes(1);
  });

  it.each([
    {
      result: {
        ...(validSandboxResponse().result as Record<string, unknown>),
        verification: [],
      },
    },
    {
      result: {
        ...(validSandboxResponse().result as Record<string, unknown>),
        verification: [
          {
            name: "check-1",
            argv: ["python3", "-m", "pytest", "-q"],
            ok: true,
            exit_code: 0,
            duration_seconds: 0.1,
            output: "",
          },
        ],
      },
    },
    {
      result: {
        ...(validSandboxResponse().result as Record<string, unknown>),
        verification: [
          {
            name: "check-1",
            argv: ["git", "diff", "--check"],
            ok: false,
            exit_code: 1,
            duration_seconds: 0.1,
            output: "failed",
          },
        ],
      },
    },
    {
      result: {
        ...(validSandboxResponse().result as Record<string, unknown>),
        changed_files: ["other.py"],
      },
    },
  ])("rejects a completed result that does not match its request contract: %j", async (resultOverride) => {
    const forged = { ...validSandboxResponse(), ...resultOverride };
    const handle = sandbox(forged);

    const response = await handleRequest(
      request("/v1/runs", requestBody()),
      env(),
      dependencies(handle),
    );

    expect(response.status).toBe(502);
  });

  it("does not claim completion when destroy rejects", async () => {
    const handle = sandbox();
    handle.destroy.mockRejectedValue(new Error("destroy failed"));
    const deps = dependencies(handle);

    const response = await handleRequest(request("/v1/runs", requestBody()), env(), deps);

    expect(response.status).toBe(500);
    expect(await response.json()).toEqual({ error: "sandbox_cleanup_failed" });
    expect(deps.revokeCapability).toHaveBeenCalledBefore(handle.destroy);
  });

  it("bounds a hanging destroy call", async () => {
    const handle = sandbox();
    handle.destroy.mockReturnValue(new Promise<void>(() => undefined));

    await expect(destroySandboxBounded(handle, 10)).rejects.toMatchObject({
      code: "sandbox_cleanup_timeout",
    });
  });

  it("bounds a hanging capability revocation before destroying", async () => {
    const handle = sandbox();
    const deps = dependencies(handle);
    deps.revokeCapability = vi.fn().mockReturnValue(new Promise<void>(() => undefined));

    await expect(revokeCapabilityBounded(deps, env(), runId, 10)).rejects.toMatchObject({
      code: "sandbox_cleanup_timeout",
    });
  });

  it("revokes the capability so a captured token cannot proxy after teardown", async () => {
    const handle = sandbox();
    let active = false;
    const deps = dependencies(handle);
    deps.activateCapability = vi.fn().mockImplementation(async () => {
      active = true;
    });
    deps.revokeCapability = vi.fn().mockImplementation(async () => {
      active = false;
    });
    deps.checkCapability = vi.fn().mockImplementation(async () =>
      active ? "ok" : "inactive",
    );
    deps.consumeCapability = vi.fn().mockImplementation(async () =>
      active ? "ok" : "inactive",
    );

    expect((await handleRequest(request("/v1/runs", requestBody()), env(), deps)).status).toBe(201);
    const token = handle.writeFile.mock.calls.find(
      ([path]) => path === "/workspace/.rivumi-run-token",
    )?.[1] as string;
    const eventToken = handle.writeFile.mock.calls.find(
      ([path]) => path === "/workspace/.rivumi-event-token",
    )?.[1] as string;
    const replay = await handleRequest(
      request(
        "/internal/v1/chat/completions",
        { model, messages: [{ role: "user", content: "late" }] },
        token,
      ),
      env(),
      deps,
    );

    expect(replay.status).toBe(401);
    expect(deps.fetch).not.toHaveBeenCalled();

    const eventReplay = await handleRequest(
      request(
        `/internal/v1/runs/${runId}/events`,
        {
          lines: [
            JSON.stringify({
              event_type: "run.created",
              run_id: "agent-run-id",
              task_id: runId,
              sequence: 0,
              data: {},
            }) + "\n",
          ],
        },
        eventToken,
      ),
      env(),
      deps,
    );
    expect(eventReplay.status).toBe(401);
    expect(deps.appendRunSessionEvents).not.toHaveBeenCalled();
  });
});

describe("RunSession APIs", () => {
  it("persists run status and exposes bounded artifact metadata", async () => {
    const handle = sandbox();
    const deps = dependencies(handle);

    expect((await handleRequest(request("/v1/runs", requestBody()), env(), deps)).status).toBe(201);
    const response = await handleRequest(getRequest(`/v1/runs/${runId}`), env(), deps);
    const body = (await response.json()) as Record<string, unknown>;

    expect(response.status).toBe(200);
    expect(body).toMatchObject({
      runId,
      status: "completed",
      model,
      summary: "fixed",
      terminalReason: "verified",
      cancelRequested: false,
      artifactKeys: ["checkpoint", "events", "patch", "request", "result", "test_log"],
    });
    expect(JSON.stringify(body)).not.toContain("diff --git");
  });

  it("requires auth before exposing run status or artifacts", async () => {
    const handle = sandbox();
    const deps = dependencies(handle);

    expect((await handleRequest(request("/v1/runs", requestBody()), env(), deps)).status).toBe(201);

    expect((await handleRequest(getRequest(`/v1/runs/${runId}`, "wrong"), env(), deps)).status).toBe(
      401,
    );
    expect(
      (await handleRequest(getRequest(`/v1/runs/${runId}/artifacts/patch`, "wrong"), env(), deps))
        .status,
    ).toBe(401);
  });

  it("returns stored terminal events as ndjson", async () => {
    const handle = sandbox();
    const deps = dependencies(handle);

    expect((await handleRequest(request("/v1/runs", requestBody()), env(), deps)).status).toBe(201);
    const response = await handleRequest(getRequest(`/v1/runs/${runId}/events`), env(), deps);
    const explicitDefault = await handleRequest(
      getRequest(`/v1/runs/${runId}/events?stream=0`),
      env(),
      deps,
    );

    expect(response.status).toBe(200);
    expect(response.headers.get("content-type")).toContain("application/x-ndjson");
    expect(response.headers.get("cache-control")).toBe("no-store");
    expect(await response.text()).toBe("{}\n");
    expect(explicitDefault.headers.get("content-type")).toContain("application/x-ndjson");
    expect(await explicitDefault.text()).toBe("{}\n");
    expect(deps.getRunSessionEvents).toHaveBeenCalledWith(expect.anything(), runId, false, undefined);
  });

  it("returns stored events as SSE when stream mode is requested", async () => {
    const handle = sandbox();
    const deps = dependencies(handle);
    await deps.createRunSession(env(), runId, {
      instruction: "running",
      model,
      allowedPaths: ["hello.py"],
      checks: [["git", "diff", "--check"]],
      fileCount: 1,
    }, now);
    await deps.appendRunSessionEvents(env(), runId, [
      JSON.stringify({
        event_type: "run.created",
        run_id: "agent-run-id",
        task_id: runId,
        sequence: 0,
        data: {},
      }) + "\n",
    ]);

    const response = await handleRequest(getRequest(`/v1/runs/${runId}/events?stream=1`), env(), deps);
    const body = await response.text();

    expect(response.status).toBe(200);
    expect(response.headers.get("content-type")).toContain("text/event-stream");
    expect(response.headers.get("cache-control")).toBe("no-store");
    expect(body).toContain("id: 0\nevent: run.created\ndata: ");
    expect(body).toContain('"task_id":"11111111-1111-4111-8111-111111111111"');
    expect(deps.getRunSessionEvents).toHaveBeenCalledWith(expect.anything(), runId, true, undefined);
  });

  it("forwards Last-Event-ID when streaming stored events", async () => {
    const handle = sandbox();
    const deps = dependencies(handle);
    await deps.createRunSession(env(), runId, {
      instruction: "running",
      model,
      allowedPaths: ["hello.py"],
      checks: [["git", "diff", "--check"]],
      fileCount: 1,
    }, now);
    await deps.appendRunSessionEvents(env(), runId, [
      JSON.stringify({
        event_type: "run.created",
        run_id: "agent-run-id",
        task_id: runId,
        sequence: 0,
        data: {},
      }) + "\n",
      JSON.stringify({
        event_type: "run.completed",
        run_id: "agent-run-id",
        task_id: runId,
        sequence: 1,
        data: {},
      }) + "\n",
    ]);

    const response = await handleRequest(
      getRequest(`/v1/runs/${runId}/events?stream=1`, controlToken, { "last-event-id": "0" }),
      env(),
      deps,
    );
    const body = await response.text();

    expect(body).not.toContain("id: 0\nevent: run.created");
    expect(body).toContain("id: 1\nevent: run.completed");
    expect(deps.getRunSessionEvents).toHaveBeenCalledWith(expect.anything(), runId, true, "0");
  });

  it("accepts internal live event appends before terminal bundle completion", async () => {
    const handle = sandbox();
    const deps = dependencies(handle);
    await deps.createRunSession(env(), runId, {
      instruction: "running",
      model,
      allowedPaths: ["hello.py"],
      checks: [["git", "diff", "--check"]],
      fileCount: 1,
    }, now);
    await deps.markRunSessionRunning(env(), runId);
    const token = await createRunToken(
      runSecret,
      runId,
      model,
      Math.floor(now / 1000),
      "/internal/v1/runs/events",
    );
    const line = JSON.stringify({
      event_type: "run.created",
      run_id: "agent-run-id",
      task_id: runId,
      sequence: 0,
      data: {},
    }) + "\n";

    const append = await handleRequest(
      request(`/internal/v1/runs/${runId}/events`, { lines: [line] }, token),
      env(),
      deps,
    );
    const events = await handleRequest(getRequest(`/v1/runs/${runId}/events`), env(), deps);

    expect(append.status).toBe(200);
    expect(deps.appendRunSessionEvents).toHaveBeenCalledWith(expect.anything(), runId, [line]);
    expect(deps.consumeCapability).not.toHaveBeenCalled();
    expect(deps.fetch).not.toHaveBeenCalled();
    expect(await events.text()).toBe(line);
  });

  it("rejects malformed internal live event appends", async () => {
    const handle = sandbox();
    const deps = dependencies(handle);
    const token = await createRunToken(
      runSecret,
      runId,
      model,
      Math.floor(now / 1000),
      "/internal/v1/runs/events",
    );

    const response = await handleRequest(
      request(`/internal/v1/runs/${runId}/events`, { lines: ["not-json\n"] }, token),
      env(),
      deps,
    );

    expect(response.status).toBe(400);
    expect(deps.appendRunSessionEvents).not.toHaveBeenCalled();
  });

  it("rejects internal live event appends with mismatched run ids or inactive capability", async () => {
    const deps = dependencies(sandbox());
    const token = await createRunToken(
      runSecret,
      runId,
      model,
      Math.floor(now / 1000),
      "/internal/v1/runs/events",
    );

    const mismatched = await handleRequest(
      request("/internal/v1/runs/other/events", { lines: [] }, token),
      env(),
      deps,
    );
    deps.checkCapability = vi.fn().mockResolvedValue("inactive");
    const inactive = await handleRequest(
      request(
        `/internal/v1/runs/${runId}/events`,
        {
          lines: [
            JSON.stringify({
              event_type: "run.created",
              run_id: "agent-run-id",
              task_id: runId,
              sequence: 0,
              data: {},
            }) + "\n",
          ],
        },
        token,
      ),
      env(),
      deps,
    );

    expect(mismatched.status).toBe(401);
    expect(inactive.status).toBe(401);
    expect(deps.appendRunSessionEvents).not.toHaveBeenCalled();
  });

  it("requires POST for internal live event appends", async () => {
    const token = await createRunToken(
      runSecret,
      runId,
      model,
      Math.floor(now / 1000),
      "/internal/v1/runs/events",
    );
    const response = await handleRequest(
      getRequest(`/internal/v1/runs/${runId}/events`, token),
      env(),
      dependencies(sandbox()),
    );

    expect(response.status).toBe(405);
  });

  it("rejects the model-proxy token on internal live event appends", async () => {
    const token = await createRunToken(runSecret, runId, model, Math.floor(now / 1000));
    const response = await handleRequest(
      request(
        `/internal/v1/runs/${runId}/events`,
        {
          lines: [
            JSON.stringify({
              event_type: "run.created",
              run_id: "agent-run-id",
              task_id: runId,
              sequence: 0,
              data: {},
            }) + "\n",
          ],
        },
        token,
      ),
      env(),
      dependencies(sandbox()),
    );

    expect(response.status).toBe(401);
  });

  it("rejects wrong event audience before parsing the event body", async () => {
    const token = await createRunToken(runSecret, runId, model, Math.floor(now / 1000));
    const response = await handleRequest(
      request(`/internal/v1/runs/${runId}/events`, { lines: ["not-json\n"] }, token),
      env(),
      dependencies(sandbox()),
    );

    expect(response.status).toBe(401);
  });

  it("prefers live event lines over terminal event artifact after completion", async () => {
    const handle = sandbox();
    const deps = dependencies(handle);
    await deps.createRunSession(env(), runId, {
      instruction: "running",
      model,
      allowedPaths: ["hello.py"],
      checks: [["git", "diff", "--check"]],
      fileCount: 1,
    }, now);
    await deps.markRunSessionRunning(env(), runId);
    await deps.appendRunSessionEvents(env(), runId, [
      JSON.stringify({
        event_type: "run.created",
        run_id: "agent-run-id",
        task_id: runId,
        sequence: 0,
        data: {},
      }) + "\n",
    ]);
    const validated = validSandboxResponse();

    await deps.completeRunSession(
      env(),
      runId,
      { success: true, exitCode: 0 },
      validated as Record<string, unknown>,
    );
    const events = await handleRequest(getRequest(`/v1/runs/${runId}/events`), env(), deps);
    const eventText = await events.text();

    expect(eventText).toContain("run.created");
    expect(eventText).not.toBe((validated.artifacts as Record<string, string>).events);
  });

  it("returns named artifacts but rejects unsafe artifact names", async () => {
    const handle = sandbox();
    const deps = dependencies(handle);

    expect((await handleRequest(request("/v1/runs", requestBody()), env(), deps)).status).toBe(201);
    const patch = await handleRequest(getRequest(`/v1/runs/${runId}/artifacts/patch`), env(), deps);
    const unknown = await handleRequest(
      getRequest(`/v1/runs/${runId}/artifacts/other`),
      env(),
      deps,
    );

    expect(patch.status).toBe(200);
    expect(await patch.text()).toBe("diff --git a/hello.py b/hello.py\n");
    expect(unknown.status).toBe(404);
    expect(await unknown.json()).toEqual({ error: "artifact_not_found" });
  });

  it("best-effort cancel revokes capability and destroys the sandbox for non-terminal runs", async () => {
    const handle = sandbox();
    const deps = dependencies(handle);
    await deps.createRunSession(env(), runId, {
      instruction: "pending",
      model,
      allowedPaths: ["hello.py"],
      checks: [["git", "diff", "--check"]],
      fileCount: 1,
    }, now);
    await deps.markRunSessionRunning(env(), runId);

    const response = await handleRequest(
      request(`/v1/runs/${runId}/cancel`, {}),
      env(),
      deps,
    );

    expect(response.status).toBe(202);
    expect(await response.json()).toEqual({ ok: true, status: "cancelled" });
    expect(deps.revokeCapability).toHaveBeenCalledWith(expect.anything(), runId);
    expect(handle.destroy).toHaveBeenCalledTimes(1);
  });

  it("cancel is idempotent for terminal runs", async () => {
    const handle = sandbox();
    const deps = dependencies(handle);

    expect((await handleRequest(request("/v1/runs", requestBody()), env(), deps)).status).toBe(201);
    const response = await handleRequest(request(`/v1/runs/${runId}/cancel`, {}), env(), deps);

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ ok: true, status: "completed" });
    expect(handle.destroy).toHaveBeenCalledTimes(1);
  });

  it("returns not_found for unknown run resources", async () => {
    const deps = dependencies(sandbox());

    expect((await handleRequest(getRequest("/v1/runs/missing"), env(), deps)).status).toBe(404);
    expect((await handleRequest(request("/v1/runs/missing/cancel", {}), env(), deps)).status).toBe(
      404,
    );
  });
});

describe("internal model proxy", () => {
  it("enforces run-token audiences independently of routes", async () => {
    const issuedAt = Math.floor(now / 1000);
    const chatToken = await createRunToken(runSecret, "run-1", model, issuedAt);
    const eventToken = await createRunToken(
      runSecret,
      "run-1",
      model,
      issuedAt,
      "/internal/v1/runs/events",
    );

    await expect(verifyRunToken(runSecret, chatToken, issuedAt)).resolves.toMatchObject({
      aud: "/internal/v1/chat/completions",
      runId: "run-1",
    });
    await expect(
      verifyRunToken(runSecret, eventToken, issuedAt, "/internal/v1/runs/events"),
    ).resolves.toMatchObject({ aud: "/internal/v1/runs/events", runId: "run-1" });
    await expect(verifyRunToken(runSecret, eventToken, issuedAt)).rejects.toMatchObject({
      status: 401,
      code: "invalid_run_token",
    });
    await expect(
      verifyRunToken(runSecret, chatToken, issuedAt, "/internal/v1/runs/events"),
    ).rejects.toMatchObject({ status: 401, code: "invalid_run_token" });
  });

  it("accepts a short-lived capability, pins the model, and keeps the provider key upstream", async () => {
    const handle = sandbox();
    const deps = dependencies(handle);
    const upstream = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ choices: [{ message: { content: "ok" } }] }), {
        headers: { "content-type": "application/json" },
      }),
    );
    deps.fetch = upstream;
    const token = await createRunToken(runSecret, "run-1", model, Math.floor(now / 1000));
    const capability = JSON.parse(
      Buffer.from(token.split(".")[0]!, "base64url").toString("utf8"),
    ) as Record<string, unknown>;
    expect(capability).toMatchObject({
      aud: "/internal/v1/chat/completions",
      runId: "run-1",
      model,
    });
    const response = await handleRequest(
      request(
        "/internal/v1/chat/completions",
        { model, messages: [{ role: "user", content: "hello" }] },
        token,
      ),
      env(),
      deps,
    );

    expect(response.status).toBe(200);
    expect(upstream).toHaveBeenCalledTimes(1);
    const [url, init] = upstream.mock.calls[0]!;
    expect(url).toBe("https://api.openai.com/v1/chat/completions");
    expect(init.headers.authorization).toBe(`Bearer ${providerSecret}`);
    expect(JSON.parse(init.body)).toMatchObject({ model, stream: false, max_tokens: 4096 });
    expect(await response.text()).not.toContain(providerSecret);
  });

  it("rejects an event-append token on the model proxy", async () => {
    const deps = dependencies(sandbox());
    const token = await createRunToken(
      runSecret,
      "run-1",
      model,
      Math.floor(now / 1000),
      "/internal/v1/runs/events",
    );
    const response = await handleRequest(
      request(
        "/internal/v1/chat/completions",
        { model, messages: [{ role: "user", content: "hello" }] },
        token,
      ),
      env(),
      deps,
    );

    expect(response.status).toBe(401);
    expect(deps.fetch).not.toHaveBeenCalled();
  });

  it("rejects an expired run capability before calling the provider", async () => {
    const handle = sandbox();
    const deps = dependencies(handle);
    const token = await createRunToken(
      runSecret,
      "run-1",
      model,
      Math.floor(now / 1000) - LIMITS.runTokenSeconds - 1,
    );
    const response = await handleRequest(
      request(
        "/internal/v1/chat/completions",
        { model, messages: [{ role: "user", content: "hello" }] },
        token,
      ),
      env(),
      deps,
    );

    expect(response.status).toBe(401);
    expect(deps.fetch).not.toHaveBeenCalled();
  });

  it("cancels an oversized streamed provider response without Content-Length", async () => {
    const handle = sandbox();
    const deps = dependencies(handle);
    const cancel = vi.fn();
    deps.fetch = vi.fn().mockResolvedValue(
      new Response(
        new ReadableStream<Uint8Array>({
          start(controller) {
            controller.enqueue(new Uint8Array(700_000));
            controller.enqueue(new Uint8Array(400_001));
          },
          cancel,
        }),
        { headers: { "content-type": "application/json" } },
      ),
    );
    const token = await createRunToken(runSecret, "run-1", model, Math.floor(now / 1000));

    const response = await handleRequest(
      request(
        "/internal/v1/chat/completions",
        { model, messages: [{ role: "user", content: "hello" }] },
        token,
      ),
      env(),
      deps,
    );

    expect(response.status).toBe(502);
    expect(cancel).toHaveBeenCalled();
  });

  it("rejects extra OpenAI fields and model substitution", async () => {
    const handle = sandbox();
    const deps = dependencies(handle);
    const token = await createRunToken(runSecret, "run-1", model, Math.floor(now / 1000));
    const response = await handleRequest(
      request(
        "/internal/v1/chat/completions",
        { model: "gpt-4o", messages: [{ role: "user", content: "hello" }], stream: true },
        token,
      ),
      env(),
      deps,
    );

    expect(response.status).toBe(400);
    expect(deps.fetch).not.toHaveBeenCalled();
  });
});

describe("operator model endpoint", () => {
  it("accepts an operator-owned OpenAI-compatible HTTPS endpoint", () => {
    expect(validatedModelApiUrl("https://openrouter.ai/api/v1/chat/completions")).toBe(
      "https://openrouter.ai/api/v1/chat/completions",
    );
  });

  it.each([
    "http://api.groq.com/openai/v1/chat/completions",
    "https://user:password@api.example/v1/chat/completions",
    "https://api.example/v1/chat/completions?key=secret",
    "https://api.example/v1/chat/completions#fragment",
  ])("rejects an unsafe operator endpoint: %s", (url) => {
    expect(() => validatedModelApiUrl(url)).toThrow();
  });
});
